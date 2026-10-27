"""M3 decision measurements on the 2013 to 2019 tuning years only (the lock enforces it).

For every month-end and every company in the index that month (member-months):
  inputs    which signal inputs are available as known that day (pit.as_of), and so which candidate definition of
            each signal can be computed how often
  sectors   members per sector each month under each candidate SIC mapping, and gics11's agreement with the GICS
            sector Wikipedia lists for today's members
  prices    member-months without validated prices, by reason, and member-months on unverified vendor prices

Usage: python -m quant.measure OUT_DIR  ->  review/m3_*.csv, plus a printed summary
"""
import io
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds

from pipeline.membership import wiki_current
from quant import lock
from quant.pit import as_of, first_available
from quant.sectors import division, gics11

FROM = "2013-01-31"
REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet"]
COST = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]
INCOME = REVENUE + COST + ["GrossProfit", "NetIncomeLoss", "OperatingIncomeLoss", "NetCashProvidedByUsedInOperatingActivities"]
BALANCE = ["Assets", "StockholdersEquity", "CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"]
EPS = "EarningsPerShareDiluted"

SIGNALS = {  # candidate definition -> inputs it needs
    "quality: gross profit / assets": ["gross_profit", "assets"],
    "quality: net income / assets (ROA)": ["net_income", "assets"],
    "quality: net income / equity (ROE)": ["net_income", "equity"],
    "quality: operating income / assets": ["operating_income", "assets"],
    "value: earnings yield (net income / market value)": ["net_income", "shares", "priced"],
    "value: book / market": ["equity", "shares", "priced"],
    "value: sales / market": ["revenue", "shares", "priced"],
    "momentum 12-1": ["priced", "priced_13m"],
    "low volatility (1 year)": ["priced", "priced_13m"],
    "earnings surprise (8+ quarters of EPS)": ["eps_8q"],
    "share issuance (1 year)": ["shares", "shares_1y"],
    "asset growth (1 year)": ["assets", "assets_1y"],
}


def month_ends():
    days = pd.date_range(FROM, lock.cutoff(), freq="ME").strftime("%Y-%m-%d")
    days = [d for d in days if d < lock.cutoff()]
    lock.check(days)
    return days


def members(segs, day):
    s = segs[(segs["start"] <= day) & (segs["end"].isna() | (segs["end"] > day))]
    return s.dropna(subset=["cik"]).assign(cik=lambda x: x["cik"].astype(int))[["ticker", "cik"]].drop_duplicates("cik")


def shift_months(day, n):
    return (pd.Timestamp(day) - pd.DateOffset(months=n)).strftime("%Y-%m-%d")


def price_intervals(pm, segs):
    """cik -> [(from, to)] of validated price stretches, through the labels the ticker map gives that company."""
    label_ciks = segs.dropna(subset=["cik"]).groupby("ticker")["cik"].apply(lambda c: {int(x) for x in c}).to_dict()
    out = defaultdict(list)
    for r in pm.itertuples(index=False):
        for c in label_ciks.get(r.label, set()) | ({int(r.cik)} if pd.notna(r.cik) else set()):
            out[c].append((r[4], r.to if isinstance(r.to, str) else "9999"))  # r[4] is 'from'
    return out


def covered(iv, cik, day):
    return any(a <= day < b for a, b in iv.get(cik, []))


