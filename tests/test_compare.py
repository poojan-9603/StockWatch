import pandas as pd

from pipeline.compare import compare


def frame(closes, dates, divs=None, splits=None):
    n = len(closes)
    return pd.DataFrame({"date": dates, "open": closes, "high": closes, "low": closes, "close": closes,
                         "volume": [1000.0] * n, "dividend": divs or [0.0] * n, "split": splits or [1.0] * n})


def test_compare_counts_agreement_and_lists_split_and_dividend_differences():
    d = ["2015-01-02", "2015-01-05", "2015-01-06", "2015-01-07", "2015-01-08"]
    y = frame([10, 10, 10.2, 10, 10], d, divs=[0, 0.5, 0, 0, 0], splits=[1, 1, 1, 2, 1])
    t = frame([10, 10, 10, 10, 10], d[:4] + ["2015-01-09"], divs=[0, 0, 0.5, 0, 0], splits=[1, 1, 1, 2, 1])
    s, ev = compare(y, t)
    assert s["days_both"] == 4 and s["only_yfinance"] == 1 and s["only_tiingo"] == 0  # 01-08 yes, 01-09 out of range
    assert s["close_within_0.5%"] == 0.75 and s["worst_close_date"] == "2015-01-06"
    assert s["split_mismatches"] == 0 and s["volume_ratio_median"] == 1.0
    assert [(e["kind"], e["date"], e["yfinance"], e["tiingo"]) for e in ev] == [
        ("dividend", "2015-01-05", 0.5, None), ("dividend", "2015-01-06", None, 0.5)]
