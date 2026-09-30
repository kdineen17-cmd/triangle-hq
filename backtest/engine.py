"""Minimal daily-bar portfolio backtester for long-only swing strategies.

Timing model (no lookahead):
  * Signals are computed from data available at the close of day t.
  * Entries and signal-based exits fill at the OPEN of day t+1, with slippage.
  * Stop-losses are checked intraday: a gap below the stop fills at the open,
    otherwise at the stop price.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# Symbols whose price history contains obvious bad prints (repeated one-day
# spike-and-reverse bars), which would corrupt any backtest that touches them.
BAD_DATA = {"SHAZ", "HYMC"}


def load_wide(path):
    df = pd.read_csv(path, parse_dates=["date"])
    df = df[~df.symbol.isin(BAD_DATA)]
    wide = {c: df.pivot(index="date", columns="symbol", values=c).sort_index()
            for c in ["open", "high", "low", "close", "volume"]}
    return wide


# ---------------------------------------------------------------- indicators
def sma(x, n):
    return x.rolling(n, min_periods=n).mean()


def rsi(close, n):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(high, low, close, n):
    pc = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - pc).abs(), (low - pc).abs()))
    return tr.rolling(n, min_periods=n).mean()


# ---------------------------------------------------------------- engine
@dataclass
class Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: float
    stop: float = np.nan
    high_close: float = np.nan
    bars: int = 0
    exit_flag: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame


def run(w, strategy, start_capital=2000.0, max_positions=5, slippage=0.0015,
        start=None, end=None):
    """strategy must expose:
         prepare(w) -> None                      (precompute indicators)
         entries(t_idx) -> list[(symbol, score)] (signals at close t, higher score first)
         initial_stop(sym, t_idx, fill) -> float or nan
         update(pos, t_idx) -> None              (trail stop / set pos.exit_flag at close t)
    """
    O, H, L, C = w["open"], w["high"], w["low"], w["close"]
    dates = C.index
    strategy.prepare(w)
    i0 = 0 if start is None else dates.searchsorted(pd.Timestamp(start))
    i1 = len(dates) if end is None else dates.searchsorted(pd.Timestamp(end), side="right")

    cash = start_capital
    positions: dict[str, Position] = {}
    pending: list = []
    eq_curve, trades = [], []
    last_px = {}

    def close_pos(pos, px, date, reason):
        nonlocal cash
        fill = px * (1 - slippage)
        cash += pos.shares * fill
        trades.append(dict(symbol=pos.symbol, entry_date=pos.entry_date, exit_date=date,
                           entry=pos.entry_price, exit=fill, bars=pos.bars,
                           ret=fill / pos.entry_price - 1, reason=reason))

    for t in range(i0, i1):
        d = dates[t]
        o, h, l, c = O.iloc[t], H.iloc[t], L.iloc[t], C.iloc[t]

        # 1) signal exits at the open
        for sym in list(positions):
            pos = positions[sym]
            if pos.exit_flag and not np.isnan(o[sym]):
                close_pos(pos, o[sym], d, "signal")
                del positions[sym]

        # 2) entries at the open (sized off yesterday's equity)
        equity_prev = eq_curve[-1][1] if eq_curve else start_capital
        for sym, _score in pending:
            if len(positions) >= max_positions:
                break
            if sym in positions or np.isnan(o[sym]) or o[sym] <= 0:
                continue
            alloc = min(cash, equity_prev / max_positions)
            if alloc < 1:
                break
            fill = o[sym] * (1 + slippage)
            shares = alloc / fill
            cash -= shares * fill
            positions[sym] = Position(sym, d, fill, shares, high_close=fill)
            positions[sym].stop = strategy.initial_stop(sym, t, fill)
        pending = []

        # 3) intraday stops
        for sym in list(positions):
            pos = positions[sym]
            if np.isnan(pos.stop) or np.isnan(l[sym]):
                continue
            if o[sym] <= pos.stop:
                close_pos(pos, o[sym], d, "stop-gap")
                del positions[sym]
            elif l[sym] <= pos.stop:
                close_pos(pos, pos.stop, d, "stop")
                del positions[sym]

        # 4) mark to market, update exits for tomorrow
        mv = 0.0
        for sym, pos in positions.items():
            px = c[sym]
            if np.isnan(px):
                px = last_px.get(sym, pos.entry_price)
            else:
                last_px[sym] = px
            mv += pos.shares * px
            pos.bars += 1
            pos.high_close = max(pos.high_close, px)
            strategy.update(pos, t)
        equity = cash + mv
        eq_curve.append((d, equity))

        # 5) new entry signals for tomorrow's open
        if t + 1 < i1:
            slots = max_positions - sum(1 for p in positions.values() if not p.exit_flag)
            if slots > 0:
                cands = [(s, sc) for s, sc in strategy.entries(t) if s not in positions]
                cands.sort(key=lambda x: -x[1])
                pending = cands[:slots]

    # liquidate at the final close for reporting
    d = dates[i1 - 1]
    for sym, pos in list(positions.items()):
        close_pos(pos, last_px.get(sym, pos.entry_price), d, "end")
    eq = pd.Series(dict(eq_curve))
    return Result(eq, pd.DataFrame(trades))


# ---------------------------------------------------------------- metrics
def stats(eq, trades=None):
    r = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan
    out = dict(total=eq.iloc[-1] / eq.iloc[0] - 1, cagr=cagr, max_dd=dd, sharpe=sharpe)
    if trades is not None and len(trades):
        wins, losses = trades.ret[trades.ret > 0], trades.ret[trades.ret <= 0]
        out.update(trades=len(trades), win_rate=len(wins) / len(trades),
                   avg_trade=trades.ret.mean(), avg_win=wins.mean(), avg_loss=losses.mean(),
                   profit_factor=wins.sum() / -losses.sum() if len(losses) else np.inf,
                   avg_bars=trades.bars.mean())
    return out
