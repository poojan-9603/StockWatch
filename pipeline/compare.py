"""M1 step 7: yfinance (raw prices rebuilt from its split table) against Tiingo (raw) on 30 stocks.

The 30 are picked to stress what could break, not at random: splits (one 50:1, one reverse 1:10), share classes,
heavy, monthly and special dividends, spin-offs, small caps, and three stocks whose yfinance series failed an
identity check in step 5 (AT&T, Progressive, Visa).

Per stock, over the dates both sources have since 2010:
  close   share of days the two closes agree within 0.5% and within 1%; the worst day
  ohl     share of days open, high and low all agree within 1%
  volume  median ratio, yfinance / Tiingo (sources count consolidated volume differently, so a steady ratio
          near 1 is the expectation, not equality)
  splits  dates and ratios on each side; any date or ratio on one side only is listed
  divs    ex-dates and amounts; matched when the same date and within 1%; the rest listed
  days    trading days in one source but not the other

Usage: python -m pipeline.compare OUT_DIR  (needs market/prices from the prices workflow and TIINGO_API_KEY)
  writes raw/tiingo_compare/<SYMBOL>.json, review/source_comparison.csv, review/source_comparison_events.csv
"""
import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds

from pipeline.prices import SINCE, fetch_tiingo

STOCKS = ["AAPL", "NVDA", "TSLA", "AMZN", "GOOGL", "WMT", "CMG", "AVGO", "NFLX", "SMCI",  # splits
          "C",                                                                         # 1:10 reverse split, 2011
          "BRK.B", "GOOG",                                                             # share classes
          "KO", "XOM", "T", "VZ", "MO", "O", "PLD",                                    # dividends (O is monthly)
          "GE", "MMM", "JNJ",                                                          # spin-offs
          "JPM", "BAC", "CROX", "ELF", "AXON",                                         # banks, smaller caps
          "PGR", "V"]                                                                  # step 5 identity fails


def tiingo_frame(path):
    t = pd.DataFrame(json.loads(path.read_text()))
    return pd.DataFrame({"date": t["date"].str[:10], "open": t["open"], "high": t["high"], "low": t["low"],
                         "close": t["close"], "volume": t["volume"], "dividend": t["divCash"],
                         "split": t["splitFactor"]})


def compare(y, t):
    """y, t: one stock's frames (date, open, high, low, close, volume, dividend, split) from each source.
    Returns (summary dict, events list)."""
    lo, hi = max(y["date"].min(), t["date"].min()), min(y["date"].max(), t["date"].max())
    y, t = y[(y["date"] >= lo) & (y["date"] <= hi)], t[(t["date"] >= lo) & (t["date"] <= hi)]
    j = y.merge(t, on="date", suffixes=("_y", "_t"))
    rel = (j["close_y"] / j["close_t"] - 1).abs()
    worst = j.loc[rel.idxmax()] if len(j) else None
    ohl = pd.concat([(j[f"{c}_y"] / j[f"{c}_t"] - 1).abs() < 0.01 for c in ["open", "high", "low"]], axis=1).all(axis=1)
    vol = (j["volume_y"] / j["volume_t"]).replace([float("inf")], float("nan"))
    events = []
    for kind, col, tol in [("split", "split", 1e-6), ("dividend", "dividend", 0.01)]:
        a = y.loc[(y[col] != (1 if kind == "split" else 0)) & y[col].notna(), ["date", col]].set_index("date")[col]
        b = t.loc[(t[col] != (1 if kind == "split" else 0)) & t[col].notna(), ["date", col]].set_index("date")[col]
        for d in sorted(set(a.index) | set(b.index)):
            va, vb = a.get(d), b.get(d)
            if va is None or vb is None or abs(va / vb - 1) > tol:
                events.append({"kind": kind, "date": d, "yfinance": va, "tiingo": vb})
    n_div = lambda f: int((f["dividend"].fillna(0) > 0).sum())
    summary = {
        "from": lo, "to": hi, "days_both": len(j),
        "only_yfinance": len(set(y["date"]) - set(t["date"])), "only_tiingo": len(set(t["date"]) - set(y["date"])),
        "close_within_0.5%": round((rel < 0.005).mean(), 4), "close_within_1%": round((rel < 0.01).mean(), 4),
        "worst_close_date": None if worst is None else worst["date"],
        "worst_close_yf": None if worst is None else round(worst["close_y"], 4),
        "worst_close_tiingo": None if worst is None else round(worst["close_t"], 4),
        "ohl_within_1%": round(ohl.mean(), 4), "volume_ratio_median": round(vol.median(), 3),
        "splits_yf": int((y["split"] != 1).sum()), "splits_tiingo": int((t["split"] != 1).sum()),
        "dividends_yf": n_div(y), "dividends_tiingo": n_div(t),
        "split_mismatches": sum(e["kind"] == "split" for e in events),
        "dividend_mismatches": sum(e["kind"] == "dividend" for e in events),
    }
    return summary, events


def main(out_dir):
    out = Path(out_dir)
    fetch_tiingo(out, [s.replace(".", "-") for s in STOCKS], folder="tiingo_compare")
    yf = ds.dataset(out / "market" / "prices", partitioning="hive").to_table(
        columns=["symbol", "date", "open", "high", "low", "close", "volume", "dividend", "split"],
        filter=(pc.field("source") == "yfinance") & pc.field("symbol").isin(STOCKS)).to_pandas()
    rows, events = [], []
    for s in STOCKS:
        y = yf[yf["symbol"] == s].sort_values("date")
        p = out / "raw" / "tiingo_compare" / f"{s.replace('.', '-')}.json"
        body = json.loads(p.read_text())
        if y.empty or not isinstance(body, list) or not body:
            rows.append({"symbol": s, "note": "missing in yfinance" if y.empty else f"Tiingo: {body}"})
            continue
        summary, ev = compare(y, tiingo_frame(p))
        rows.append({"symbol": s, **summary})
        events += [{"symbol": s, **e} for e in ev]
    (out / "review").mkdir(parents=True, exist_ok=True)
    r = pd.DataFrame(rows)
    r.to_csv(out / "review" / "source_comparison.csv", index=False)
    pd.DataFrame(events, columns=["symbol", "kind", "date", "yfinance", "tiingo"]).to_csv(
        out / "review" / "source_comparison_events.csv", index=False)
    pd.set_option("display.width", 250)
    print(r.to_string(index=False))
    print(f"\nsplit or dividend differences: {len(events)}")


if __name__ == "__main__":
    main(sys.argv[1])
