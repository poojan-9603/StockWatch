"""M1 data quality checks over the full history. Every issue is sorted into block (no verdicts that day),
quarantine (skip that stock for those dates) or log.

Prices (index stretches from market/price_map.parquet, plus every other symbol's whole series):
  calendar     trading days = days on which at least half of the year's most-covered yfinance symbols traded
  missing      trading days without a row inside a stretch: a run of more than 5 is quarantine, fewer is log
  block day    a trading day on which under 95% of the index stretches that should have a price have one
  sanity       price at or below 0, high below low, or close outside the day's range (0.1% leeway): quarantine
  move         a day-on-day close move over 40%, after the day's split, not explained by an 8-K from the
               company filed within 5 days before to 2 days after, or by a second source moving the same way:
               quarantine
  stale        the same close 5 or more trading days running: quarantine if volume is 0 throughout, else log
Fundamentals (10-K and 10-Q balance sheets, every filed copy):
  identity     Assets against LiabilitiesAndStockholdersEquity off by more than 0.5%: quarantine; Assets
               against Liabilities + equity including noncontrolling interest off by more than 5%: log

Usage: python -m pipeline.quality OUT_DIR  -> market/quality/issues.parquet, review/quality_*.csv
"""
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds

MOVE, STALE, MISSING_RUN, BLOCK_SHARE = 0.40, 5, 5, 0.95
COLS = ["symbol", "source", "date", "open", "high", "low", "close", "volume", "split"]


def issue(check, severity, source, symbol, start, end, detail, label=None):
    return {"check": check, "severity": severity, "source": source, "symbol": symbol, "label": label,
            "start": start, "end": end, "detail": detail}


def sanity(p):
    bad = (p["close"] <= 0) | (p["open"] <= 0) | (p["high"] < p["low"]) | \
          (p["close"] > p["high"] * 1.001) | (p["close"] < p["low"] * 0.999)
    return [issue("sanity", "quarantine", r.source, r.symbol, r.date, r.date,
                  f"o={r.open:.4g} h={r.high:.4g} l={r.low:.4g} c={r.close:.4g}") for r in p[bad].itertuples()]


def big_moves(p):
    """(date, move) where close moved more than MOVE from the previous row, after that day's split."""
    r = p["close"] * p["split"] / p["close"].shift() - 1
    m = r.abs() > MOVE
    return list(zip(p.loc[m, "date"], r[m]))


def stale(p):
    out = []
    same = p["close"].eq(p["close"].shift())
    run_id = (~same).cumsum()
    for _, g in p.groupby(run_id):
        if len(g) >= STALE:
            zero = (g["volume"].fillna(0) == 0).all()
            out.append(issue("stale", "quarantine" if zero else "log", g["source"].iloc[0], g["symbol"].iloc[0],
                             g["date"].iloc[0], g["date"].iloc[-1], f"{len(g)} days at {g['close'].iloc[0]:.4g}"
                             + (", volume 0" if zero else "")))
    return out


def missing_runs(dates, days, lo, hi, pos):
    """Runs (start, end, length) of trading days in [lo, hi) with no row. pos: trading day -> its index."""
    want = days[(days >= lo) & (days < hi)]
    gone = want[~want.isin(dates)]
    runs, start, prev, n = [], None, None, 0
    for d in gone:
        if prev is not None and pos[d] == pos[prev] + 1:
            n += 1
        else:
            if start is not None:
                runs.append((start, prev, n))
            start, n = d, 1
        prev = d
    if start is not None:
        runs.append((start, prev, n))
    return runs, len(want)


def calendar(prices_dir):
    t = ds.dataset(prices_dir, partitioning="hive").to_table(columns=["date"], filter=pc.field("source") == "yfinance")
    counts = t.to_pandas()["date"].value_counts()
    year_max = counts.groupby(counts.index.str[:4]).transform("max")
    return pd.Index(sorted(counts[counts >= 0.5 * year_max].index))


def eight_k_dates(eightk, ciks):
    return pd.to_datetime(eightk.loc[eightk["cik"].isin(ciks), "filed"]).sort_values().values


def explained(day, ciks, eightk_by_cik, others):
    d = np.datetime64(day)
    for c in ciks:
        f = eightk_by_cik.get(c)
        if f is not None and ((f >= d - np.timedelta64(5, "D")) & (f <= d + np.timedelta64(2, "D"))).any():
            return "8-K"
    for o in others:  # another source's series for the same symbol moving the same way that day
        if day in o.index:
            i = o.index.get_loc(day)
            if i > 0 and abs(o.iloc[i] / o.iloc[i - 1] - 1) > MOVE / 2:
                return "second source"
    return None


