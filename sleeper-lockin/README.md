# Sleeper NBA lock-in advisor

Analyzes your Sleeper NBA roster and tells you, game by game, whether a player's score is
worth **locking in** for the week.

It's built for the lock-in format. Each player's games play out in order. After each game you
lock that score, which is final for the week, or let it go. If you never lock, the player's
last game counts.

```bash
cd sleeper-lockin
python3 -m lockin --league <LEAGUE_ID> --user <your sleeper username>
```

It needs Python 3.11 or newer and no third-party packages. The league ID is the long number
in your league's URL (`sleeper.com/leagues/<LEAGUE_ID>/...`).

## What you get

1. **Lock-in board.** Each player on your roster gets one of these actions:
   - `LOCK`: the latest score beats what you'd expect by waiting. Lock it now.
   - `HOLD`: the score is under the bar. Let it go and take your chances on the remaining games.
   - `WAIT`: no game played yet this week. The strip shows the score each upcoming game must
     reach to be worth locking (for example `Fr >=42`).
   - `FINAL`: the player's last game of the week has been played, so it counts.
   - `LOCKED`: you told the tool you already locked this player (see `--locked`).

   `Lock if>=` is the bar for the score on the table. `If held` is the expected final score
   if you pass on it. `E[week]` is the expected locked score.
2. **Roster strengths and weaknesses.** Your projected weekly total and your fantasy points
   per game from each scoring category (points, rebounds, assists, turnovers and so on). The
   tool ranks each against every other team in the league. Categories in the top third are
   strengths; the bottom third are weaknesses.
3. **Players.** Average, spread (SD and CV), 90th percentile, games this week, the week's
   expected locked score, and the **lock premium**: how much the format adds over an average
   game. Volatile players with four games gain the most, steady players with two the least.
   Lock premium is worth weighing in trades and pickups.
4. **Flags.** A bench player projecting above a starter, injured players, and starters with
   light schedules.
5. **Top free agents** ranked by expected locked score this week.

### Options

| Flag | Meaning |
|---|---|
| `--locked "Name=41.5"` | A player you already locked, so the tool stops advising on them (repeatable). |
| `--date YYYY-MM-DD` | Pretend today is this date. Useful for replaying a past week. |
| `--week-start` / `--week-end` | Override the matchup week. The default is Monday to Sunday. |
| `--free-agents N` | How many free agents to list (`0` hides them). |
| `--json` | Print the lock-in board as JSON. |

Run it after each night's games go final. A game shown for today counts as upcoming until
its box score appears.

## How the lock decision works

This is an optimal-stopping problem, solved exactly by backward induction (see `lockin/model.py`):

- **Score distribution.** It uses the player's per-game fantasy points under *your league's*
  `scoring_settings`, weighted toward recent games (10-game half-life). Early in the season,
  last season's games fill in and fade out over the first 15 games.
- **The bar.** With one game left, lock any score above that game's expected value. With more
  games left, the bar is higher, because the remaining games include the option to lock
  later. Lock when `score >= value of waiting`.
- **Availability.** Each remaining game has a chance the player sits, based on Sleeper's
  injury status: Out (0% next game), Doubtful (25%), Questionable (60%), healthy (95%). The
  model assumes that if the player sits, the unlocked score stays on the table.
- **Expected locked score per week** is the value of following this rule from Monday on. It
  drives the weekly projections and the free-agent ranking.

## Data sources

- Sleeper's public API (`api.sleeper.app`) supplies league, rosters, scoring settings,
  players and injury status.
- NBA.com supplies game logs (`stats.nba.com` playergamelogs, one call per season) and the
  schedule (`cdn.nba.com`).

Responses are cached in `~/.cache/sleeper-lockin` (override with `SLEEPER_LOCKIN_CACHE`).
Game logs refresh hourly and the Sleeper player list daily. stats.nba.com can be slow, so if
a run times out, retry it.

## Limits

- The model doesn't adjust for opponent, minutes trends, back-to-backs or blowout risk. It
  works from the player's own score history.
- Locks you've made in the Sleeper app aren't readable through the public API. Pass them
  with `--locked`.
- The model maximizes expected points. It doesn't adjust for your matchup score; when you're
  far behind late in a week, chasing variance can beat locking.
- If your league scores a stat this tool doesn't model, it prints `! Scoring keys not
  modelled` and ignores that key. Each key it does handle has its own lookup in
  `lockin/scoring.py`.
- Player matching goes from Sleeper names to NBA.com names. Any player it can't match is
  listed at the top of the report.

## Tests

```bash
python3 -m unittest tests/test_lockin.py
```

The test suite checks the solver against brute force over every possible threshold rule,
covers scoring and name matching, and runs end to end on a synthetic league.
