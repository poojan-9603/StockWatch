import pandas as pd

from pipeline.universe import build


def test_universe_filters_price_value_volume_and_adrs():
    companies = pd.DataFrame([
        {"cik": 1, "name": "BIG", "tickers": "BIG", "exchanges": "NYSE", "sic": "1"},
        {"cik": 2, "name": "PENNY", "tickers": "PNY", "exchanges": "Nasdaq", "sic": "1"},
        {"cik": 3, "name": "ADR", "tickers": "ADR", "exchanges": "NYSE", "sic": "1"},      # files 20-F only
        {"cik": 4, "name": "TINY", "tickers": "TNY", "exchanges": "NYSE", "sic": "1"},     # under $300M
    ])
    d = pd.bdate_range("2026-09-01", periods=20).strftime("%Y-%m-%d")
    prices = pd.concat([pd.DataFrame({"symbol": s, "date": d, "close": c, "volume": 1e6})
                        for s, c in [("BIG", 50.0), ("PNY", 3.0), ("ADR", 40.0), ("TNY", 20.0)]])
    facts = pd.DataFrame([{"cik": k, "tag": "EntityCommonStockSharesOutstanding", "end": "2026-07-01", "value": sh,
                           "form": f, "filed": "2026-08-01", "accn": f"a{k}"}
                          for k, sh, f in [(1, 1e8, "10-Q"), (2, 1e9, "10-Q"), (3, 1e8, "20-F"), (4, 1e6, "10-Q")]])
    u = build(companies, prices, facts, "2026-10-01")
    assert list(u["symbol"]) == ["BIG"]
