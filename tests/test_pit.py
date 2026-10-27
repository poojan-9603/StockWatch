"""The point-in-time test (PLAN quality checks): no calculation may use a number before its filing date."""
import pandas as pd

from quant import lock
from quant.pit import as_of, first_available


def f(cik, tag, start, end, value, filed):
    return {"cik": cik, "tag": tag, "start": start, "end": end, "value": value, "form": "10-K", "filed": filed}


FACTS = pd.DataFrame([
    f(1, "Assets", None, "2014-12-31", 100.0, "2015-02-20"),
    f(1, "Assets", None, "2015-12-31", 120.0, "2016-02-19"),     # not public until 2016-02-19
    f(1, "Assets", None, "2014-12-31", 99.0, "2016-02-19"),      # restated in the 2015 10-K
    f(1, "NetIncomeLoss", "2015-01-01", "2015-12-31", 10.0, "2016-02-19"),
    f(1, "NetIncomeLoss", "2015-10-01", "2015-12-31", 3.0, "2016-02-19"),  # a quarter, not a year
    f(2, "Revenues", "2015-01-01", "2015-12-31", 50.0, "2016-03-01"),
    f(3, "SalesRevenueNet", "2015-01-01", "2015-12-31", 70.0, "2016-03-01"),
])


def test_nothing_is_used_before_its_filing_date():
    v = as_of(FACTS, "2016-02-18", ["Assets"], annual=False)
    assert v["Assets"][1] == 100.0          # the 2015 balance sheet isn't public yet
    v = as_of(FACTS, "2016-02-19", ["Assets"], annual=False)
    assert v["Assets"][1] == 120.0          # now it is: latest period wins


def test_annual_durations_and_fallback_chain():
    v = as_of(FACTS, "2016-06-30", ["NetIncomeLoss", "Revenues", "SalesRevenueNet"], annual=True)
    assert v["NetIncomeLoss"][1] == 10.0    # the quarter is ignored
    rev = first_available(v, ["Revenues", "SalesRevenueNet"])
    assert rev.to_dict() == {2: 50.0, 3: 70.0}


def test_stale_values_count_as_missing():
    assert "Assets" not in as_of(FACTS, "2018-06-30", ["Assets"], annual=False, max_age=550)


def test_test_years_are_locked(monkeypatch):
    monkeypatch.delenv("STOCKWATCH_UNLOCK_TEST", raising=False)
    try:
        as_of(FACTS, "2020-01-02", ["Assets"])
    except PermissionError:
        pass
    else:
        raise AssertionError("a 2020 date got through the lock")
    monkeypatch.setenv("STOCKWATCH_UNLOCK_TEST", lock.UNLOCK_PHRASE)
    as_of(FACTS, "2020-01-02", ["Assets"])  # only with the user's unlock
