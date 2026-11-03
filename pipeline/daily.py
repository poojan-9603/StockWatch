"""Nightly price update, checks and spot check (the SEC refresh, records and backups are separate steps).

prices      re-fetch the last WINDOW calendar days from yfinance for every yfinance symbol in the price plan, rebuild
            raw prices from the window's split table, and replace those (source, symbol, date) rows in their year
            partitions. Raw prices only change on or after a split, so a window catches every change: rerunning is
            idempotent.
checks      on the window rows: sanity and unexplained 40% moves (pipeline.quality's rules), and a block if under 95%
            of index stocks that should trade have a row for the latest trading day.
spotcheck   20 stocks a day from a fixed pool of 150 index stocks (re-drawn each month), against Tiingo over the
            window: closes within 0.5%, and the same splits and dividends on the same dates.

Usage: python -m pipeline.daily OUT_DIR prices|checks|spotcheck
  writes market/prices/year=YYYY/part-0.parquet (rewritten), market/quality/daily/<date>.parquet, logs/runs/<date>.json
"""
import json
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.prices import COLS, fetch_tiingo, fetch_yfinance, rebuild_raw, today
from pipeline.quality import BLOCK_SHARE, MOVE, big_moves, explained, issue, sanity

WINDOW = 14
POOL, PER_DAY = 150, 20


def window_start():
    return (datetime.now(timezone.utc) - timedelta(days=WINDOW)).strftime("%Y-%m-%d")


def merge(dest, new):
    """Replace new's (source, symbol, date) rows in the year partitions under dest; one file per touched year.
    Returns the touched years."""
    years = sorted(new["year"].unique())
    for y in years:
        part = dest / f"year={y}"
        old = pd.read_parquet(part) if part.exists() else pd.DataFrame(columns=new.columns)
        old = old.drop(columns=["year"], errors="ignore")
        n = new[new["year"] == y].drop(columns=["year"])
        key = lambda f: f["source"] + "|" + f["symbol"] + "|" + f["date"]
        keep = old[~key(old).isin(set(key(n)))]
        merged = pd.concat([keep, n], ignore_index=True).sort_values(["source", "symbol", "date"])
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir(parents=True)
        merged.to_parquet(part / "part-0.parquet", index=False)
    return years


def window_rows(out):
    """The raw window batches fetched today -> rows in the prices schema (raw prices rebuilt)."""
    d = out / "raw" / "yfinance_daily" / today()
    y = pd.concat([pd.read_parquet(p) for p in sorted(d.glob("batch_*.parquet"))], ignore_index=True)
    y = y.dropna(subset=["Close"])
    parts = [rebuild_raw(g.set_index("date").sort_index()).rename_axis("date").reset_index().assign(
        symbol=s, source="yfinance", fetched=today()) for s, g in y.groupby("symbol")]
    df = pd.concat(parts, ignore_index=True)
    return df.assign(year=df["date"].str[:4].astype(int))[COLS]


def run_prices(out, log):
    plan = pd.read_parquet(out / "market" / "price_plan.parquet")
    symbols = sorted({s for x in plan.loc[plan["source"] == "yfinance", "symbols"] for s in x.split("|") if s})
    failed = fetch_yfinance(out, symbols, start=window_start(), folder="yfinance_daily")
    new = window_rows(out)
    years = merge(out / "market" / "prices", new)
    idx = set(plan.loc[plan["index"] & plan["need_to"].isna() & (plan["source"] == "yfinance"), "symbols"])
    log.update(prices_rows=len(new), prices_symbols=int(new["symbol"].nunique()), prices_years=[int(y) for y in years],
               latest_day=new["date"].max(), yfinance_empty=len(failed), yfinance_empty_index=sorted(set(failed) & idx))
    if set(failed) & idx:
        log["errors"].append(f"index stocks without yfinance data: {sorted(set(failed) & idx)}")


def run_checks(out, log):
    plan = pd.read_parquet(out / "market" / "price_plan.parquet")
    new = window_rows(out)
    latest = new["date"].max()
    eightk = pd.read_parquet(out / "market" / "filings_8k.parquet", columns=["cik", "filed"])
    by_cik = {c: pd.to_datetime(g["filed"]).values for c, g in eightk.groupby("cik")}
    owner = {}
    for c in pd.read_parquet(out / "market" / "companies.parquet").itertuples():
        for t in filter(None, str(c.tickers).split(",")):
            owner.setdefault(t.replace("-", "."), set()).add(int(c.cik))
    issues = []
    for s, p in new.groupby("symbol"):
        p = p.sort_values("date").reset_index(drop=True)
        issues += sanity(p)
        for day, move in big_moves(p):
            if explained(day, owner.get(s, set()), by_cik, []) is None:
                issues.append(issue("move", "quarantine", "yfinance", s, day, day, f"{move:+.0%} unexplained"))
    idx = set(plan.loc[plan["index"] & plan["need_to"].isna() & (plan["source"] == "yfinance"), "symbols"])
    have = set(new.loc[new["date"] == latest, "symbol"])
    share = len(idx & have) / max(len(idx), 1)
    if share < BLOCK_SHARE:
        issues.append(issue("block day", "block", None, None, latest, latest,
                            f"{len(idx & have)} of {len(idx)} index stocks have a price"))
    # the run starts after midnight UTC; the trading day it's for is the US date (UTC minus 6 hours covers EST/EDT)
    us_day = datetime.now(timezone.utc) - timedelta(hours=6)
    # which day the index stocks' newest bar is on, so a block says why (vendor late, or a dating problem)
    last_bar = new[new["symbol"].isin(idx)].groupby("symbol")["date"].max().value_counts().head(3)
    log.update(checks_latest=latest, checks_index_share=round(share, 4), checks_index_last_bar=last_bar.to_dict(),
               checks=pd.Series([i["severity"] for i in issues]).value_counts().to_dict())
    if latest < us_day.strftime("%Y-%m-%d") and us_day.weekday() < 5:
        log["notes"].append(f"no bar for {us_day:%Y-%m-%d} (holiday or vendor delay); latest is {latest}")
    (out / "market" / "quality" / "daily").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(issues, columns=["check", "severity", "source", "symbol", "label", "start", "end", "detail"]).to_parquet(
        out / "market" / "quality" / "daily" / f"{today()}.parquet", index=False)
    if share < BLOCK_SHARE:
        log["errors"].append(f"block: only {share:.1%} of index stocks priced on {latest}; no verdicts today")


