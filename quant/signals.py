"""The seven signals as known on a day, their percentile ranks, and the score (PLAN Step 3).

Raw values, higher is better (volatility, issuance and asset growth are negated):
  momentum           total return from 12 months to 1 month ago (231 trading days, 90% of them priced)
  low_volatility     minus the annualized std of the last 252 daily total returns (200 of them priced)
  quality            candidates gpa (gross profit / assets), roa, roe (equity > 0), oia (operating income / assets)
  value              candidates bm (book / market), ep (net income / market), sp (sales / market)
  earnings_surprise  latest quarter's EPS change on the same quarter a year earlier, over the std of the last 8
                     such changes (4 at least): the surprise against the company's own trend. Q4 is the fiscal year
                     less the nine months. Missing if the quarter ended over 200 days ago
  issuance           minus the 1-year change in shares outstanding, adjusted for splits
  asset_growth       minus the 1-year change in total assets
Market value is the close times the latest shares outstanding, split-adjusted to the day. Companies with more than
one share class (several cover-page share counts in one filing) get no value or issuance: one class's price times
all classes' shares would be wrong, and a missing signal counts as 50 instead.

Ranks are 0 to 100: fundamentals within sector (quant.sectors.gics11), price signals across the market. A stock
needs 5 of 7 signals to be scored; the score is the mean of the 7 ranks with missing ones at 50.
"""
import numpy as np
import pandas as pd

from quant import lock
from quant.measure import COST, REVENUE
from quant.pit import as_of, first_available

SHARES = ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"]
INCOME = REVENUE + COST + ["GrossProfit", "NetIncomeLoss", "OperatingIncomeLoss"]
BALANCE = ["Assets", "StockholdersEquity"] + SHARES
EPS = "EarningsPerShareDiluted"
TAGS = INCOME + BALANCE + [EPS]

SIGNALS = ["momentum", "quality", "value", "low_volatility", "earnings_surprise", "issuance", "asset_growth"]
PRICE = {"momentum", "low_volatility"}
CANDIDATES = {"quality": ["gpa", "roa", "roe", "oia"], "value": ["bm", "ep", "sp"]}
MIN_SIGNALS, MISSING = 5, 50.0


