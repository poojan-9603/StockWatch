"""The leader-dip study's event logic on a synthetic stock: one dip per threshold crossed, re-armed only after a
recovery, and forward returns against SPY that stop at the data's end (the lock)."""
import numpy as np
import pandas as pd

from quant import backtest as bt, dips


def test_dips_fire_once_per_threshold_and_forward_returns():
    days = list(pd.bdate_range("2012-01-02", periods=900).strftime("%Y-%m-%d"))
    px = np.r_[np.linspace(100, 150, 400), np.linspace(150, 109.5, 40), np.linspace(109.5, 160, 460)]  # -27%, back
    close = pd.DataFrame({1: px}, index=days)
    tr = close.pct_change()
    segs = pd.DataFrame({"ticker": ["A"], "cik": [1], "start": ["2010-01-01"], "end": [None]})
    d = {"close": close, "tr": tr, "segs": segs, "blocked": set(), "failed": set()}
    tri = dips.tri_of(tr, close)
    flags = {m: pd.DataFrame({"leader": [True], "strong": [True], "tech": [False]}, index=[1])
             for m in bt.rebalance_days(days, "monthly", "2013-01-01")}
    ev, base = dips.events(d, tri, flags)
    assert sorted(ev["dip"]) == [0.15, 0.20, 0.25]  # a 27% fall crosses three thresholds, not 30%
    assert (ev.groupby("dip").size() == 1).all()  # once each: no re-arm until back within half the dip
    spy = pd.Series(1.0, index=days)  # flat SPY: excess is the stock's own return
    f = dips.forward(d, tri, spy, ev)
    i = days.index(ev["date"].iloc[0])
    assert abs(f["excess_3m"].iloc[0] - (px[i + 63] / px[i] - 1)) < 1e-9
    late = dips.forward(d, tri, spy, ev.assign(date=days[-100]))  # a window past the data's end isn't counted
    assert late["excess_3m"].notna().all() and late["excess_6m"].isna().all()
    assert len(base) and base["strong"].all()