def inputs(facts, segs, pm):
    iv = price_intervals(pm, segs)
    eps = facts[(facts["tag"] == EPS)]
    eps = eps[(pd.to_datetime(eps["end"]) - pd.to_datetime(eps["start"])).dt.days.between(80, 100)]
    rows = []
    for d in month_ends():
        m = members(segs, d)
        inc, bs = as_of(facts, d, INCOME, annual=True), as_of(facts, d, BALANCE, annual=False)
        bs1 = as_of(facts, shift_months(d, 12), BALANCE, annual=False)
        rev, cost = first_available(inc, REVENUE), first_available(inc, COST)
        gp = first_available({"GrossProfit": inc.get("GrossProfit", pd.Series(dtype=float)),
                              "derived": (rev - cost).dropna()}, ["GrossProfit", "derived"])
        shares = first_available(bs, ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"])
        shares1 = first_available(bs1, ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"])
        e = eps[(eps["filed"] <= d) & (eps["end"] >= shift_months(d, 36))]
        n_eps = e.drop_duplicates(["cik", "end"]).groupby("cik").size()
        get = lambda s, c: c in s.index if s is not None else False
        for c in m["cik"]:
            rows.append({"month": d, "cik": c,
                         "assets": get(bs.get("Assets"), c), "equity": get(bs.get("StockholdersEquity"), c),
                         "net_income": get(inc.get("NetIncomeLoss"), c),
                         "operating_income": get(inc.get("OperatingIncomeLoss"), c),
                         "revenue": c in rev.index, "gross_profit": c in gp.index,
                         "gross_profit_tag_only": get(inc.get("GrossProfit"), c),
                         "shares": c in shares.index, "shares_1y": c in shares1.index,
                         "assets_1y": get(bs1.get("Assets"), c), "eps_8q": n_eps.get(c, 0) >= 8,
                         "priced": covered(iv, c, d), "priced_13m": covered(iv, c, shift_months(d, 13))})
        print(f"inputs {d}: {len(m)} members", flush=True)
    return pd.DataFrame(rows)


def sector_tables(companies, segs, wiki_html):
    co = companies.set_index("cik")
    rows = []
    for d in month_ends():
        for c in members(segs, d)["cik"]:
            sic = co["sic"].get(c)
            rows.append({"month": d, "cik": c, "division": division(sic), "gics11": gics11(sic),
                         "office": co["owner_org"].get(c)})
    m = pd.DataFrame(rows)
    sizes = {k: m.groupby(["month", k]).size().groupby(level=1).mean().round(1).sort_values()
             for k in ["division", "gics11", "office"]}
    cur = wiki_current(wiki_html)
    t = next(t for t in pd.read_html(io.StringIO(wiki_html)) if "Symbol" in t.columns)
    cur["gics"] = t["GICS Sector"].values
    cur["mapped"] = cur["cik"].map(lambda c: gics11(co["sic"].get(c)))
    return m, sizes, cur


def price_issues(gaps, idv, segs):
    out = []
    for d in month_ends():
        m = members(segs, d)
        labels = set(m["ticker"])
        g = gaps[gaps["label"].isin(labels) & (gaps["gap_from"] <= d) & (gaps["gap_to"] > d)]
        for r in g.itertuples():
            out.append({"month": d, "label": r.label, "kind": f"unpriced: {r.reason}"})
        u = idv[idv["label"].isin(labels) & (idv["verdict"] == "unverifiable") & idv["source"].isin(["tiingo", "wiki"])]
        u = u[(u["from"] <= d) & (u["to"].fillna("9999") > d)]
        for r in u.itertuples():
            out.append({"month": d, "label": r.label, "kind": "priced, unverified vendor series"})
    return pd.DataFrame(out, columns=["month", "label", "kind"])


def main(out_dir):
    out = Path(out_dir)
    segs = pd.read_parquet(out / "market" / "ticker_map.parquet")
    pm = pd.read_parquet(out / "market" / "price_map.parquet")
    index_ciks = sorted({int(c) for c in segs["cik"].dropna()})
    facts = ds.dataset(out / "market" / "fundamentals" / "facts.parquet").to_table(
        columns=["cik", "tag", "start", "end", "value", "form", "filed"],
        filter=pc.field("tag").isin(INCOME + BALANCE + [EPS]) & (pc.field("filed") < lock.cutoff())
        & pc.field("cik").isin(index_ciks)).to_pandas()
    lock.check(facts["filed"])
    (out / "review").mkdir(parents=True, exist_ok=True)

    inp = inputs(facts, segs, pm)
    n = len(inp)
    cov = pd.DataFrame([{"definition": k, "member_months": n, "share_computable": round(inp[v].all(axis=1).mean(), 4)}
                        for k, v in SIGNALS.items()])
    cov.to_csv(out / "review" / "m3_signal_coverage.csv", index=False)
    inp.drop(columns=["month", "cik"]).mean().round(4).to_csv(out / "review" / "m3_input_coverage.csv", header=["share"])

    wiki_html = sorted((out / "raw" / "membership").glob("*/wiki_current.html"))[-1].read_text(encoding="utf-8")
    m, sizes, cur = sector_tables(pd.read_parquet(out / "market" / "companies.parquet"), segs, wiki_html)
    for k, s in sizes.items():
        s.to_csv(out / "review" / f"m3_sector_sizes_{k}.csv", header=["members_per_month"])
    agree = (cur["gics"] == cur["mapped"]).mean()
    cur.to_csv(out / "review" / "m3_gics_check.csv", index=False)

    pi = price_issues(pd.read_parquet(out / "market" / "price_gaps.parquet"),
                      pd.read_parquet(out / "market" / "price_identity.parquet"), segs)
    pi.groupby("kind").size().to_csv(out / "review" / "m3_price_issue_months.csv", header=["member_months"])
    pi.groupby(["kind", "label"]).size().to_csv(out / "review" / "m3_price_issue_labels.csv", header=["member_months"])

    print(f"\nmember-months 2013-2019: {n}")
    print(cov.to_string(index=False))
    print("\ninputs:", inp.drop(columns=["month", "cik"]).mean().round(3).to_dict())
    for k, s in sizes.items():
        print(f"\nsector sizes, {k}: {len(s)} groups, smallest {s.iloc[0]} ({s.index[0]}), median {s.median()}")
    print(f"\ngics11 agrees with Wikipedia's GICS sector for {agree:.1%} of today's {len(cur)} members")
    print(cur[cur["gics"] != cur["mapped"]].groupby(["gics", "mapped"]).size().sort_values(ascending=False).head(12).to_string())
    print("\nprice issues (member-months):", pi.groupby("kind").size().to_dict(), "of", n)


if __name__ == "__main__":
    main(sys.argv[1])
