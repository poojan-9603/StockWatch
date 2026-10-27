"""Monthly live universe (PLAN step 1): NYSE or Nasdaq, common stock, US companies (no ADRs), price above $5, market
value above $300M, and a minimum daily volume.

  US company   files 10-K/10-Q XBRL (an ADR files 20-F or 40-F instead), with one in the last 18 months
  price        the last close
  market value last close x the latest XBRL shares outstanding (cover-page count, classes summed)
  volume       median daily dollar volume over the last 20 trading days at or above MIN_DOLLAR_VOLUME. The plan doesn't
               set a number; $1M is a starting point to revisit (docs/M2.md)

Usage: python -m pipeline.universe OUT_DIR  ->  market/universe/<yyyy-mm>.parquet
"""
import sys
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds

from pipeline.prices import common_tickers, listed, today

MIN_PRICE, MIN_MARKET_VALUE, MIN_DOLLAR_VOLUME = 5.0, 300e6, 1e6


def build(companies, prices, facts, as_of):
    """companies: SEC table; prices: yfinance rows of the last ~30 days; facts: dei shares and 10-K/10-Q forms.
    Returns one row per qualifying (cik, symbol) with the numbers that qualified it."""
    recent = facts[facts["filed"] >= (pd.Timestamp(as_of) - pd.Timedelta(days=548)).strftime("%Y-%m-%d")]
    us = set(recent.loc[recent["form"].str.match(r"10-[KQ]"), "cik"])
    shares = (facts[facts["tag"] == "EntityCommonStockSharesOutstanding"]
              .groupby(["cik", "end", "accn"], as_index=False)["value"].sum()
              .sort_values("end").groupby("cik").tail(1).set_index("cik")["value"])
    last20 = prices.sort_values("date").groupby("symbol").tail(20)
    stats = last20.groupby("symbol").agg(close=("close", "last"), date=("date", "last"),
                                         dollar_volume=("volume", lambda v: (v * last20.loc[v.index, "close"]).median()))
    rows = []
    for c in companies.itertuples():
        if c.cik not in us or not listed(companies.loc[c.Index]) or c.cik not in shares.index:
            continue
        for t in common_tickers(companies.loc[c.Index]):
            if t not in stats.index:
                continue
            s = stats.loc[t]
            mv = s["close"] * shares[c.cik]
            if s["close"] > MIN_PRICE and mv > MIN_MARKET_VALUE and s["dollar_volume"] >= MIN_DOLLAR_VOLUME:
                rows.append({"cik": c.cik, "symbol": t, "name": c.name, "close": s["close"], "as_of": s["date"],
                             "market_value": mv, "dollar_volume": s["dollar_volume"], "sic": c.sic})
    return pd.DataFrame(rows)


def main(out_dir):
    out = Path(out_dir)
    as_of = today()
    start = (pd.Timestamp(as_of) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
    prices = ds.dataset(out / "market" / "prices", partitioning="hive").to_table(
        columns=["symbol", "date", "close", "volume"],
        filter=(pc.field("source") == "yfinance") & (pc.field("date") >= start)).to_pandas()
    facts = ds.dataset(out / "market" / "fundamentals" / "facts.parquet").to_table(
        columns=["cik", "tag", "end", "value", "form", "filed", "accn"]).to_pandas()
    u = build(pd.read_parquet(out / "market" / "companies.parquet"), prices, facts, as_of)
    (out / "market" / "universe").mkdir(parents=True, exist_ok=True)
    u.to_parquet(out / "market" / "universe" / f"{as_of[:7]}.parquet", index=False)
    print(f"universe {as_of[:7]}: {len(u)} stocks; market value median ${u['market_value'].median() / 1e9:.1f}B")


if __name__ == "__main__":
    main(sys.argv[1])
