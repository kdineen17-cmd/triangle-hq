"""Turn an NBA box-score line into fantasy points using a Sleeper league's scoring_settings."""
from __future__ import annotations

import re

# Sleeper scoring key -> stats.nba.com game-log column.
STAT_COLUMNS = {
    "pts": "PTS", "reb": "REB", "ast": "AST", "stl": "STL", "blk": "BLK",
    "to": "TOV", "tpm": "FG3M", "tpa": "FG3A", "fgm": "FGM", "fga": "FGA",
    "ftm": "FTM", "fta": "FTA", "oreb": "OREB", "dreb": "DREB", "pf": "PF",
}
# Missed shots, derived from made/attempted.
MISSED = {"fgmi": ("FGA", "FGM"), "ftmi": ("FTA", "FTM"), "tpmi": ("FG3A", "FG3M")}
# Double-double / triple-double / quadruple-double: number of the five counting cats >= 10.
MULTI_DOUBLE = {"dd": 2, "td": 3, "qd": 4}
DOUBLE_CATS = ("PTS", "REB", "AST", "STL", "BLK")
BONUS_STAT = {"pt": "PTS", "pts": "PTS", "reb": "REB", "ast": "AST", "stl": "STL", "blk": "BLK", "tpm": "FG3M"}
BONUS_RE = re.compile(r"^bonus_([a-z]+)_(\d+)p$")


def _num(row: dict, col: str) -> float:
    v = row.get(col)
    return float(v) if v not in (None, "") else 0.0


def key_value(key: str, row: dict) -> float | None:
    """Raw stat value behind one scoring key, or None if the key isn't understood."""
    if key in STAT_COLUMNS:
        return _num(row, STAT_COLUMNS[key])
    if key in MISSED:
        att, made = MISSED[key]
        return _num(row, att) - _num(row, made)
    if key in MULTI_DOUBLE:
        tens = sum(_num(row, c) >= 10 for c in DOUBLE_CATS)
        return 1.0 if tens >= MULTI_DOUBLE[key] else 0.0
    m = BONUS_RE.match(key)
    if m and m.group(1) in BONUS_STAT:
        return 1.0 if _num(row, BONUS_STAT[m.group(1)]) >= int(m.group(2)) else 0.0
    return None


def unknown_keys(scoring: dict[str, float]) -> list[str]:
    """Scoring keys with a nonzero weight that this module can't compute."""
    return sorted(k for k, w in scoring.items() if w and key_value(k, {}) is None)


def fantasy_points(row: dict, scoring: dict[str, float]) -> float:
    total = 0.0
    for key, weight in scoring.items():
        if not weight:
            continue
        v = key_value(key, row)
        if v is not None:
            total += weight * v
    return round(total, 2)


def breakdown(row: dict, scoring: dict[str, float]) -> dict[str, float]:
    """Fantasy points contributed by each scoring key."""
    out = {}
    for key, weight in scoring.items():
        v = key_value(key, row) if weight else None
        if v:
            out[key] = weight * v
    return out
