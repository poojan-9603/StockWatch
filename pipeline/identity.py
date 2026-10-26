"""Check that each validated price stretch belongs to the company it's assigned to, using SEC data only.

Dates can't prove identity: vendors differ on whether a series follows the ticker or the company, so where an
acquirer took the target's ticker (ACE took Chubb's CB) one series can end up assigned to two companies. Three
checks, no extra API calls:

  shared     the same series is assigned to two different companies over the same dates (the same vendor
             symbol, or the same ticker from two vendors with matching closes). The one that passes the other
             checks keeps it; the rest fail.
  dividends  the vendor's dividends, summed by fiscal year (ex-dates), against the company's XBRL
             CommonStockDividendsPerShareDeclared for that year as first reported (pre-later-split, like the
             raw vendor prices). A year passes at 0.7 to 1.3.
  float      price on the public-float date x XBRL shares outstanding, against XBRL EntityPublicFloat. Float
             excludes insiders, so it sits below market value: a year passes at 0.2 to 1.25.
A check needs at least 2 years to vote; it passes at 60% or more good years and fails at 20% or less.

Verdict: fail if any check fails, pass if at least one passes, otherwise unverifiable.
"""
from collections import defaultdict

import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds

DIV_TAG = "CommonStockDividendsPerShareDeclared"
SHARE_TAGS = ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"]


def vote(goods):
    if len(goods) < 2:
        return "n/a"
    share = sum(goods) / len(goods)
    return "pass" if share >= 0.6 else "fail" if share <= 0.2 else "n/a"


def first_reported(f):
    """One value per (cik, start, end): the earliest filing's."""
    return f.sort_values("filed").drop_duplicates(["cik", "start", "end"])


def dividend_check(px, divs):
    """px: one stretch's prices (date, dividend). divs: XBRL annual dividends per share (start, end, value)."""
    goods = []
    for d in divs.itertuples():
        if d.value <= 0 or d.start < px["date"].min() or d.end > px["date"].max():
            continue
        paid = px.loc[(px["date"] >= d.start) & (px["date"] <= d.end), "dividend"].sum()
        goods.append(0.7 <= paid / d.value <= 1.3)
    return vote(goods), len(goods)


