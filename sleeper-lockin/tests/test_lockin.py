import datetime as dt
import itertools
import math
import random
import unittest

from lockin.analysis import advise, load, norm_name, LogIndex
from lockin.model import Dist, build_dist, sample_weights, solve
from lockin.report import render, to_json
from lockin.scoring import fantasy_points, unknown_keys
from lockin.sources import Game, season_label


def brute_force(dist, probs):
    """Expected value of the best lock rule found by trying every per-game threshold on the support."""
    vals = sorted(set(dist.values)) + [math.inf]
    best = -math.inf
    for ths in itertools.product(vals, repeat=len(probs) - 1):
        ths = list(ths) + [-math.inf]
        best = max(best, _value(dist, probs, ths, 0, 0.0))
    return best


def _value(dist, probs, ths, k, held):
    if k == len(probs):
        return held
    p = probs[k]
    skip = _value(dist, probs, ths, k + 1, held)
    play = sum(w * (x if x >= ths[k] else _value(dist, probs, ths, k + 1, x))
               for x, w in zip(dist.values, dist.weights))
    return (1 - p) * skip + p * play


class ModelTests(unittest.TestCase):
    def test_matches_brute_force(self):
        rng = random.Random(1)
        for _ in range(20):
            vals = tuple(rng.uniform(5, 60) for _ in range(4))
            ws = [rng.random() for _ in vals]
            d = Dist(vals, tuple(w / sum(ws) for w in ws))
            probs = [rng.choice([1.0, 0.95, 0.6, 0.0]) for _ in range(rng.randint(1, 4))]
            self.assertAlmostEqual(solve(d, probs).expected_value, brute_force(d, probs), places=9)

    def test_two_game_threshold_is_mean(self):
        d = Dist((10.0, 30.0), (0.5, 0.5))
        plan = solve(d, [1.0, 1.0])
        self.assertAlmostEqual(plan.thresholds[0], 20.0)
        self.assertEqual(plan.thresholds[1], -math.inf)
        self.assertAlmostEqual(plan.expected_value, 25.0)

    def test_more_games_raise_the_bar(self):
        d = build_dist([float(x) for x in range(10, 50, 3)], [])
        t = solve(d, [1.0] * 4).thresholds
        self.assertTrue(t[0] > t[1] > t[2])

    def test_weights_blend_seasons(self):
        cur, prev = sample_weights(3, 50)
        self.assertAlmostEqual(sum(cur) + sum(prev), 1.0)
        self.assertAlmostEqual(sum(prev), 0.8)
        cur, prev = sample_weights(20, 50)
        self.assertAlmostEqual(sum(prev), 0.0)
        cur, prev = sample_weights(0, 5)
        self.assertAlmostEqual(sum(prev), 1.0)


class ScoringTests(unittest.TestCase):
    ROW = {"PTS": 41, "REB": 12, "AST": 9, "STL": 2, "BLK": 0, "TOV": 4, "FG3M": 5, "FG3A": 11,
           "FGM": 15, "FGA": 27, "FTM": 6, "FTA": 8, "OREB": 2, "DREB": 10, "PF": 3}

    def test_points(self):
        s = {"pts": 1, "reb": 1.2, "ast": 1.5, "stl": 3, "blk": 3, "to": -1, "tpm": 0.5,
             "dd": 1.5, "td": 3, "bonus_pt_40p": 2, "bonus_pt_50p": 2, "fgmi": -0.5}
        expected = 41 + 14.4 + 13.5 + 6 - 4 + 2.5 + 1.5 + 0 + 2 + 0 - 6
        self.assertAlmostEqual(fantasy_points(self.ROW, s), expected)

    def test_unknown(self):
        self.assertEqual(unknown_keys({"pts": 1, "weird": 2, "zero": 0}), ["weird"])


class MatchTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(norm_name("Luka Dončić"), "luka doncic")
        self.assertEqual(norm_name("Jaren Jackson Jr."), "jaren jackson")
        idx = LogIndex([
            {"PLAYER_ID": 1, "PLAYER_NAME": "Nicolas Claxton", "TEAM_ABBREVIATION": "BKN", "GAME_DATE": "2026-10-21"},
            {"PLAYER_ID": 2, "PLAYER_NAME": "Jalen Williams", "TEAM_ABBREVIATION": "OKC", "GAME_DATE": "2026-10-21"},
            {"PLAYER_ID": 3, "PLAYER_NAME": "Jalen Williams", "TEAM_ABBREVIATION": "DEN", "GAME_DATE": "2026-10-21"},
        ])
        self.assertEqual(idx.match("Nic Claxton", "BKN"), 1)
        self.assertEqual(idx.match("Jalen Williams", "OKC"), 2)
        self.assertIsNone(idx.match("Jalen Williams", None))

    def test_season(self):
        self.assertEqual(season_label(dt.date(2026, 10, 24)), "2026-27")
        self.assertEqual(season_label(dt.date(2027, 3, 1)), "2026-27")


