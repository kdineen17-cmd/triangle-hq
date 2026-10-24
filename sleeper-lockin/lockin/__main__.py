"""CLI: python -m lockin --league <id> --user <sleeper username>"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error

from .analysis import load
from .report import render, to_json
from .sources import LiveSource


def parse_locked(items: list[str]) -> dict[str, float]:
    out = {}
    for item in items:
        name, sep, score = item.rpartition("=")
        if not sep or not name:
            raise SystemExit(f"--locked expects NAME=SCORE, got '{item}'")
        out[name.strip()] = float(score)
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="lockin", description="Sleeper NBA lock-in advisor and roster analyzer")
    p.add_argument("--league", required=True, help="Sleeper league ID (from the league URL)")
    p.add_argument("--user", required=True, help="your Sleeper username")
    p.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today(),
                   help="treat this as today (YYYY-MM-DD); default: today")
    p.add_argument("--week-start", type=dt.date.fromisoformat, help="first day of the matchup week (default: Monday)")
    p.add_argument("--week-end", type=dt.date.fromisoformat, help="last day of the matchup week (default: Sunday)")
    p.add_argument("--locked", action="append", default=[], metavar="NAME=SCORE",
                   help="a player you already locked, e.g. --locked 'Jalen Brunson=41.5' (repeatable)")
    p.add_argument("--free-agents", type=int, default=10, help="how many free agents to list (0 to skip)")
    p.add_argument("--json", action="store_true", help="print the lock-in board as JSON")
    args = p.parse_args(argv)

    try:
        lg = load(LiveSource(), args.league, args.user, args.date, args.week_start, args.week_end,
                  parse_locked(args.locked))
    except urllib.error.URLError as e:
        sys.exit(f"Network error: {e}. Needs access to api.sleeper.app, stats.nba.com and cdn.nba.com; stats.nba.com sometimes times out, so retry in a minute.")
    print(json.dumps(to_json(lg), indent=2) if args.json else render(lg, args.free_agents))


if __name__ == "__main__":
    main()