def price_checks(out):
    prices_dir = out / "market" / "prices"
    days = calendar(prices_dir)
    pm = pd.read_parquet(out / "market" / "price_map.parquet")
    segs = pd.read_parquet(out / "market" / "ticker_map.parquet")
    companies = pd.read_parquet(out / "market" / "companies.parquet")
    eightk = pd.read_parquet(out / "market" / "filings_8k.parquet", columns=["cik", "filed"])
    eightk_by_cik = {c: pd.to_datetime(g["filed"]).values for c, g in eightk.groupby("cik")}
    # which companies a symbol stands for: index stretches through the ticker map, the rest through SEC tickers
    owner = {}
    for c in companies.itertuples():
        for t in filter(None, str(c.tickers).split(",")):
            owner.setdefault(("yfinance", t.replace("-", ".")), set()).add(int(c.cik))
    for r in pm.itertuples():
        s = segs[segs["ticker"] == r.label]
        owner.setdefault((r.source, r.symbol), set()).update(int(x) for x in s["cik"].dropna())

    issues, expected, present = [], Counter(), Counter()
    dset = ds.dataset(prices_dir, partitioning="hive")
    symbols = sorted(set(pc.unique(dset.to_table(columns=["symbol"])["symbol"]).to_pylist()))
    stretches = {k: g for k, g in pm.rename(columns={"from": "frm"}).groupby(["source", "symbol"])}
    pos = {d: i for i, d in enumerate(days)}
    for i in range(0, len(symbols), 300):
        chunk = dset.to_table(columns=COLS, filter=pc.field("symbol").isin(symbols[i:i + 300])).to_pandas()
        series = {k: g.sort_values("date").reset_index(drop=True) for k, g in chunk.groupby(["source", "symbol"])}
        for (src, sym), p in series.items():
            issues += sanity(p) + stale(p)
            closes = {s2: q.set_index("date")["close"] for (s2, y2), q in series.items() if y2 == sym and s2 != src}
            for day, move in big_moves(p):
                why = explained(day, owner.get((src, sym), set()), eightk_by_cik, closes.values())
                if why is None:
                    issues.append(issue("move", "quarantine", src, sym, day, day, f"{move:+.0%} unexplained"))
            dates = pd.Index(p["date"])
            # index stretches: within their validated dates; other symbols: first to last row (a live-universe stock
            # that delisted isn't missing the days after it). '~' sorts after any date: an open end includes the last day
            spans = [(r.label, r.frm, r.to if isinstance(r.to, str) else days[-1] + "~")
                     for r in stretches[(src, sym)].itertuples()] if (src, sym) in stretches else \
                ([(None, dates[0], dates[-1] + "~")] if src == "yfinance" else [])
            for label, lo, hi in spans:
                runs, n = missing_runs(dates, days, lo, hi, pos)
                for a, b, k in runs:
                    issues.append(issue("missing", "quarantine" if k > MISSING_RUN else "log", src, sym, a, b,
                                        f"{k} trading days missing", label))
                if label is not None:
                    want = days[(days >= lo) & (days < hi)]
                    expected.update(want)
                    present.update(want[want.isin(dates)])
        print(f"prices checked: {min(i + 300, len(symbols))}/{len(symbols)} symbols", flush=True)
    for d in sorted(expected):
        if present[d] < BLOCK_SHARE * expected[d]:
            issues.append(issue("block day", "block", None, None, d, d,
                                f"{present[d]} of {expected[d]} index stretches have a price"))
    return issues, days


def fundamentals_checks(out):
    tags = ["Assets", "LiabilitiesAndStockholdersEquity", "Liabilities",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
    f = ds.dataset(out / "market" / "fundamentals" / "facts.parquet").to_table(
        columns=["cik", "tag", "unit", "start", "end", "value", "form", "filed", "accn"],
        filter=pc.field("tag").isin(tags) & pc.field("start").is_null() & (pc.field("unit") == "USD")).to_pandas()
    f = f[f["form"].str.match(r"10-[KQ]")]
    w = f.pivot_table(index=["cik", "accn", "end", "filed"], columns="tag", values="value", aggfunc="first")
    out_ = []
    a, le = w.get("Assets"), w.get("LiabilitiesAndStockholdersEquity")
    both = w[a.notna() & le.notna() & (a.abs() > 0)]
    off = (both["Assets"] - both["LiabilitiesAndStockholdersEquity"]).abs() / both["Assets"].abs() > 0.005
    for (cik, accn, end, filed), r in both[off].iterrows():
        out_.append(issue("identity", "quarantine", "sec", str(cik), end, end,
                          f"Assets {r['Assets']:.4g} vs LiabilitiesAndStockholdersEquity "
                          f"{r['LiabilitiesAndStockholdersEquity']:.4g} ({accn}, filed {filed})"))
    eq = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
    if "Liabilities" in w and eq in w:
        sub = w[w["Assets"].notna() & w["Liabilities"].notna() & w[eq].notna() & (w["Assets"].abs() > 0)]
        off = (sub["Assets"] - sub["Liabilities"] - sub[eq]).abs() / sub["Assets"].abs() > 0.05
        for (cik, accn, end, filed), r in sub[off].iterrows():
            out_.append(issue("identity", "log", "sec", str(cik), end, end,
                              f"Assets {r['Assets']:.4g} vs Liabilities + equity incl. NCI "
                              f"{r['Liabilities'] + r[eq]:.4g}, often temporary equity ({accn})"))
    return out_, len(both)


def main(out_dir):
    out = Path(out_dir)
    issues, days = price_checks(out)
    fi, n_bs = fundamentals_checks(out)
    df = pd.DataFrame(issues + fi, columns=["check", "severity", "source", "symbol", "label", "start", "end", "detail"])
    (out / "market" / "quality").mkdir(parents=True, exist_ok=True)
    df.to_parquet(out / "market" / "quality" / "issues.parquet", index=False)
    (out / "review").mkdir(parents=True, exist_ok=True)
    for sev in ["block", "quarantine"]:
        df[df["severity"] == sev].to_csv(out / "review" / f"quality_{sev}.csv", index=False)
    per_year = pd.Series(days.str[:4]).value_counts().sort_index()
    print(f"trading days per year: {per_year.to_dict()}")
    print(f"balance sheets with both Assets and LiabilitiesAndStockholdersEquity: {n_bs}")
    df["period"] = np.where(df["start"].astype(str) < "2020-01-01", "to 2019", "2020 on")
    print(df.groupby(["check", "severity", "period"]).size().to_string())
    blocks = (df["severity"] == "block").sum()
    print(f"block days: {blocks}")
    if blocks:
        sys.exit("block days found: see review/quality_block.csv")


if __name__ == "__main__":
    main(sys.argv[1])
