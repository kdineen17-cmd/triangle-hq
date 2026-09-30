"""Backtest insider cluster buys.

Steps (each cached, re-runnable):
  python backtest/run_backtest.py download   # SEC quarterly datasets + share counts
  python backtest/run_backtest.py events     # build events.csv, write tickers.txt
  python backtest/run_backtest.py prices     # daily bars for tickers.txt via Robinhood MCP
  python backtest/run_backtest.py report     # event study + portfolio simulation

Timing: filings can land after the close, so every trade enters at the OPEN of
the first trading day AFTER the filing date. Costs are charged per side.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "backtest"))   # reuse the portfolio engine

import sec_data  # noqa: E402
from events import cluster_events  # noqa: E402

DATA = ROOT / "data" / "backtest"
HOLDS = [5, 10, 21, 63]
COSTS = [0.0015, 0.004]      # per side: optimistic vs. realistic small-cap spread
BENCH = "IWM"


# --------------------------------------------------------------------- prices
def load_prices(path: Path) -> dict[str, pd.DataFrame]:
    df = pd.read_csv(path, parse_dates=["date"])
    return {c: df.pivot_table(index="date", columns="symbol", values=c).sort_index()
            for c in ["open", "high", "low", "close", "volume"]}


async def fetch_prices_robinhood(symbols: list[str], start: str, out: Path) -> None:
    """Daily bars through the Robinhood Agentic MCP server (uses src/trader.py's OAuth)."""
    from src.main import load_configs
    from src.trader import RobinhoodAgenticTrader
    _, rcfg = load_configs()
    rows = []
    async with RobinhoodAgenticTrader(rcfg) as t:
        for i in range(0, len(symbols), 10):
            batch = symbols[i:i + 10]
            try:
                res = await t._call("get_equity_historicals",
                                    {"symbols": batch, "start_time": f"{start}T00:00:00Z", "interval": "day"})
            except Exception as e:
                print(f"  {batch}: {e}")
                continue
            for r in res.get("data", {}).get("results", []):
                for b in r.get("bars", []):
                    if not b.get("interpolated"):
                        rows.append((b["begins_at"][:10], r["symbol"], b["open_price"], b["high_price"],
                                     b["low_price"], b["close_price"], b["volume"]))
            print(f"  fetched {min(i + 10, len(symbols))}/{len(symbols)}")
    pd.DataFrame(rows, columns=["date", "symbol", "open", "high", "low", "close", "volume"]) \
        .drop_duplicates(["date", "symbol"]).to_csv(out, index=False)


# --------------------------------------------------------------------- event study
def event_returns(ev: pd.DataFrame, w: dict[str, pd.DataFrame], cost: float) -> pd.DataFrame:
    O, C = w["open"], w["close"]
    dates = C.index
    out = []
    for e in ev.itertuples():
        if e.ticker not in C.columns:
            continue
        i = dates.searchsorted(e.signal_date, side="right")      # first trading day after filing
        if i >= len(dates):
            continue
        entry = O[e.ticker].iloc[i]
        b_entry = O[BENCH].iloc[i]
        if not np.isfinite(entry) or entry <= 0:
            continue
        row = dict(signal_date=e.signal_date, ticker=e.ticker, entry_date=dates[i])
        for h in HOLDS:
            j = i + h - 1
            if j >= len(dates) or not np.isfinite(C[e.ticker].iloc[j]):
                row[f"r{h}"] = row[f"x{h}"] = np.nan
                continue
            r = C[e.ticker].iloc[j] * (1 - cost) / (entry * (1 + cost)) - 1
            b = C[BENCH].iloc[j] / b_entry - 1
            row[f"r{h}"], row[f"x{h}"] = r, r - b
        out.append(row)
    return pd.DataFrame(out)


def summarize(er: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for h in HOLDS:
        r, x = er[f"r{h}"].dropna(), er[f"x{h}"].dropna()
        if len(r) == 0:
            continue
        rows.append(dict(hold_days=h, n=len(r), mean=r.mean(), median=r.median(), win_rate=(r > 0).mean(),
                         mean_excess_vs_IWM=x.mean(), beat_IWM=(x > 0).mean(),
                         t_stat_excess=x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 else np.nan))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- portfolio
class ClusterStrategy:
    """Buy at next open after each filing-date signal, hold `hold` bars (engine interface)."""

    def __init__(self, ev: pd.DataFrame, hold: int):
        self.ev, self.hold = ev, hold

    def prepare(self, w):
        dates = w["close"].index
        self.by_t: dict[int, list] = {}
        for e in self.ev.itertuples():
            t = dates.searchsorted(e.signal_date, side="right") - 1   # last session on/before filing
            if 0 <= t < len(dates) and e.ticker in w["close"].columns:
                self.by_t.setdefault(t, []).append((e.ticker, e.total_value))

    def entries(self, t):
        return self.by_t.get(t, [])

    def initial_stop(self, sym, t, fill):
        return np.nan

    def update(self, pos, t):
        if pos.bars >= self.hold:
            pos.exit_flag = True


# --------------------------------------------------------------------- main
def main():
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser()
    p.add_argument("step", choices=["download", "events", "prices", "report"])
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--prices", type=Path, default=DATA / "prices.csv")
    p.add_argument("--min-insiders", type=int, default=int(os.getenv("MIN_INSIDERS", "2")))
    p.add_argument("--window", type=int, default=int(os.getenv("CLUSTER_WINDOW_DAYS", "30")))
    p.add_argument("--max-cap", type=float, default=float(os.getenv("MAX_MARKET_CAP_USD", "2e9")))
    p.add_argument("--min-value", type=float, default=float(os.getenv("MIN_CLUSTER_VALUE_USD", "0")))
    a = p.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    sec_dir = DATA / "sec"

    if a.step == "download":
        sec_data.download(a.start, a.end, sec_dir, os.getenv("SEC_USER_AGENT", ""))

    elif a.step == "events":
        pur = sec_data.load_purchases(sorted(sec_dir.glob("*_form345.zip")))
        pur.to_csv(DATA / "purchases.csv", index=False)
        ev = cluster_events(pur, sec_data.load_shares_outstanding(sec_dir),
                            a.min_insiders, a.window, a.max_cap, a.min_value)
        ev.to_csv(DATA / "events.csv", index=False)
        tickers = sorted(set(ev[ev.cap_ok].ticker)) + [BENCH]
        (DATA / "tickers.txt").write_text("\n".join(tickers))
        print(f"{len(pur):,} qualifying purchases -> {len(ev):,} cluster events, "
              f"{int(ev.cap_ok.sum()):,} under ${a.max_cap / 1e9:.1f}B "
              f"({int(ev.est_market_cap.isna().sum()):,} with unknown cap dropped); "
              f"{len(tickers) - 1:,} tickers -> tickers.txt")

    elif a.step == "prices":
        syms = (DATA / "tickers.txt").read_text().split()
        asyncio.run(fetch_prices_robinhood(syms, a.start, a.prices))

    else:
        ev = pd.read_csv(DATA / "events.csv", parse_dates=["signal_date"])
        ev = ev[ev.cap_ok.astype(bool) & (ev.signal_date >= a.start)]
        w = load_prices(a.prices)
        cov = ev.ticker.isin(w["close"].columns).mean()
        print(f"{len(ev):,} events; price data for {cov:.0%} of them "
              f"(missing = mostly delisted/renamed -> survivorship bias flatters results)\n")
        for cost in COSTS:
            er = event_returns(ev, w, cost)
            print(f"=== event study, cost {cost:.2%}/side, entry next open after filing ===")
            print(summarize(er).to_string(index=False, float_format=lambda v: f"{v:.3f}"))
            er["year"] = er.entry_date.dt.year
            print("  21-day mean excess vs IWM by year: " + "  ".join(
                f"{y}:{g.x21.mean():+.1%}(n={g.x21.notna().sum()})" for y, g in er.groupby("year")))
            print()
        from engine import run, stats
        print("=== portfolio: $2,000, max 5 equal-weight positions, hold 21 days ===")
        bench = w["close"][BENCH].loc[a.start:].dropna()
        print(f"  IWM buy&hold: {bench.iloc[-1] / bench.iloc[0] - 1:+.1%}")
        for cost in COSTS:
            r = run(w, ClusterStrategy(ev, 21), slippage=cost, start=a.start)
            s = stats(r.equity, r.trades)
            print(f"  cost {cost:.2%}: total {s['total']:+.1%}  CAGR {s['cagr']:+.1%}  maxDD {s['max_dd']:.1%}  "
                  f"Sharpe {s['sharpe']:.2f}  trades {s.get('trades', 0)}  win {s.get('win_rate', 0):.0%}")


if __name__ == "__main__":
    main()
