import pandas as pd

from pipeline.daily import compare_window, merge, pool_for, todays


def rows(symbol, dates, close, year=2026, source="yfinance"):
    return pd.DataFrame({"symbol": symbol, "source": source, "date": dates, "open": close, "high": close, "low": close,
                         "close": close, "volume": 1.0, "dividend": 0.0, "split": 1.0, "fetched": "x", "year": year})


def test_merge_replaces_window_rows_and_is_idempotent(tmp_path):
    old = rows("AAPL", ["2026-10-20", "2026-10-21", "2026-10-22"], 100.0)
    merge(tmp_path, old)
    new = rows("AAPL", ["2026-10-22", "2026-10-23"], 101.0)  # corrects 10-22, adds 10-23
    for _ in range(2):
        merge(tmp_path, new)
    got = pd.read_parquet(tmp_path / "year=2026").sort_values("date")
    assert list(got["date"]) == ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    assert list(got["close"]) == [100.0, 100.0, 101.0, 101.0]


def test_spot_pool_is_fixed_for_the_month_and_rotates_daily():
    syms = {f"S{i:03d}" for i in range(400)}
    p = pool_for("2026-10", syms)
    assert p == pool_for("2026-10", syms) and p != pool_for("2026-11", syms) and len(p) == 150
    assert len(todays(p, 1)) == 20 and todays(p, 1) != todays(p, 2)
    assert set().union(*[todays(p, d) for d in range(1, 9)]) == set(p)  # every stock checked within 8 days


def test_compare_window_flags_close_and_lists_dividend_differences():
    y = rows("KO", ["2026-10-20", "2026-10-21"], 60.0)
    y.loc[1, "dividend"] = 0.51
    t = pd.DataFrame({"date": ["2026-10-20", "2026-10-21"], "close": [60.0, 61.0], "split": 1.0, "dividend": [0.0, 0.51]})
    got = [(i["check"], i["severity"]) for i in compare_window(y, t)]
    assert got == [("spot close", "quarantine")]  # dividends match; the 10-21 close is 1.6% off
