"""Data sources: Sleeper's public API (league, rosters, players) and NBA.com (game logs, schedule).

Everything is plain HTTPS GET with no auth. Responses are cached under
~/.cache/sleeper-lockin (override with SLEEPER_LOCKIN_CACHE) so repeated runs
don't refetch the 5 MB Sleeper player dump or the full-season game logs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SLEEPER = "https://api.sleeper.app/v1"
NBA_GAMELOGS = "https://stats.nba.com/stats/playergamelogs"
NBA_SCHEDULE = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
# stats.nba.com stalls on requests that don't look like they come from nba.com.
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
}

HOUR = 3600


def cache_dir() -> Path:
    d = Path(os.environ.get("SLEEPER_LOCKIN_CACHE", Path.home() / ".cache" / "sleeper-lockin"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             cache_name: str | None = None, max_age: float = 0, timeout: float = 60):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    path = cache_dir() / cache_name if cache_name else None
    if path and path.exists() and time.time() - path.stat().st_mtime < max_age:
        return json.loads(path.read_text())
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "sleeper-lockin/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    data = json.loads(body)
    if path:
        path.write_text(body)
    return data


def season_label(day: dt.date) -> str:
    """NBA season containing `day`, e.g. 2026-10-24 -> '2026-27'. Seasons roll over in August."""
    start = day.year if day.month >= 8 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def previous_season(label: str) -> str:
    start = int(label[:4]) - 1
    return f"{start}-{(start + 1) % 100:02d}"


@dataclass(frozen=True)
class Game:
    game_id: str
    date: dt.date
    home: str
    away: str

    def opponent(self, team: str) -> str:
        return self.away if team == self.home else self.home

    def label(self, team: str) -> str:
        return f"vs {self.away}" if team == self.home else f"@ {self.home}"


class LiveSource:
    """Fetches from Sleeper and NBA.com. The analysis only depends on these methods,
    so tests swap in an offline source with the same interface."""

    def state(self) -> dict:
        return get_json(f"{SLEEPER}/state/nba")

    def user_id(self, username: str) -> str:
        data = get_json(f"{SLEEPER}/user/{username}")
        if not data:
            raise SystemExit(f"Sleeper user '{username}' not found")
        return data["user_id"]

    def league(self, league_id: str) -> dict:
        data = get_json(f"{SLEEPER}/league/{league_id}")
        if not data:
            raise SystemExit(f"Sleeper league {league_id} not found")
        return data

    def rosters(self, league_id: str) -> list[dict]:
        return get_json(f"{SLEEPER}/league/{league_id}/rosters")

    def users(self, league_id: str) -> list[dict]:
        return get_json(f"{SLEEPER}/league/{league_id}/users")

    def players(self) -> dict[str, dict]:
        return get_json(f"{SLEEPER}/players/nba", cache_name="sleeper_players_nba.json", max_age=24 * HOUR)

    def game_logs(self, season: str, current: bool) -> list[dict]:
        """Every player's regular-season game log for `season` as a list of row dicts."""
        data = get_json(NBA_GAMELOGS,
                        params={"Season": season, "SeasonType": "Regular Season", "LeagueID": "00"},
                        headers=NBA_HEADERS, cache_name=f"gamelogs_{season}.json",
                        max_age=HOUR if current else 7 * 24 * HOUR)
        rs = data["resultSets"][0]
        return [dict(zip(rs["headers"], row)) for row in rs["rowSet"]]

    def schedule(self) -> list[Game]:
        data = get_json(NBA_SCHEDULE, headers=NBA_HEADERS, cache_name="schedule.json", max_age=12 * HOUR)
        games = []
        for day in data["leagueSchedule"]["gameDates"]:
            for g in day["games"]:
                gid = g["gameId"]
                # 001 = preseason, 003 = All-Star; neither counts for fantasy.
                if gid[:3] in ("001", "003"):
                    continue
                home, away = g["homeTeam"].get("teamTricode"), g["awayTeam"].get("teamTricode")
                if not home or not away:  # unseeded NBA Cup knockout slots
                    continue
                date = dt.date.fromisoformat(g["gameDateEst"][:10])
                games.append(Game(gid, date, home, away))
        return games
