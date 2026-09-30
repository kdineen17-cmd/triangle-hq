# Small-cap swing strategy backtests

Daily-bar backtests of short-term (2–10 day) long-only strategies on a universe of
~200 liquid US small caps ($300M–$4B market cap, >1.5M avg daily volume, price > $5),
using Robinhood historical data (2021 → present).

## Files
- `build_dataset.py` – converts Robinhood `get_equity_historicals` JSON dumps to `data/daily.csv`
- `engine.py` – portfolio simulator (signals at close, fills at next open + slippage, intraday stops, max 5 equal-weight positions)
- `strategies.py` – RSI(2) mean reversion, volume breakout, weekly momentum rotation
- `run.py` – full / in-sample (2021-11→2023) / out-of-sample (2024→) comparison at 0.15% and 0.40% slippage per side

`python run.py data/daily.csv`

## Known biases
- **Survivorship / selection bias**: the universe is today's small caps, so it excludes names
  that went bankrupt or shrank and includes names that grew into it. This flatters every result;
  compare strategies against the "Universe equal-weight" row, which carries the same bias.
- SHAZ and HYMC are excluded for bad price prints.
