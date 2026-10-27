"""Signals, scoring and the backtest simulator on small made-up data."""
import numpy as np
import pandas as pd

from quant import backtest as bt
from quant import signals as sg


def test_real_splits_and_spin_off_factors():
    assert all(sg.real_split(r) for r in [2.0, 1.5, 3.0, 0.5, 1 / 3, 0.1, 7.0])
    assert not any(sg.real_split(r) for r in [1.0, 1.196, 1.04, 1.0662, 1.324, 1.998])


def test_split_factor_counts_only_splits_inside_the_window():
    splits = pd.DataFrame({"cik": [1, 1, 2], "date": ["2015-06-01", "2016-06-01", "2015-06-01"], "split": [2.0, 3.0, 4.0]})
    f = sg.split_factor(splits, [1, 2, 3], pd.Series({1: "2015-01-01", 2: "2015-07-01", 3: None}),
                        pd.Series({1: "2016-01-01", 2: "2016-01-01", 3: "2016-01-01"}))
    assert f.to_dict() == {1: 2.0, 2: 1.0, 3: 1.0}


def eps(cik, start, end, value, filed):
    return {"cik": cik, "tag": sg.EPS, "start": start, "end": end, "value": value, "filed": filed}


def test_q4_is_the_year_less_nine_months_and_surprise_uses_own_trend():
    rows = []
    for y in range(2012, 2017):
        rows += [eps(1, f"{y}-01-01", f"{y}-03-31", 1.0 + 0.1 * (y - 2012), f"{y}-05-01"),
                 eps(1, f"{y}-04-01", f"{y}-06-30", 1.0 + 0.1 * (y - 2012), f"{y}-08-01"),
                 eps(1, f"{y}-07-01", f"{y}-09-30", 1.0 + 0.1 * (y - 2012), f"{y}-11-01"),
                 eps(1, f"{y}-01-01", f"{y}-09-30", 3.0 + 0.3 * (y - 2012), f"{y}-11-01"),
                 eps(1, f"{y}-01-01", f"{y}-12-31", 4.0 + 0.4 * (y - 2012) + (1.0 if y == 2016 else 0), f"{y + 1}-02-15")]
    eq = sg.eps_quarters(pd.DataFrame(rows))
    q4 = eq[eq["end"] == "2016-12-31"]["value"].iloc[0]
    assert abs(q4 - 2.4) < 1e-9                       # 4.0 + 1.6 + 1.0 - (3.0 + 1.2)
    before = sg.surprise(eq, "2017-02-14")            # the 2016 10-K isn't filed yet
    after = sg.surprise(eq, "2017-02-15")
    assert 1 not in before.index                      # steady growth: zero spread, no surprise defined
    assert after[1] > 0                               # the jump in Q4 shows up on its filing day


def test_score_needs_five_signals_and_counts_missing_as_50():
    r = pd.DataFrame({s: [1.0, 2.0, 3.0] for s in ["momentum", "low_volatility", "roa", "bm", "earnings_surprise",
                                                   "issuance", "asset_growth"]}, index=[1, 2, 3])
    r.loc[3, ["roa", "bm", "earnings_surprise"]] = np.nan
    s = sg.score(r, pd.Series("Tech", index=r.index), {"quality": "roa", "value": "bm"})
    assert s.loc[2, "n"] == 7 and s.loc[3, "n"] == 4 and np.isnan(s.loc[3, "score"])
    assert s.loc[1, "score"] < s.loc[2, "score"]


def test_simulate_buys_at_next_open_and_exits_a_failed_company_at_a_penalty():
    days = pd.bdate_range("2013-01-02", "2013-06-28").strftime("%Y-%m-%d")
    ciks = [1, 2]
    tr = pd.DataFrame(0.001, index=days, columns=ciks)
    tro = tr.copy()
    close = pd.DataFrame(10.0, index=days, columns=ciks)
    last = days[100]
    for f in (tr, tro, close):
        f.loc[f.index > last, 2] = np.nan
    d = {"tr": tr, "tro": tro, "close": close, "ended": pd.Series({2: last}), "failed": {2}, "blocked": set()}
    sc = pd.DataFrame([{"date": t, "cik": c, "score": 90.0, "n60": 7, "sector": "S", "vol": 0.2}
                       for t in days for c in ciks if pd.notna(close.at[t, c])])
    spy = pd.DataFrame({"date": days, "close": 100.0, "tr": 0.0})
    vix = pd.DataFrame({"date": days, "vix": 15.0})
    p = bt.BASE | {"filter": "none"}
    nav, stats = bt.simulate(d, sc, {"SPY": spy, "RSP": spy}, vix, p)
    assert stats["exits_failed"] == 1
    assert nav.iloc[0] == 1.0                         # decided at the close, nothing bought yet
    assert nav.loc[last] < nav.loc[days[99]]          # the 30% penalty lands on the last day