def shift(day, days):
    return (pd.Timestamp(day) + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def real_split(r):
    """A share split (2, 3:2, 1:10), not one of Yahoo's spin-off factors (1.196, 1.04, 1.998)."""
    return any(abs(x - round(x * 4) / 4) / x < 5e-4 for x in (r, 1 / r)) and abs(r - 1) > 1e-9


def splits_table(long):
    """long: cik, date, split -> the real splits only (cik, date, split)."""
    s = long[long["split"].notna() & (long["split"] != 1) & (long["split"] > 0)]
    return s[s["split"].map(real_split)][["cik", "date", "split"]]


def split_factor(splits, ciks, frm, to):
    """Product of real splits per cik on dates in (frm, to]; frm and to are Series indexed by cik."""
    s = splits[splits["cik"].isin(ciks)]
    s = s.assign(frm=s["cik"].map(frm), to=s["cik"].map(to))
    s = s[(s["date"] > s["frm"]) & (s["date"] <= s["to"])]
    return s.groupby("cik")["split"].prod().reindex(ciks).fillna(1.0)


def multi_class(facts):
    """CIKs that report more than one cover-page share count in a filing."""
    d = facts[facts["tag"] == "EntityCommonStockSharesOutstanding"]
    n = d.groupby(["cik", "accn", "end"]).size()
    return set(n[n > 1].index.get_level_values("cik"))


def eps_quarters(facts):
    """Quarterly diluted EPS (cik, end, value, filed): 10-Q quarters plus Q4 as the year less the nine months."""
    e = facts[facts["tag"] == EPS]
    days = (pd.to_datetime(e["end"]) - pd.to_datetime(e["start"])).dt.days
    q, nine, fy = e[days.between(80, 100)], e[days.between(260, 285)], e[days.between(330, 380)]
    q4 = fy.merge(nine[["cik", "start", "value", "filed"]], on=["cik", "start"], suffixes=("", "_9m"))
    q4 = q4[q4["filed_9m"] <= q4["filed"]].sort_values("filed_9m").assign(value=lambda x: x["value"] - x["value_9m"])
    cols = ["cik", "end", "value", "filed"]
    return pd.concat([q[cols], q4[cols]], ignore_index=True)


def surprise(eq, day):
    e = eq[(eq["filed"] <= day) & (eq["end"] >= shift(day, -4 * 365))]
    e = e.sort_values("filed").drop_duplicates(["cik", "end"], keep="last").sort_values(["cik", "end"])
    g = e.groupby("cik")
    gap = (pd.to_datetime(e["end"]) - pd.to_datetime(g["end"].shift(4))).dt.days
    e = e.assign(diff=(e["value"] - g["value"].shift(4)).where(gap.between(350, 380)))
    last = e.groupby("cik").tail(1).set_index("cik")
    sd = e.groupby("cik").tail(8).groupby("cik")["diff"].agg(lambda s: s.std() if s.count() >= 4 else np.nan)
    out = last["diff"] / sd.reindex(last.index)
    return out.where((last["end"] >= shift(day, -200)) & (sd.reindex(last.index) > 1e-9)).dropna()


def price_signals(tr, day):
    """tr: daily total returns, trading days x cik (NaN where unpriced)."""
    i = tr.index.get_loc(day)
    w = tr.iloc[max(0, i - 251):i + 1]
    vol = (w.std() * np.sqrt(252)).where(w.count() >= 200)
    m = tr.iloc[max(0, i - 251):max(0, i - 20)]
    mom = ((1 + m).prod() - 1).where(m.count() >= 0.9 * 231)
    return pd.DataFrame({"momentum": mom, "low_volatility": -vol, "vol": vol})


def raw(day, ciks, close, tr, facts, eq, splits, multi):
    """Every candidate's raw value for `ciks` as known at `day`'s close. close: raw closes, days x cik."""
    lock.check([day])
    out = price_signals(tr, day).reindex(ciks)
    inc, bs = as_of(facts, day, INCOME, annual=True), as_of(facts, day, BALANCE, annual=False)
    year_ago = shift(day, -365)
    bs1 = as_of(facts, year_ago, BALANCE, annual=False)
    ends = as_of(facts, day, SHARES, annual=False, field="end")
    ends1 = as_of(facts, year_ago, SHARES, annual=False, field="end")
    get = lambda d, t: d.get(t, pd.Series(dtype=float)).reindex(ciks)
    rev, cost = first_available(inc, REVENUE).reindex(ciks), first_available(inc, COST).reindex(ciks)
    gp = get(inc, "GrossProfit").fillna(rev - cost)
    ni, oi, assets, equity = get(inc, "NetIncomeLoss"), get(inc, "OperatingIncomeLoss"), get(bs, "Assets"), \
        get(bs, "StockholdersEquity")
    today = pd.Series(day, ciks)  # both counts in today's shares
    shares = first_available(bs, SHARES).reindex(ciks) * split_factor(
        splits, ciks, first_available(ends, SHARES).reindex(ciks), today)
    shares1 = first_available(bs1, SHARES).reindex(ciks) * split_factor(
        splits, ciks, first_available(ends1, SHARES).reindex(ciks), today)
    single = ~pd.Index(ciks).isin(list(multi))
    mv = (close.loc[day].reindex(ciks) * shares).where(single & (shares > 0))
    assets = assets.where(assets > 0)
    out = out.assign(gpa=gp / assets, roa=ni / assets, roe=(ni / equity).where(equity > 0), oia=oi / assets,
                     bm=equity / mv, ep=ni / mv, sp=rev / mv, mv=mv,
                     earnings_surprise=surprise(eq, day).reindex(ciks),
                     issuance=-(shares / shares1 - 1).where(single),
                     asset_growth=-(assets / get(bs1, "Assets").where(lambda a: a > 0) - 1))
    return out.replace([np.inf, -np.inf], np.nan)


def score(r, sector, choice):
    """r: raw frame for one day (index cik); sector: Series cik -> sector; choice: {"quality": "roa", ...}.
    Returns ranks for the 7 signals plus n (signals present), n60 (ranks above 60) and score (NaN if n < 5)."""
    cols = {s: r[choice.get(s, s)] for s in SIGNALS}
    sec = sector.reindex(r.index).fillna("Unknown")
    ranks = pd.DataFrame({s: v.rank(pct=True) * 100 if s in PRICE else v.groupby(sec).rank(pct=True) * 100
                          for s, v in cols.items()})
    n = ranks.notna().sum(axis=1)
    out = ranks.assign(n=n, n60=(ranks > 60).sum(axis=1))
    return out.assign(score=ranks.fillna(MISSING).mean(axis=1).where(n >= MIN_SIGNALS))