def pool_for(month, index_symbols):
    """The month's fixed spot-check pool: 150 index stocks drawn with the month as the seed."""
    return random.Random(month).sample(sorted(index_symbols), min(POOL, len(index_symbols)))


def todays(pool, day_of_month):
    k = (day_of_month - 1) * PER_DAY % len(pool)
    return (pool + pool)[k:k + PER_DAY]


def compare_window(y, t):
    """y: our window rows for one stock, t: Tiingo's. Issues for closes off by over 0.5% and split or dividend
    differences (Yahoo encodes spin-offs as fractional splits, Tiingo as cash: listed as log, not quarantine)."""
    out = []
    j = y.merge(t, on="date", suffixes=("_y", "_t"))
    off = j[(j["close_y"] / j["close_t"] - 1).abs() > 0.005]
    for r in off.itertuples():
        out.append(issue("spot close", "quarantine", "yfinance", y["symbol"].iloc[0], r.date, r.date,
                         f"yfinance {r.close_y:.4g} vs Tiingo {r.close_t:.4g}"))
    for col, base in [("split", 1.0), ("dividend", 0.0)]:
        a = j[["date", f"{col}_y"]][j[f"{col}_y"].fillna(base) != base].set_index("date")[f"{col}_y"]
        b = j[["date", f"{col}_t"]][j[f"{col}_t"].fillna(base) != base].set_index("date")[f"{col}_t"]
        for d in sorted(set(a.index) | set(b.index)):
            va, vb = a.get(d), b.get(d)
            if va is None or vb is None or abs(va / vb - 1) > 0.01:
                out.append(issue(f"spot {col}", "log", "yfinance", y["symbol"].iloc[0], d, d,
                                 f"yfinance {va} vs Tiingo {vb}"))
    return out


def run_spotcheck(out, log):
    plan = pd.read_parquet(out / "market" / "price_plan.parquet")
    idx = {s for s in plan.loc[plan["index"] & plan["need_to"].isna() & (plan["source"] == "yfinance"), "symbols"]}
    now = datetime.now(timezone.utc)
    picks = todays(pool_for(now.strftime("%Y-%m"), idx), now.day)
    folder = f"tiingo_spot/{today()}"
    fetch_tiingo(out, [s.replace(".", "-") for s in picks], folder=folder, start=window_start(), pause=1)
    new = window_rows(out)
    issues, compared = [], 0
    for s in picks:
        body = json.loads((out / "raw" / folder / f"{s.replace('.', '-')}.json").read_text())
        if not isinstance(body, list) or not body:
            issues.append(issue("spot missing", "log", "tiingo", s, today(), today(), str(body)[:80]))
            continue
        t = pd.DataFrame(body)
        t = pd.DataFrame({"date": t["date"].str[:10], "close": t["close"], "split": t["splitFactor"],
                          "dividend": t["divCash"]})
        issues += compare_window(new[new["symbol"] == s], t)
        compared += 1
    log.update(spot_compared=compared, spot_picks=picks,
               spot=pd.Series([i["check"] for i in issues]).value_counts().to_dict())
    (out / "market" / "quality" / "spot").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(issues, columns=["check", "severity", "source", "symbol", "label", "start", "end", "detail"]).to_parquet(
        out / "market" / "quality" / "spot" / f"{today()}.parquet", index=False)


def main(out_dir, step):
    out = Path(out_dir)
    path = out / "logs" / "runs" / f"{today()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    log = json.loads(path.read_text()) if path.exists() else {"date": today(), "errors": [], "notes": []}
    {"prices": run_prices, "checks": run_checks, "spotcheck": run_spotcheck}[step](out, log)
    path.write_text(json.dumps(log, indent=1, default=str))
    print(json.dumps({k: v for k, v in log.items() if k != "spot_picks"}, indent=1, default=str))
    if log["errors"]:
        sys.exit("; ".join(log["errors"]))


if __name__ == "__main__":
    main(*sys.argv[1:3])
