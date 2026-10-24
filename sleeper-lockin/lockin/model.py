"""Score distributions and the optimal lock-in rule.

The lock-in format is an optimal-stopping problem. Each week a player has games
1..n. After each game you either lock that score (final for the week) or let it
go; an unlocked score is replaced by the next game the player actually plays,
and if you never lock, the last game played counts.

Let C_i(s) be the expected final score when you hold an unlocked score s just
before game i, and p_i the chance the player plays game i. A skipped game
leaves s in place, so

    C_{n+1}(s) = s
    C_i(s)     = (1 - p_i) * C_{i+1}(s) + p_i * E_x[ max(x, C_{i+1}(x)) ]

By induction C_i(s) = alpha_i * s + beta_i is linear, with
alpha_i = prod_{j>=i} (1 - p_j). After scoring x in a game, lock exactly when
x >= C_next(x), i.e. x >= beta / (1 - alpha) for the remaining games.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Dist:
    """Discrete fantasy-point distribution: parallel lists of values and weights summing to 1."""
    values: tuple[float, ...]
    weights: tuple[float, ...]

    @property
    def mean(self) -> float:
        return sum(v * w for v, w in zip(self.values, self.weights))

    @property
    def stdev(self) -> float:
        m = self.mean
        return math.sqrt(sum(w * (v - m) ** 2 for v, w in zip(self.values, self.weights)))

    def quantile(self, q: float) -> float:
        acc = 0.0
        for v, w in sorted(zip(self.values, self.weights)):
            acc += w
            if acc >= q - 1e-12:
                return v
        return max(self.values)

    def prob_at_least(self, x: float) -> float:
        return sum(w for v, w in zip(self.values, self.weights) if v >= x)

    def expect_max_linear(self, alpha: float, beta: float) -> float:
        """E[max(X, alpha*X + beta)]."""
        return sum(w * max(v, alpha * v + beta) for v, w in zip(self.values, self.weights))


def sample_weights(n_current: int, n_previous: int, halflife: float = 10.0,
                   prior_games: float = 15.0) -> tuple[list[float], list[float]]:
    """Weights for this season's and last season's games (each list newest first), summing to 1.

    Games decay with a `halflife` measured in games. Last season fades out
    linearly as this season accumulates `prior_games` games.
    """
    decay = 0.5 ** (1.0 / halflife)
    cur = [decay ** i for i in range(n_current)]
    prev = [decay ** i for i in range(n_previous)]
    prev_share = max(0.0, 1.0 - n_current / prior_games) if prev else 0.0
    if not cur:
        prev_share = 1.0 if prev else 0.0
    cur_total, prev_total = sum(cur), sum(prev)
    cur = [w * (1 - prev_share) / cur_total for w in cur] if cur else []
    prev = [w * prev_share / prev_total for w in prev] if prev else []
    return cur, prev


def build_dist(current: list[float], previous: list[float], **kw) -> Dist | None:
    """Recency-weighted empirical distribution of per-game fantasy points.

    `current` and `previous` are this season's and last season's scores, newest first.
    """
    if not current and not previous:
        return None
    wc, wp = sample_weights(len(current), len(previous), **kw)
    pairs = [(v, w) for v, w in zip(current + previous, wc + wp) if w > 0]
    return Dist(tuple(v for v, _ in pairs), tuple(w for _, w in pairs))


@dataclass(frozen=True)
class Plan:
    """Lock-in solution for a sequence of games.

    thresholds[k] is the score that game k must reach to be worth locking (the
    last game's threshold is -inf: it counts automatically). alpha/beta[k]
    describe C_k, the value of holding an unlocked score before game k;
    expected_value is the week's expected locked score from an empty start.
    """
    thresholds: tuple[float, ...]
    alphas: tuple[float, ...]
    betas: tuple[float, ...]

    @property
    def expected_value(self) -> float:
        return self.betas[0]

    def hold_value(self, k: int, score: float) -> float:
        """Expected final score if you hold `score` unlocked before game k."""
        return self.alphas[k] * score + self.betas[k]


def solve(dist: Dist, play_probs: list[float]) -> Plan:
    """Backward induction over the remaining games (play_probs[k] = P(plays game k))."""
    n = len(play_probs)
    alphas = [1.0] * (n + 1)
    betas = [0.0] * (n + 1)
    thresholds = [0.0] * n
    for k in range(n - 1, -1, -1):
        a, b = alphas[k + 1], betas[k + 1]
        thresholds[k] = b / (1 - a) if a < 1 else -math.inf
        p = play_probs[k]
        alphas[k] = (1 - p) * a
        betas[k] = (1 - p) * b + p * dist.expect_max_linear(a, b)
    return Plan(tuple(thresholds), tuple(alphas), tuple(betas))
