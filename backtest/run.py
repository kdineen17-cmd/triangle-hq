"""Run every candidate strategy and print an honest comparison table."""
import sys

import numpy as np
import pandas as pd

import strategies as S
from engine import load_wide, run, stats

START = "2021-11-01"          # leaves ~200 bars of warm-up for SMA200
SPLIT = "2024-01-01"          # in-sample before, out-of-sample after
SLIPS = [0.0015, 0.004]       # per side: optimistic vs. realistic small-cap spread

w = load_wide(sys.argv[1] if len(sys.argv) > 1 else "data/daily.csv")
C = w["close"]


def bench_series(start, end=None):
    idx = C.loc[start:end].index
    iwm = C["IWM"].loc[idx]
    uni = C.drop(columns=list(S.BENCH)).loc[idx]
    ew = (1 + uni.pct_change().clip(-0.9, 5).mean(axis=1).fillna(0)).cumprod()
    return {"IWM buy&hold": iwm / iwm.iloc[0] * 2000,
            "Universe equal-weight": ew / ew.iloc[0] * 2000}


configs = [
    ("RSI2", lambda: S.RSI2MeanReversion()),
    ("RSI2 +regime", lambda: S.RSI2MeanReversion(regime=True)),
    ("RSI2 +regime +3ATR stop", lambda: S.RSI2MeanReversion(regime=True, stop_atr=3)),
    ("Breakout", lambda: S.VolumeBreakout()),
    ("Breakout +regime", lambda: S.VolumeBreakout(regime=True)),
    ("Momentum wk", lambda: S.WeeklyMomentum()),
    ("Momentum wk +regime", lambda: S.WeeklyMomentum(regime=True)),
]

fmt = lambda x: "  n/a " if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:6.1%}"
cols = ["total", "cagr", "max_dd", "sharpe", "trades", "win_rate", "avg_trade", "profit_factor"]


def row(name, st):
    parts = [f"{name:28s}"]
    for k in cols:
        v = st.get(k)
        if k in ("sharpe", "profit_factor"):
            parts.append("  n/a " if v is None else f"{v:6.2f}")
        elif k == "trades":
            parts.append("   -  " if v is None else f"{v:6d}")
        else:
            parts.append(fmt(v))
    return " ".join(parts)


header = f"{'':28s} " + " ".join(f"{c[:6]:>6s}" for c in cols)
for label, (a, b) in {"FULL": (START, None), "IN-SAMPLE": (START, "2023-12-31"),
                      "OUT-OF-SAMPLE": (SPLIT, None)}.items():
    for slip in SLIPS:
        print(f"\n=== {label}  {a} -> {b or 'now'}  slippage {slip:.2%}/side ===")
        print(header)
        for name, eq in bench_series(a, b).items():
            print(row(name, stats(eq)))
        for name, mk in configs:
            r = run(w, mk(), slippage=slip, start=a, end=b)
            print(row(name, stats(r.equity, r.trades)))
            if label == "FULL" and slip == SLIPS[-1]:
                yearly = r.equity.resample("YE").last()
                yearly = pd.concat([r.equity.iloc[:1], yearly]).pct_change().dropna()
                print(f"{'':28s} yearly: " + "  ".join(f"{d.year}:{v:+.0%}" for d, v in yearly.items()))
