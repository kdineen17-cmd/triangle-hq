"""Join Sleeper rosters to NBA game logs, then build lock-in advice and roster profiles."""
from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from .model import Dist, Plan, build_dist, sample_weights, solve
from .scoring import breakdown, fantasy_points, unknown_keys
from .sources import Game, previous_season, season_label

# Sleeper team codes that differ from NBA.com tricodes.
TEAM_ALIASES = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "PHO": "PHX", "UTAH": "UTA",
                "WSH": "WAS", "BRK": "BKN", "CHO": "CHA"}
NON_STARTER_SLOTS = {"BN", "IR", "TAXI"}

# (P(plays next game), P(plays each later game)) by Sleeper injury_status. Rough priors, not data.
INJURY_PLAY = {
    "out": (0.0, 0.6), "ir": (0.0, 0.2), "sus": (0.0, 0.5), "ofs": (0.0, 0.0),
    "doubtful": (0.25, 0.75), "questionable": (0.6, 0.9), "gtd": (0.6, 0.9),
    "dtd": (0.6, 0.9), "day to day": (0.6, 0.9), "probable": (0.9, 0.95),
}
HEALTHY_PLAY = 0.95


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s.replace("-", " "))
    return " ".join(w for w in s.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"})


def game_date(row: dict) -> dt.date:
    return dt.date.fromisoformat(str(row["GAME_DATE"])[:10])


def play_probs(injury: str | None, n: int) -> list[float]:
    first, later = INJURY_PLAY.get((injury or "").strip().lower(), (HEALTHY_PLAY, HEALTHY_PLAY))
    return [first if i == 0 else later for i in range(n)]


class LogIndex:
    """Game logs grouped by NBA player id, newest first, with a name index for matching."""

    def __init__(self, rows: list[dict]):
        self.by_player: dict[int, list[dict]] = defaultdict(list)
        self.names: dict[str, set[int]] = defaultdict(set)
        self.initials: dict[str, set[int]] = defaultdict(set)
        for r in rows:
            pid = int(r["PLAYER_ID"])
            self.by_player[pid].append(r)
            n = norm_name(r["PLAYER_NAME"])
            self.names[n].add(pid)
            if " " in n:
                first, last = n.split(" ", 1)
                self.initials[f"{first[0]} {last}"].add(pid)
        for games in self.by_player.values():
            games.sort(key=game_date, reverse=True)

    def team(self, pid: int) -> str | None:
        games = self.by_player.get(pid)
        return games[0]["TEAM_ABBREVIATION"] if games else None

    def match(self, name: str, team: str | None) -> int | None:
        n = norm_name(name)
        for pool in (self.names.get(n), self._initial(n)):
            if not pool:
                continue
            if len(pool) == 1:
                return next(iter(pool))
            on_team = [p for p in pool if team and self.team(p) == team]
            if len(on_team) == 1:
                return on_team[0]
        return None

    def _initial(self, n: str) -> set[int] | None:
        # "Nic Claxton" vs "Nicolas Claxton": same first initial and last name.
        if " " not in n:
            return None
        first, last = n.split(" ", 1)
        return self.initials.get(f"{first[0]} {last}")


@dataclass
class WeekGame:
    game: Game
    row: dict | None      # box score if played
    score: float | None
    status: str           # "played" | "dnp" | "upcoming"


@dataclass
class Advice:
    action: str           # LOCK / HOLD / FINAL / LOCKED / WAIT / NO GAMES / NO DATA
    current: float | None = None      # score on the table (latest unlocked game)
    threshold: float | None = None    # lock the current score if it is at least this
    hold_value: float | None = None   # expected final score if you pass on `current`
    upcoming_thresholds: list[float] = field(default_factory=list)
    expected_week: float = 0.0
    note: str = ""


@dataclass
class PlayerView:
    sleeper_id: str
    name: str
    positions: list[str]
    injury: str | None
    team: str | None
    nba_id: int | None
    cur_rows: list[dict]
    prev_rows: list[dict]
    dist: Dist | None
    week: list[WeekGame]
    weekly_value: float           # expected locked score for the whole week, before it starts
    per_game_cats: dict[str, float]
    advice: Advice | None = None
    starter: bool = False

    @property
    def mean(self) -> float:
        return self.dist.mean if self.dist else 0.0

    @property
    def stdev(self) -> float:
        return self.dist.stdev if self.dist else 0.0

    @property
    def games_this_week(self) -> int:
        return len(self.week)

    @property
    def lock_premium(self) -> float:
        """How much the lock-in format adds over a plain average game this week."""
        return self.weekly_value - self.mean if self.dist and self.week else 0.0


def advise(dist: Dist | None, week: list[WeekGame], injury: str | None,
           today: dt.date, locked: float | None = None) -> Advice:
    if locked is not None:
        return Advice("LOCKED", current=locked, expected_week=locked)
    if not week:
        return Advice("NO GAMES", note="no games in this window")
    if dist is None:
        return Advice("NO DATA", note="no game logs matched")
    played = [g for g in week if g.status == "played"]
    upcoming = [g for g in week if g.status == "upcoming"]
    plan = solve(dist, play_probs(injury, len(upcoming)))
    ups = list(plan.thresholds)
    today_note = "game today: rerun after it goes final" if any(g.game.date == today for g in upcoming) else ""
    if not played:
        ev = plan.expected_value if upcoming else 0.0
        return Advice("WAIT", upcoming_thresholds=ups, expected_week=ev, note=today_note)
    x = played[-1].score
    if not upcoming:
        return Advice("FINAL", current=x, expected_week=x, note="last game of the week counts")
    thr = _current_threshold(plan)
    hold = plan.hold_value(0, x)
    action = "LOCK" if x >= thr else "HOLD"
    return Advice(action, current=x, threshold=thr, hold_value=hold, upcoming_thresholds=ups,
                  expected_week=max(x, hold), note=today_note)


def _current_threshold(plan: Plan) -> float:
    a, b = plan.alphas[0], plan.betas[0]
    return b / (1 - a) if a < 1 else -math.inf


def week_window(day: dt.date) -> tuple[dt.date, dt.date]:
    """Monday-Sunday week containing `day` (Sleeper NBA matchup weeks run Mon-Sun)."""
    start = day - dt.timedelta(days=day.weekday())
    return start, start + dt.timedelta(days=6)


@dataclass
class League:
    league: dict
    scoring: dict[str, float]
    rosters: list[dict]
    team_names: dict[int, str]          # roster_id -> display name
    my_roster_id: int | None
    views: dict[str, PlayerView]        # sleeper player id -> view (rostered + matched free agents)
    free_agents: list[PlayerView]
    unmatched: list[str]
    warnings: list[str]
    start: dt.date
    end: dt.date
    today: dt.date

    @property
    def starter_slots(self) -> int:
        return sum(1 for p in self.league.get("roster_positions", []) if p not in NON_STARTER_SLOTS)

    def roster_views(self, roster: dict) -> list[PlayerView]:
        return [self.views[p] for p in roster.get("players") or [] if p in self.views]


def load(source, league_id: str, username: str | None, today: dt.date,
         start: dt.date | None = None, end: dt.date | None = None,
         locked: dict[str, float] | None = None) -> League:
    locked = {norm_name(k): v for k, v in (locked or {}).items()}
    league = source.league(league_id)
    scoring = {k: float(v) for k, v in (league.get("scoring_settings") or {}).items()}
    rosters = source.rosters(league_id)
    users = {u["user_id"]: u for u in source.users(league_id)}
    team_names = {}
    for r in rosters:
        u = users.get(r.get("owner_id")) or {}
        team_names[r["roster_id"]] = (u.get("metadata") or {}).get("team_name") or u.get("display_name") \
            or f"Roster {r['roster_id']}"
    my_roster_id = None
    if username:
        uid = source.user_id(username)
        mine = [r for r in rosters if r.get("owner_id") == uid or uid in (r.get("co_owners") or [])]
        if not mine:
            raise SystemExit(f"'{username}' doesn't own a roster in league {league_id}")
        my_roster_id = mine[0]["roster_id"]

    if start is None or end is None:
        s, e = week_window(today)
        start, end = start or s, end or e

    season = season_label(today)
    cur = LogIndex(source.game_logs(season, current=True))
    prev = LogIndex(source.game_logs(previous_season(season), current=False))
    schedule = [g for g in source.schedule() if start <= g.date <= end]
    players = source.players()

    warnings = []
    if unknown := unknown_keys(scoring):
        warnings.append("Scoring keys not modelled (ignored): " + ", ".join(unknown))

    rostered = {p for r in rosters for p in (r.get("players") or [])}
    starters = {p for r in rosters for p in (r.get("starters") or [])}
    views, unmatched = {}, []
    for pid in rostered:
        meta = players.get(pid)
        if not meta:
            continue
        v = _view(pid, meta, cur, prev, schedule, scoring, today)
        if v.nba_id is None:
            unmatched.append(v.name)
        v.starter = pid in starters
        v.advice = advise(v.dist, v.week, v.injury, today, locked.get(norm_name(v.name)))
        views[pid] = v

    # Free agents: anyone with a current-season log who isn't rostered.
    rostered_nba = {v.nba_id for v in views.values() if v.nba_id}
    free_agents = []
    for pid, meta in players.items():
        if pid in rostered or not meta.get("active", True) or not _name(meta):
            continue
        team = TEAM_ALIASES.get(meta.get("team") or "", meta.get("team"))
        nba_id = cur.match(_name(meta), team)
        if nba_id is None or nba_id in rostered_nba:
            continue
        v = _view(pid, meta, cur, prev, schedule, scoring, today, nba_id=nba_id)
        v.advice = advise(v.dist, v.week, v.injury, today)
        free_agents.append(v)
    free_agents.sort(key=lambda v: v.weekly_value, reverse=True)

    return League(league, scoring, rosters, team_names, my_roster_id, views, free_agents,
                  sorted(unmatched), warnings, start, end, today)


def _name(meta: dict) -> str:
    return meta.get("full_name") or f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip()


def _view(pid: str, meta: dict, cur: LogIndex, prev: LogIndex, schedule: list[Game],
          scoring: dict[str, float], today: dt.date, nba_id: int | None = None) -> PlayerView:
    name = _name(meta)
    sleeper_team = TEAM_ALIASES.get(meta.get("team") or "", meta.get("team"))
    if nba_id is None:
        nba_id = cur.match(name, sleeper_team) or prev.match(name, sleeper_team)
    cur_rows = cur.by_player.get(nba_id, []) if nba_id else []
    prev_rows = prev.by_player.get(nba_id, []) if nba_id else []
    team = (cur_rows[0]["TEAM_ABBREVIATION"] if cur_rows else None) or sleeper_team \
        or (prev_rows[0]["TEAM_ABBREVIATION"] if prev_rows else None)

    cur_fp = [fantasy_points(r, scoring) for r in cur_rows]
    prev_fp = [fantasy_points(r, scoring) for r in prev_rows]
    dist = build_dist(cur_fp, prev_fp)

    by_game = {r["GAME_ID"]: r for r in cur_rows}
    week = []
    for g in sorted((g for g in schedule if team in (g.home, g.away)), key=lambda g: g.date):
        row = by_game.get(g.game_id)
        if row is not None:
            week.append(WeekGame(g, row, fantasy_points(row, scoring), "played"))
        else:
            week.append(WeekGame(g, None, None, "dnp" if g.date < today else "upcoming"))

    injury = meta.get("injury_status")
    weekly_value = solve(dist, play_probs(injury, len(week))).expected_value if dist and week else 0.0

    cats: dict[str, float] = defaultdict(float)
    wc, wp = sample_weights(len(cur_rows), len(prev_rows))
    for r, w in zip(cur_rows + prev_rows, wc + wp):
        if w:
            for k, pts in breakdown(r, scoring).items():
                cats[k] += w * pts

    return PlayerView(pid, name, meta.get("fantasy_positions") or [], injury, team, nba_id,
                      cur_rows, prev_rows, dist, week, weekly_value, dict(cats))


@dataclass
class TeamProfile:
    roster_id: int
    name: str
    weekly_projection: float           # sum of the top-N players' expected locked scores
    per_game: float                    # sum of the top-N players' average game
    categories: dict[str, float]       # fantasy points per game by scoring key, top-N players
    core: list[PlayerView]


def team_profiles(lg: League) -> list[TeamProfile]:
    n = lg.starter_slots or 10
    out = []
    for r in lg.rosters:
        views = sorted(lg.roster_views(r), key=lambda v: v.weekly_value, reverse=True)
        by_avg = sorted(views, key=lambda v: v.mean, reverse=True)[:n]
        cats: dict[str, float] = defaultdict(float)
        for v in by_avg:
            for k, pts in v.per_game_cats.items():
                cats[k] += pts
        out.append(TeamProfile(r["roster_id"], lg.team_names[r["roster_id"]],
                               sum(v.weekly_value for v in views[:n]), sum(v.mean for v in by_avg),
                               dict(cats), views[:n]))
    return out


def rank(values: dict[int, float], key: int, reverse: bool = True) -> int:
    ordered = sorted(values.values(), reverse=reverse)
    return ordered.index(values[key]) + 1
