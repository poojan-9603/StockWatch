import pandas as pd

from pipeline.prices import common_tickers, gaps, plan, rebuild_raw


def test_rebuild_raw_undoes_split_adjustment_like_aapl_2012():
    # yfinance shows AAPL's 2012-08-09 close as 22.17 and dividend as 0.0946 after the 7:1 (2014) and 4:1 (2020) splits
    h = pd.DataFrame({
        "Open": [22.17, 90.0, 120.0], "High": [22.17, 90.0, 120.0], "Low": [22.17, 90.0, 120.0],
        "Close": [22.17, 90.0, 120.0], "Volume": [221.6e6, 50e6, 40e6], "Dividends": [0.0946, 0.0, 0.0],
        "Stock Splits": [0.0, 7.0, 4.0],
    }, index=["2012-08-09", "2014-06-09", "2020-08-31"])
    r = rebuild_raw(h)
    assert round(r.loc["2012-08-09", "close"], 1) == 620.8      # x 28
    assert round(r.loc["2012-08-09", "dividend"], 2) == 2.65
    assert round(r.loc["2012-08-09", "volume"] / 1e6, 2) == 7.91  # / 28
    assert r.loc["2014-06-09", "close"] == 360.0                 # only the 2020 split comes after
    assert r.loc["2020-08-31", "close"] == 120.0 and r.loc["2020-08-31", "split"] == 4.0


def test_common_tickers_keeps_share_classes_drops_preferreds_and_warrants():
    c = {"tickers": "BRK-B,BRK-A,BAC-PL,SKYD-WT,ABCDW,GOOGL"}
    assert common_tickers(c) == ["BRK.B", "BRK.A", "GOOGL"]


def seg(ticker, start, end, cik, link=None):
    return {"ticker": ticker, "spell_start": start, "start": start, "end": end, "cik": cik, "link": link}


def test_plan_routes_live_renamed_and_dead_securities():
    segs = pd.DataFrame([
        seg("AAPL", "1996-01-02", None, 1),           # still trades under this ticker
        seg("FB", "2013-12-23", "2022-06-09", 2),     # renamed, company still listed as META
        seg("LLTC", "2000-04-03", "2017-03-13", 3),   # dead; Tiingo only from 2016, WIKI before
        seg("TSS", "2008-01-02", "2019-09-23", 4),    # dead; nothing in Tiingo, WIKI stops 2018-03-27
    ])
    companies = pd.DataFrame([
        {"cik": 1, "tickers": "AAPL", "exchanges": "Nasdaq"},
        {"cik": 2, "tickers": "META", "exchanges": "Nasdaq"},
        {"cik": 3, "tickers": "", "exchanges": ""},
        {"cik": 4, "tickers": "", "exchanges": ""},
        {"cik": 5, "tickers": "XYZ", "exchanges": "NYSE"},  # not an index member: live universe only
    ])
    tiingo = pd.DataFrame([{"ticker": "LLTC", "assetType": "Stock", "priceCurrency": "USD",
                            "startDate": "2016-01-04", "endDate": "2017-03-10"}])
    p = plan(segs, companies, tiingo, {})
    got = {(r.label, r.source, r.symbols, r.need_from, r.need_to if isinstance(r.need_to, str) else None)
           for r in p.itertuples()}
    assert ("AAPL", "yfinance", "AAPL", "2010-01-01", None) in got
    assert ("FB", "yfinance", "META", "2013-12-23", "2022-06-09") in got
    assert ("LLTC", "tiingo", "LLTC", "2016-01-04", "2017-03-13") in got
    assert ("LLTC", "wiki", "LLTC", "2010-01-01", "2016-01-04") in got
    assert ("TSS", "wiki", "TSS", "2010-01-01", "2018-03-27") in got
    assert ("TSS", "none", "", "2018-03-27", "2019-09-23") in got
    assert ("XYZ", "yfinance", "XYZ", "2010-01-01", None) in got
    assert not p.loc[p["label"] == "XYZ", "index"].any()


def test_gaps_allow_a_few_days_at_the_end_and_flag_real_holes():
    p = pd.DataFrame([
        {"label": "A", "need_from": "2010-01-01", "need_to": "2016-02-01", "source": "wiki", "symbols": "A", "cik": 1, "index": True},
        {"label": "B", "need_from": "2010-01-01", "need_to": "2013-09-11", "source": "wiki", "symbols": "B", "cik": 2, "index": True},
        {"label": "C", "need_from": "2018-03-27", "need_to": "2019-09-23", "source": "none", "symbols": "", "cik": 3, "index": True},
    ])
    span = {("wiki", "A"): ("2010-01-04", "2016-01-29"), ("wiki", "B"): ("2011-06-01", "2013-09-10")}
    g = gaps(p, span, "2026-10-23")
    assert list(zip(g["label"], g["gap_from"], g["gap_to"])) == [
        ("B", "2010-01-01", "2011-06-01"), ("C", "2018-03-27", "2019-09-23")]


def test_build_writes_year_partitions_from_raw_files_and_reruns_identically(tmp_path):
    import json

    from pipeline.prices import build
    (tmp_path / "raw" / "tiingo").mkdir(parents=True)
    (tmp_path / "raw" / "tiingo" / "OLD.json").write_text(json.dumps([
        {"date": f"{d}T00:00:00.000Z", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10, "divCash": 0,
         "splitFactor": 1} for d in ["2009-12-31", "2012-05-01", "2013-01-02"]]))
    for _ in range(2):
        n, span = build(tmp_path)
    assert n == 2 and span[("tiingo", "OLD")] == ("2012-05-01", "2013-01-02")  # 2009 row dropped
    assert sorted(p.parent.name for p in (tmp_path / "market" / "prices").rglob("*.parquet")) == ["year=2012", "year=2013"]
