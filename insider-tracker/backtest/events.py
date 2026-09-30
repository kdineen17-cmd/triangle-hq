"""Point-in-time cluster-buy events.

An event fires on the FILING date on which an issuer first has >= min_insiders
distinct qualifying insiders with open-market purchases (by transaction date)
in the trailing `window_days`, counting only filings public by that date.
After an event the issuer is on cooldown for `window_days` so one cluster is
traded once. The market cap is estimated with the latest shares-outstanding
frame on or before the filing date x the latest insider purchase price.
"""
from __future__ import annotations

import pandas as pd


def cluster_events(purchases: pd.DataFrame, shares_out: pd.DataFrame | None,
                   min_insiders: int = 2, window_days: int = 30,
                   max_market_cap: float = 2e9, min_value: float = 0.0) -> pd.DataFrame:
    win = pd.Timedelta(days=window_days)
    events = []
    for cik, g in purchases.sort_values("filed").groupby("issuer_cik"):
        cooldown_until = pd.Timestamp.min
        for f in g.filed.unique():
            f = pd.Timestamp(f)
            if f < cooldown_until:
                continue
            w = g[(g.filed <= f) & (g.tx_date > f - win) & (g.tx_date <= f)]
            if w.owner_cik.nunique() < min_insiders or w.value.sum() < min_value:
                continue
            last = w.sort_values(["tx_date", "filed"]).iloc[-1]
            events.append(dict(
                signal_date=f, issuer_cik=int(cik), ticker=last.ticker, issuer_name=last.issuer_name,
                n_insiders=w.owner_cik.nunique(), total_value=float(w.value.sum()),
                last_price=float(last.price),
                roles="; ".join(sorted(set(w.role)))))
            cooldown_until = f + win
    ev = pd.DataFrame(events)
    if ev.empty:
        return ev.assign(est_market_cap=pd.Series(dtype=float))

    if shares_out is not None and not shares_out.empty:
        so = shares_out.rename(columns={"cik": "issuer_cik", "as_of": "so_date"}).sort_values("so_date")
        ev = pd.merge_asof(ev.sort_values("signal_date"), so, left_on="signal_date", right_on="so_date",
                           by="issuer_cik", direction="backward",
                           tolerance=pd.Timedelta(days=200))
        ev["est_market_cap"] = ev.shares * ev.last_price
    else:
        ev["est_market_cap"] = float("nan")
    ev["cap_ok"] = ev.est_market_cap < max_market_cap
    return ev.sort_values("signal_date").reset_index(drop=True)
