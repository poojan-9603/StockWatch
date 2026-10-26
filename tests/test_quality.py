import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

from pipeline.fundamentals import SCHEMA
from pipeline.quality import big_moves, main, missing_runs, sanity, stale


def series(symbol, closes, start="2015-01-02", source="yfinance", split=None, volume=1000.0):
    d = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"symbol": symbol, "source": source, "date": d, "open": closes, "high": [c * 1.01 for c in closes],
                         "low": [c * 0.99 for c in closes], "close": closes, "volume": volume,
                         "split": split if split is not None else [1.0] * len(closes)})


def test_moves_count_splits_and_flag_real_jumps():
    p = series("X", [100, 50, 50, 80, 80], split=[1, 2, 1, 1, 1])  # 2:1 split day, then a +60% jump
    assert [(d, round(m, 2)) for d, m in big_moves(p)] == [(p["date"][3], 0.6)]


def test_sanity_and_stale():
    p = series("X", [10, 10, 10, 10, 10, 11], volume=0.0)
    p.loc[5, "high"] = 9  # high below low
    assert [i["check"] for i in sanity(p)] == ["sanity"]
    s = stale(p)
    assert len(s) == 1 and s[0]["severity"] == "quarantine" and s[0]["detail"].startswith("5 days")


def test_missing_runs_counts_trading_days_only():
    days = pd.Index(["2015-01-05", "2015-01-06", "2015-01-07", "2015-01-08", "2015-01-09"])
    runs, n = missing_runs(pd.Index(["2015-01-05", "2015-01-09"]), days, "2015-01-05", "2015-01-09~",
                           {d: i for i, d in enumerate(days)})
    assert runs == [("2015-01-06", "2015-01-08", 3)] and n == 5


def test_main_end_to_end(tmp_path):
    m = tmp_path / "market"
    (m / "fundamentals").mkdir(parents=True)
    n = 30
    a = series("AAA", [100.0] * 14 + [150.0] * (n - 14))           # +50% on day 15: an 8-K explains it
    b = series("BBB", [20.0] * 14 + [35.0] * (n - 14))             # +75%, no 8-K: quarantine
    c = series("CCC", [50.0 + i for i in range(n)]).drop(index=range(5, 12))  # 7 trading days missing
    d = series("DDD", [5.0 + 0.01 * i for i in range(25)] + [500.0] * (n - 25), source="tiingo")  # jump after its stretch
    px = pd.concat([a, b, c, d], ignore_index=True)
    px["dividend"], px["fetched"], px["year"] = 0.0, "x", 2015
    ds.write_dataset(pa.Table.from_pandas(px, preserve_index=False), m / "prices", format="parquet",
                     partitioning=["year"], partitioning_flavor="hive")
    pd.DataFrame([{"label": s, "cik": i, "source": "yfinance", "symbol": s, "from": "2015-01-02", "to": None, "late": False}
                  for i, s in [(1, "AAA"), (2, "BBB"), (3, "CCC")]] +
                 [{"label": "DDD", "cik": 4, "source": "tiingo", "symbol": "DDD", "from": "2015-01-02",
                   "to": d["date"][20], "late": False}]).to_parquet(m / "price_map.parquet")
    pd.DataFrame([{"ticker": s, "spell_start": "2010-01-04", "start": "2010-01-04", "end": None, "cik": float(i), "link": None}
                  for i, s in [(1, "AAA"), (2, "BBB"), (3, "CCC")]]).to_parquet(m / "ticker_map.parquet")
    pd.DataFrame([{"cik": i, "tickers": s, "exchanges": "NYSE"} for i, s in [(1, "AAA"), (2, "BBB"), (3, "CCC")]]
                 ).to_parquet(m / "companies.parquet")
    pd.DataFrame([{"cik": 1, "filed": a["date"][13]}]).to_parquet(m / "filings_8k.parquet")
    facts = [{"cik": 1, "taxonomy": "us-gaap", "tag": t, "unit": "USD", "start": None, "end": "2015-12-31", "value": v,
              "fy": 2015, "fp": "FY", "form": "10-K", "filed": "2016-02-20", "accn": "a1", "frame": None}
             for t, v in [("Assets", 100.0), ("LiabilitiesAndStockholdersEquity", 90.0)]]  # off by 10%
    pa.parquet.write_table(pa.Table.from_pylist(facts, SCHEMA), m / "fundamentals" / "facts.parquet")
    try:
        main(tmp_path)
    except SystemExit as e:  # with only 3 index stocks, CCC's missing days leave 2 of 3 priced: block days
        assert "block days" in str(e)
    else:
        raise AssertionError("expected block days")
    i = pd.read_parquet(m / "quality" / "issues.parquet")
    got = {(r.check, r.severity, r.symbol) for r in i.itertuples()}
    assert ("move", "quarantine", "BBB") in got and not any(c == "move" and s == "AAA" for c, _, s in got)
    assert ("missing", "quarantine", "CCC") in got
    assert ("identity", "quarantine", "1") in got
    assert not any(s == "DDD" for _, _, s in got)  # Tiingo checked only inside its validated stretch
    assert (i["check"] == "block day").sum() == 7
