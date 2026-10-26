import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

from pipeline.fundamentals import SCHEMA
from pipeline.identity import check


def prices(rows):
    df = pd.DataFrame(rows)
    df["year"] = df["date"].str[:4].astype(int)
    return df


def days(symbol, source, start, end, close, divs=()):
    out = []
    for d in pd.bdate_range(start, end).strftime("%Y-%m-%d"):
        out.append({"symbol": symbol, "source": source, "date": d, "close": close,
                    "dividend": 0.05 if d in divs else 0.0})
    return out


def fact(cik, tag, unit, start, end, value, filed):
    return {"cik": cik, "taxonomy": "us-gaap", "tag": tag, "unit": unit, "start": start, "end": end, "value": value,
            "fy": None, "fp": "FY", "form": "10-K", "filed": filed, "accn": f"{cik}-{end}", "frame": None}


def test_identity_resolves_a_shared_series_and_catches_a_wrong_price_level(tmp_path):
    quarterly = [f"{y}-{m}-15" for y in (2011, 2012, 2013) for m in ("02", "05", "08", "11")]
    px = prices(days("AGN", "tiingo", "2011-01-03", "2013-12-31", 100.0, quarterly) +  # Allergan Inc's series
                days("BIG", "wiki", "2011-01-03", "2013-12-31", 10.0))                  # a $10 stock
    ds.write_dataset(pa.Table.from_pandas(px, preserve_index=False), tmp_path / "prices", format="parquet",
                     partitioning=["year"], partitioning_flavor="hive")
    facts = []
    for y in (2011, 2012, 2013):
        facts.append(fact(1, "CommonStockDividendsPerShareDeclared", "USD/shares", f"{y}-01-01", f"{y}-12-31", 0.20, f"{y + 1}-02-20"))
        # BIG: public float 400M at a $10 price would need ~50M shares; it reports 50M: fits
        facts.append(fact(3, "EntityPublicFloat", "USD", None, f"{y}-06-30", 4.0e8, f"{y + 1}-02-20"))
        facts.append(fact(3, "EntityCommonStockSharesOutstanding", "shares", None, f"{y + 1}-02-01", 5.0e7, f"{y + 1}-02-20"))
        # WRONG: same $10 series but the company is worth $40B: float/(price x shares) is far below 0.2
        facts.append(fact(4, "EntityPublicFloat", "USD", None, f"{y}-06-30", 4.0e10, f"{y + 1}-02-20"))
        facts.append(fact(4, "EntityCommonStockSharesOutstanding", "shares", None, f"{y + 1}-02-01", 5.0e10, f"{y + 1}-02-20"))
    pa.parquet.write_table(pa.Table.from_pylist(facts, SCHEMA), tmp_path / "facts.parquet")
    segs = pd.DataFrame([
        {"ticker": "AGN", "spell_start": "1996-01-02", "start": "1996-01-02", "end": "2015-03-23", "cik": 1.0},
        {"ticker": "ACT", "spell_start": "1999-04-12", "start": "1999-04-12", "end": "2015-03-23", "cik": 2.0},
        {"ticker": "BIG", "spell_start": "1998-01-20", "start": "1998-01-20", "end": "2014-01-01", "cik": 3.0},
        {"ticker": "WRONG", "spell_start": "2005-01-03", "start": "2005-01-03", "end": "2014-01-01", "cik": 4.0},
    ])
    pm = pd.DataFrame([
        {"label": "AGN", "cik": 1, "source": "tiingo", "symbol": "AGN", "from": "2011-01-03", "to": "2014-01-01"},
        {"label": "ACT", "cik": 2, "source": "tiingo", "symbol": "AGN", "from": "2011-01-03", "to": "2014-01-01"},
        {"label": "BIG", "cik": 3, "source": "wiki", "symbol": "BIG", "from": "2011-01-03", "to": "2014-01-01"},
        {"label": "WRONG", "cik": 4, "source": "wiki", "symbol": "BIG", "from": "2011-01-03", "to": "2014-01-01"},
        {"label": "FRC", "cik": None, "source": "tiingo", "symbol": "FRCB", "from": "2019-01-02", "to": "2023-05-04"},
    ])
    v = check(pm, tmp_path / "prices", tmp_path / "facts.parquet", segs, "2026-10-23").set_index("label")
    assert v.loc["AGN", "dividends"] == "pass" and v.loc["AGN", "verdict"] == "pass"  # keeps the shared series
    assert bool(v.loc["ACT", "shared"]) and v.loc["ACT", "verdict"] == "fail"          # no proof it's Actavis
    assert v.loc["BIG", "float"] == "pass"
    assert v.loc["WRONG", "float"] == "fail" and v.loc["WRONG", "verdict"] == "fail"
    assert v.loc["FRC", "verdict"] == "unverifiable"  # no SEC company record: nothing to check against


def test_late_starting_vendor_series_must_pass_not_just_avoid_failing(tmp_path):
    from pipeline.prices import apply_identity
    px = prices(days("PARA", "tiingo", "2021-02-12", "2021-12-31", 10.0) + days("OLDX", "wiki", "2010-01-04", "2012-12-31", 50.0))
    (tmp_path / "market" / "fundamentals").mkdir(parents=True)
    ds.write_dataset(pa.Table.from_pandas(px, preserve_index=False), tmp_path / "market" / "prices", format="parquet",
                     partitioning=["year"], partitioning_flavor="hive")
    pa.parquet.write_table(pa.Table.from_pylist([], SCHEMA), tmp_path / "market" / "fundamentals" / "facts.parquet")
    pd.DataFrame([
        {"ticker": "VIAC", "spell_start": "2019-12-05", "start": "2019-12-05", "end": "2022-02-17", "cik": 9.0},
        {"ticker": "OLDX", "spell_start": "2005-01-03", "start": "2005-01-03", "end": "2013-01-01", "cik": 8.0},
    ]).to_parquet(tmp_path / "market" / "ticker_map.parquet")
    pm = pd.DataFrame([  # nothing to check either against: both unverifiable
        {"label": "VIAC", "cik": 9, "source": "tiingo", "symbol": "PARA", "from": "2021-02-12", "to": "2022-02-17", "late": True},
        {"label": "OLDX", "cik": 8, "source": "wiki", "symbol": "OLDX", "from": "2010-01-04", "to": "2013-01-01", "late": False},
    ])
    gaps = pd.DataFrame(columns=["label", "cik", "source", "symbols", "gap_from", "gap_to", "reason", "rejected"])
    keep, g, idv = apply_identity(tmp_path, pm, gaps, "2026-10-23")
    assert list(keep["label"]) == ["OLDX"]  # kept, and listed as unverifiable for review
    assert list(zip(g["label"], g["reason"], g["rejected"])) == [
        ("VIAC", "late-starting series not proven to be this company", "PARA")]
