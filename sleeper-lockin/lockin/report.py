"""Plain-text and JSON rendering of the lock-in board and roster analysis."""
from __future__ import annotations

import math
import statistics

from .analysis import League, PlayerView, TeamProfile, rank, team_profiles

CATEGORY_LABELS = {
    "pts": "Points", "reb": "Rebounds", "ast": "Assists", "stl": "Steals", "blk": "Blocks",
    "to": "Turnovers", "tpm": "3PM", "tpa": "3PA", "fgm": "FGM", "fga": "FGA", "ftm": "FTM",
    "fta": "FTA", "oreb": "Off reb", "dreb": "Def reb", "pf": "Fouls", "fgmi": "FG missed",
    "ftmi": "FT missed", "tpmi": "3P missed", "dd": "Double-doubles", "td": "Triple-doubles",
    "qd": "Quad-doubles",
}
ACTION_ORDER = {"LOCK": 0, "HOLD": 1, "WAIT": 2, "FINAL": 3, "LOCKED": 4, "NO GAMES": 5, "NO DATA": 6}


def fmt(x: float | None, nd: int = 1) -> str:
    if x is None:
        return "-"
    if x == -math.inf:
        return "any"
    return f"{x:.{nd}f}"


def table(headers: list[str], rows: list[list], right: set[int] = frozenset()) -> str:
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]

    def line(r):
        return "  ".join(c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(r, widths)))
    return "\n".join([line(headers), line(["-" * w for w in widths]), *(line(r) for r in cells)])


def week_cell(v: PlayerView) -> str:
    """Compact week strip: scores for played games, the lock bar for upcoming ones."""
    parts, ups = [], iter(v.advice.upcoming_thresholds if v.advice else [])
    for wg in v.week:
        day = wg.game.date.strftime("%a")[:2]
        if wg.status == "played":
            parts.append(f"{day} {wg.score:.1f}")
        elif wg.status == "dnp":
            parts.append(f"{day} DNP")
        else:
            t = next(ups, None)
            parts.append(f"{day} " + ("auto" if t is None or t == -math.inf else f">={t:.0f}"))
    return " | ".join(parts) or "-"


def lock_board(lg: League, views: list[PlayerView]) -> str:
    views = sorted(views, key=lambda v: (not v.starter, ACTION_ORDER.get(v.advice.action, 9),
                                         -v.advice.expected_week))
    rows = []
    for v in views:
        a = v.advice
        rows.append([("* " if v.starter else "  ") + v.name + (f" ({v.injury})" if v.injury else ""),
                     v.team or "-", a.action, fmt(a.current), fmt(a.threshold), fmt(a.hold_value),
                     fmt(a.expected_week), week_cell(v)])
    out = table(["Player", "Tm", "Action", "Now", "Lock if>=", "If held", "E[week]", "This week"],
                rows, right={3, 4, 5, 6})
    return (out + "\n\n* = starter.  'This week' shows played scores; for upcoming games, the score that"
            "\ngame must reach to be worth locking ('auto' = last game, it counts regardless).")


def board_notes(views: list[PlayerView]) -> list[str]:
    notes = []
    for v in views:
        a = v.advice
        if a.action == "LOCK":
            notes.append(f"LOCK {v.name}: {a.current:.1f} beats the {a.hold_value:.1f} you'd expect by waiting.")
        elif a.action == "HOLD":
            notes.append(f"Hold {v.name}: {a.current:.1f} is under the {a.threshold:.1f} bar "
                         f"(waiting is worth ~{a.hold_value:.1f}).")
        if a.note and a.action in ("LOCK", "HOLD", "WAIT"):
            notes.append(f"  {v.name}: {a.note}.")
    return notes