def float_check(px, floats, shares):
    """floats: XBRL public float (end, value). shares: shares outstanding (end, value), classes summed."""
    goods = []
    closes = px.set_index("date")["close"]
    for f in floats.itertuples():
        before = closes[(closes.index <= f.end) & (closes.index >= (pd.Timestamp(f.end) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"))]
        if before.empty or shares.empty or f.value <= 0:
            continue
        gap = (pd.to_datetime(shares["end"]) - pd.Timestamp(f.end)).dt.days.abs()
        if gap.min() > 200:
            continue
        s = shares.loc[gap.idxmin(), "value"]
        goods.append(0.2 <= f.value / (before.iloc[-1] * s) <= 1.25)
    return vote(goods), len(goods)


def shared_series(pm, closes):
    """Pairs of stretches for different companies that are the same series over overlapping dates.
    closes: {(source, symbol): Series of close by date}."""
    out = set()
    rows = list(pm.itertuples())
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a.company == b.company or a.symbol != b.symbol:
                continue
            lo, hi = max(a.frm, b.frm), min(a.to_, b.to_)
            if lo >= hi:
                continue
            if (a.source, a.symbol) == (b.source, b.symbol):
                out |= {a.Index, b.Index}
                continue
            x = closes.get((a.source, a.symbol), pd.Series(dtype=float))
            y = closes.get((b.source, b.symbol), pd.Series(dtype=float))
            j = pd.concat([x[(x.index >= lo) & (x.index < hi)], y], axis=1, join="inner").dropna()
            if len(j) >= 20 and ((j.iloc[:, 0] / j.iloc[:, 1] - 1).abs() < 0.005).mean() >= 0.9:
                out |= {a.Index, b.Index}
    return out


def check(pm, prices_dir, facts_path, segs, last_day):
    """Verdict per price_map row: pm plus columns company, shared, dividends, dividend_years, float, float_years,
    verdict. company is the set of CIKs (as text) the ticker map gives this label over the stretch."""
    pm = pm.rename(columns={"from": "frm"}).reset_index(drop=True)  # 'from' is a Python keyword
    pm["to_"] = pm["to"].where(pm["to"].notna(), last_day)
    cik_sets = []
    for r in pm.itertuples():
        s = segs[(segs["ticker"] == r.label) & (segs["start"] < r.to_) &
                 (segs["end"].isna() | (segs["end"] > r.frm))]
        fallback = str(int(r.cik)) if pd.notna(r.cik) else f"ticker:{r.label}"  # First Republic has no SEC record
        cik_sets.append(",".join(sorted({str(int(c)) for c in s["cik"].dropna()})) or fallback)
    pm["company"] = cik_sets

    keys = set(zip(pm["source"], pm["symbol"]))
    t = ds.dataset(prices_dir, partitioning="hive").to_table(
        columns=["symbol", "source", "date", "close", "dividend"],
        filter=pc.field("symbol").isin(sorted({s for _, s in keys})))
    px = t.to_pandas()
    px = px[[k in keys for k in zip(px["source"], px["symbol"])]]
    by_key = {k: g.sort_values("date") for k, g in px.groupby(["source", "symbol"])}
    closes = {k: g.set_index("date")["close"] for k, g in by_key.items()}

    ciks = sorted({int(c) for s in cik_sets for c in s.split(",") if c.isdigit()})
    tags = [DIV_TAG, "EntityPublicFloat"] + SHARE_TAGS
    f = ds.dataset(facts_path).to_table(
        columns=["cik", "tag", "unit", "start", "end", "value", "form", "filed"],
        filter=pc.field("cik").isin(ciks) & pc.field("tag").isin(tags)).to_pandas()
    span_days = (pd.to_datetime(f["end"]) - pd.to_datetime(f["start"])).dt.days
    divs = first_reported(f[(f["tag"] == DIV_TAG) & span_days.between(330, 380) & f["form"].str.startswith("10-K")])
    floats = f[f["tag"] == "EntityPublicFloat"].sort_values("filed").drop_duplicates(["cik", "end"])
    sh = f[f["tag"].isin(SHARE_TAGS)]
    # shares: the dei cover-page count (classes summed per filing) where there is one, else the balance sheet's
    sh = sh.groupby(["cik", "end", "tag", "filed"], as_index=False)["value"].sum()
    sh = sh.assign(pri=(sh["tag"] != SHARE_TAGS[0]).astype(int)).sort_values(["pri", "filed"])
    sh = sh.drop_duplicates(["cik", "end"])

    res = defaultdict(list)
    for r in pm.itertuples():
        p = by_key.get((r.source, r.symbol), pd.DataFrame(columns=["date", "close", "dividend"]))
        p = p[(p["date"] >= r.frm) & (p["date"] < r.to_)]
        cs = [int(c) for c in r.company.split(",") if c.isdigit()]  # none: no SEC data, so unverifiable
        d = divs[divs["cik"].isin(cs) & (divs["start"] >= r.frm) & (divs["end"] < r.to_)]
        fl = floats[floats["cik"].isin(cs) & (floats["end"] >= r.frm) & (floats["end"] < r.to_)]
        s = sh[sh["cik"].isin(cs)].reset_index(drop=True)
        dv, dn = dividend_check(p, d) if len(p) else ("n/a", 0)
        fv, fn = float_check(p, fl, s) if len(p) else ("n/a", 0)
        for k, v in [("dividends", dv), ("dividend_years", dn), ("float", fv), ("float_years", fn)]:
            res[k].append(v)
    for k, v in res.items():
        pm[k] = v
    pm["shared"] = pm.index.isin(shared_series(pm, closes))
    own = (pm["dividends"] == "pass") | (pm["float"] == "pass")
    bad = (pm["dividends"] == "fail") | (pm["float"] == "fail") | (pm["shared"] & ~own)
    pm["verdict"] = ["fail" if b else "pass" if o else "unverifiable" for b, o in zip(bad, own)]
    return pm.drop(columns="to_").rename(columns={"frm": "from"})