def row(pid, name, team, gid, date, pts):
    return {"PLAYER_ID": pid, "PLAYER_NAME": name, "TEAM_ABBREVIATION": team, "GAME_ID": gid,
            "GAME_DATE": f"{date}T00:00:00", "PTS": pts, "REB": 5, "AST": 5}


class FakeSource:
    """Synthetic league for an end-to-end test; not real data."""
    def __init__(self):
        rng = random.Random(7)
        self.prev = [row(pid, name, team, f"p{pid}{i}", dt.date(2026, 3, 1) - dt.timedelta(days=i),
                         rng.randint(lo, hi))
                     for pid, name, team, lo, hi in [(1, "Alpha One", "BOS", 15, 45), (2, "Bravo Two", "NYK", 18, 24),
                                                     (3, "Charlie Three", "MIA", 5, 30), (4, "Delta Four", "LAL", 10, 40)]
                     for i in range(40)]
        self.cur = [row(1, "Alpha One", "BOS", "g1", "2026-10-19", 44),
                    row(2, "Bravo Two", "NYK", "g1", "2026-10-19", 19),
                    row(3, "Charlie Three", "MIA", "g2", "2026-10-20", 30),
                    row(4, "Delta Four", "LAL", "g2", "2026-10-20", 25)]
        self.games = [Game("g1", dt.date(2026, 10, 19), "BOS", "NYK"), Game("g2", dt.date(2026, 10, 20), "MIA", "LAL"),
                      Game("g3", dt.date(2026, 10, 23), "NYK", "BOS"), Game("g4", dt.date(2026, 10, 24), "LAL", "MIA"),
                      Game("g5", dt.date(2026, 10, 25), "BOS", "MIA")]

    def league(self, _): return {"name": "Test League", "roster_positions": ["PG", "SF", "BN"],
                                 "scoring_settings": {"pts": 1, "reb": 1.2, "ast": 1.5}}
    def rosters(self, _): return [{"roster_id": 1, "owner_id": "u1", "players": ["a", "b"], "starters": ["a", "b"]},
                                  {"roster_id": 2, "owner_id": "u2", "players": ["c"], "starters": ["c"]}]
    def users(self, _): return [{"user_id": "u1", "display_name": "me"}, {"user_id": "u2", "display_name": "them"}]
    def user_id(self, _): return "u1"
    def players(self): return {"a": {"full_name": "Alpha One", "team": "BOS", "fantasy_positions": ["PG"]},
                               "b": {"full_name": "Bravo Two", "team": "NY", "fantasy_positions": ["SF"],
                                     "injury_status": "Questionable"},
                               "c": {"full_name": "Charlie Three", "team": "MIA", "fantasy_positions": ["C"]},
                               "d": {"full_name": "Delta Four", "team": "LAL", "fantasy_positions": ["PF"]}}
    def game_logs(self, season, current): return self.cur if season == "2026-27" else self.prev
    def schedule(self): return self.games


class EndToEndTests(unittest.TestCase):
    def test_board(self):
        lg = load(FakeSource(), "L", "me", dt.date(2026, 10, 21))
        a, b = lg.views["a"].advice, lg.views["b"].advice
        self.assertEqual(a.current, 44 + 6 + 7.5)
        self.assertEqual(a.action, "LOCK")
        self.assertIn(b.action, ("LOCK", "HOLD"))
        self.assertEqual(len(a.upcoming_thresholds), 2)
        self.assertEqual([v.name for v in lg.free_agents], ["Delta Four"])
        text = render(lg)
        self.assertIn("LOCK-IN BOARD", text)
        self.assertIn("Strengths", text)
        self.assertEqual(len(to_json(lg)["players"]), 2)

    def test_locked_and_final(self):
        lg = load(FakeSource(), "L", "me", dt.date(2026, 10, 25), locked={"Alpha One": 50})
        self.assertEqual(lg.views["a"].advice.action, "LOCKED")
        self.assertEqual(lg.views["b"].advice.action, "FINAL")


if __name__ == "__main__":
    unittest.main()