def profile_section(lg: League, profiles: list[TeamProfile]) -> str:
    me = next(p for p in profiles if p.roster_id == lg.my_roster_id)
    weekly = {p.roster_id: p.weekly_projection for p in profiles}
    per_game = {p.roster_id: p.per_game for p in profiles}
    n = len(profiles)
    lines = [f"Team: {me.name}   (top {lg.starter_slots or 10} players by value, {n} teams)",
             f"  Projected locked total this week: {me.weekly_projection:.1f}  "
             f"(rank {rank(weekly, me.roster_id)}/{n}, league avg {statistics.mean(weekly.values()):.1f})",
             f"  Average-game total:               {me.per_game:.1f}  "
             f"(rank {rank(per_game, me.roster_id)}/{n}, league avg {statistics.mean(per_game.values()):.1f})",
             ""]

    keys = sorted({k for p in profiles for k in p.categories}, key=lambda k: -abs(me.categories.get(k, 0)))
    rows, strengths, weaknesses = [], [], []
    for k in keys:
        vals = {p.roster_id: p.categories.get(k, 0.0) for p in profiles}
        r = rank(vals, me.roster_id)
        avg = statistics.mean(vals.values())
        label = CATEGORY_LABELS.get(k, k)
        rows.append([label, fmt(me.categories.get(k, 0.0)), fmt(avg), f"{r}/{n}"])
        if abs(avg) < 0.5:   # too small to matter either way
            continue
        if r <= max(1, n // 3):
            strengths.append(label)
        elif r > n - max(1, n // 3):
            weaknesses.append(label)
    lines.append("Fantasy points per game by source (higher is better; turnovers etc. are negative):")
    lines.append(table(["Category", "You", "Lg avg", "Rank"], rows, right={1, 2, 3}))
    lines.append("")
    lines.append("Strengths:  " + (", ".join(strengths) or "none in the top third"))
    lines.append("Weaknesses: " + (", ".join(weaknesses) or "none in the bottom third"))
    return "\n".join(lines)


def player_section(views: list[PlayerView]) -> str:
    rows = []
    for v in sorted(views, key=lambda v: -v.weekly_value):
        cv = v.stdev / v.mean if v.mean > 0 else 0
        rows.append([v.name, "/".join(v.positions) or "-", len(v.cur_rows), fmt(v.mean), fmt(v.stdev),
                     fmt(cv, 2), fmt(v.dist.quantile(0.9) if v.dist else None), v.games_this_week,
                     fmt(v.weekly_value), f"{v.lock_premium:+.1f}"])
    return (table(["Player", "Pos", "GP", "Avg", "SD", "CV", "P90", "Wk G", "Wk value", "Lock +"],
                  rows, right=set(range(2, 10)))
            + "\n\nGP = games this season (early on, last season fills in).  Wk value = expected locked score"
              "\nthis week.  Lock + = what lock-in adds over an average game: volatile players with more"
              "\ngames gain most.")


def flags(lg: League, views: list[PlayerView]) -> list[str]:
    out = []
    bench = [v for v in views if not v.starter]
    starters = [v for v in views if v.starter]
    if starters and bench:
        worst = min(starters, key=lambda v: v.weekly_value)
        for b in sorted(bench, key=lambda v: -v.weekly_value):
            if b.weekly_value > worst.weekly_value + 1:
                out.append(f"Bench {b.name} ({b.weekly_value:.1f}) projects above starter {worst.name} "
                           f"({worst.weekly_value:.1f}) this week; check position eligibility.")
                break
    for v in views:
        if v.injury:
            out.append(f"{v.name} is listed {v.injury}; the week is discounted for likely missed games.")
        if v.games_this_week <= 2 and v.starter and v.dist:
            out.append(f"{v.name} has only {v.games_this_week} game(s) this week, so less upside to chase.")
    return out


def free_agent_section(lg: League, limit: int = 10) -> str:
    rows = [[v.name, v.team or "-", "/".join(v.positions) or "-", fmt(v.mean), fmt(v.stdev),
             v.games_this_week, fmt(v.weekly_value), v.injury or ""]
            for v in lg.free_agents[:limit]]
    return table(["Player", "Tm", "Pos", "Avg", "SD", "Wk G", "Wk value", "Inj"], rows, right={3, 4, 5, 6})


def render(lg: League, show_fa: int = 10) -> str:
    my = next(r for r in lg.rosters if r["roster_id"] == lg.my_roster_id)
    views = lg.roster_views(my)
    out = [f"{lg.league.get('name', 'League')}  |  week {lg.start:%a %b %d} - {lg.end:%a %b %d}  |  "
           f"as of {lg.today:%a %b %d}", ""]
    out += [f"! {w}" for w in lg.warnings]
    if lg.unmatched:
        out.append("! No NBA game logs matched for: " + ", ".join(lg.unmatched))
    out += ["", "== LOCK-IN BOARD ==", lock_board(lg, views), ""]
    out += board_notes(views)
    out += ["", "== ROSTER STRENGTHS & WEAKNESSES ==", profile_section(lg, team_profiles(lg)), ""]
    out += ["== PLAYERS ==", player_section(views), ""]
    if f := flags(lg, views):
        out += ["== FLAGS =="] + [f"- {x}" for x in f] + [""]
    if show_fa:
        out += ["== TOP FREE AGENTS THIS WEEK ==", free_agent_section(lg, show_fa)]
    return "\n".join(out)


def to_json(lg: League) -> dict:
    my = next(r for r in lg.rosters if r["roster_id"] == lg.my_roster_id)

    def num(x):
        return None if x is None or x == -math.inf else round(x, 2)

    players = []
    for v in lg.roster_views(my):
        a = v.advice
        players.append({
            "name": v.name, "team": v.team, "starter": v.starter, "injury": v.injury,
            "action": a.action, "current": num(a.current), "lock_threshold": num(a.threshold),
            "hold_value": num(a.hold_value), "expected_week": num(a.expected_week),
            "avg": num(v.mean), "stdev": num(v.stdev), "weekly_value": num(v.weekly_value),
            "week": [{"date": wg.game.date.isoformat(), "matchup": wg.game.label(v.team), "status": wg.status,
                      "score": wg.score} for wg in v.week],
            "upcoming_thresholds": [num(t) for t in a.upcoming_thresholds],
        })
    return {"league": lg.league.get("name"), "week_start": lg.start.isoformat(), "week_end": lg.end.isoformat(),
            "today": lg.today.isoformat(), "warnings": lg.warnings, "unmatched": lg.unmatched, "players": players}
