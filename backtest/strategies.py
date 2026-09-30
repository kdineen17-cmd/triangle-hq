"""Candidate swing strategies (2-10 day holds) for a small-cap universe."""
import numpy as np
import pandas as pd

from engine import atr, rsi, sma

BENCH = {"IWM", "SPY"}


class Base:
    min_price = 5.0
    min_dollar_vol = 5e6      # 20-day average dollar volume
    regime = False            # only enter when IWM > its 200-day SMA

    def prepare(self, w):
        C, V = w["close"], w["volume"]
        self.C, self.H, self.L = C, w["high"], w["low"]
        self.tradable = [s for s in C.columns if s not in BENCH]
        self.liquid = (C > self.min_price) & (sma(C * V, 20) > self.min_dollar_vol)
        iwm = C["IWM"]
        self.risk_on = iwm > sma(iwm, 200)
        self.atr = atr(w["high"], w["low"], C, 14)
        self.setup(w)

    def setup(self, w):
        pass

    def ok(self, t):
        return (not self.regime) or bool(self.risk_on.iloc[t])

    def initial_stop(self, sym, t, fill):
        return np.nan

    def _pick(self, mask_row, score_row):
        m = mask_row[self.tradable].fillna(False).astype(bool)
        syms = m.index[m.values]
        return [(s, float(score_row[s])) for s in syms]


class RSI2MeanReversion(Base):
    """Buy a sharp short-term pullback inside a long-term uptrend.
    Entry: close > SMA200 and RSI(2) < 10.  Exit: close > SMA5, or after 7 bars.
    Optional hard stop at entry - stop_atr * ATR14."""
    name = "RSI(2) mean reversion"

    def __init__(self, entry_rsi=10, max_bars=7, stop_atr=None, regime=False):
        self.entry_rsi, self.max_bars, self.stop_atr, self.regime = entry_rsi, max_bars, stop_atr, regime

    def setup(self, w):
        C = self.C
        self.r2 = rsi(C, 2)
        self.sma5 = sma(C, 5)
        self.sig = self.liquid & (C > sma(C, 200)) & (self.r2 < self.entry_rsi)

    def entries(self, t):
        if not self.ok(t):
            return []
        return self._pick(self.sig.iloc[t], -self.r2.iloc[t])

    def initial_stop(self, sym, t, fill):
        if self.stop_atr is None:
            return np.nan
        return fill - self.stop_atr * self.atr[sym].iloc[t - 1]

    def update(self, pos, t):
        c = self.C[pos.symbol].iloc[t]
        if c > self.sma5[pos.symbol].iloc[t] or pos.bars >= self.max_bars:
            pos.exit_flag = True


class VolumeBreakout(Base):
    """Buy a 20-day closing-high breakout on heavy volume in an uptrend.
    Entry: close = 20d high close, volume > 1.5x 50d avg, close > SMA50.
    Exit: 2xATR initial stop, trailing stop 2xATR under highest close, 10-bar time stop."""
    name = "Volume breakout"

    def __init__(self, lookback=20, vol_mult=1.5, stop_atr=2.0, max_bars=10, regime=False):
        self.lookback, self.vol_mult, self.stop_atr, self.max_bars, self.regime = \
            lookback, vol_mult, stop_atr, max_bars, regime

    def setup(self, w):
        C, V = self.C, w["volume"]
        self.vr = V / sma(V, 50)
        hh = C.rolling(self.lookback, min_periods=self.lookback).max()
        self.sig = self.liquid & (C >= hh) & (self.vr > self.vol_mult) & (C > sma(C, 50))

    def entries(self, t):
        if not self.ok(t):
            return []
        return self._pick(self.sig.iloc[t], self.vr.iloc[t])

    def initial_stop(self, sym, t, fill):
        return fill - self.stop_atr * self.atr[sym].iloc[t - 1]

    def update(self, pos, t):
        a = self.atr[pos.symbol].iloc[t]
        if not np.isnan(a):
            pos.stop = max(pos.stop, pos.high_close - self.stop_atr * a)
        if pos.bars >= self.max_bars:
            pos.exit_flag = True


class WeeklyMomentum(Base):
    """Every week hold the strongest names by 20-day return (trend-filtered).
    Rebalance on the last trading day of each week; exit names that drop out."""
    name = "Weekly momentum rotation"

    def __init__(self, lookback=20, top=5, stop_atr=None, regime=False):
        self.lookback, self.top, self.stop_atr, self.regime = lookback, top, stop_atr, regime

    def setup(self, w):
        C = self.C
        self.mom = C / C.shift(self.lookback) - 1
        self.elig = self.liquid & (C > sma(C, 50))
        d = C.index
        wk = pd.Series(d.isocalendar().week.values * 10000 + d.year, index=d)
        self.rebal = (wk != wk.shift(-1)).fillna(True).astype(bool).values  # last bar of each week
        self.current_top = set()

    def _top(self, t):
        cand = self._pick(self.elig.iloc[t], self.mom.iloc[t])
        cand = [x for x in cand if not np.isnan(x[1])]
        cand.sort(key=lambda x: -x[1])
        return cand[:self.top]

    def entries(self, t):
        if not self.rebal[t]:
            return []
        if not self.ok(t):
            self.current_top = set()
            return []
        top = self._top(t)
        self.current_top = {s for s, _ in top}
        return top

    def initial_stop(self, sym, t, fill):
        if self.stop_atr is None:
            return np.nan
        return fill - self.stop_atr * self.atr[sym].iloc[t - 1]

    def update(self, pos, t):
        if self.rebal[t]:
            if not self.ok(t):
                pos.exit_flag = True
            else:
                top = {s for s, _ in self._top(t)}
                if pos.symbol not in top:
                    pos.exit_flag = True
