import pandas as pd

from pipeline.ticker_map import company_spells, covers, norm


def test_norm_matches_sec_and_wikipedia_spellings():
    assert norm("Walt Disney Co/DE/") == norm("The Walt Disney Company") == "WALT DISNEY"
    assert norm("AETNA INC /PA/") == norm("Aetna") == "AETNA"
    assert norm("Alphabet Inc. (Class A)") == "ALPHABET"
    assert norm("Johnson & Johnson") == "JOHNSON AND JOHNSON"


def test_covers_rejects_company_that_started_filing_mid_spell():
    alphabet = pd.Series({"first_filed": "2015-10-02", "last_filed": "2026-10-23"})
    assert not covers(alphabet, "2006-04-03", None)  # GOOGL spell: Google Inc. filed until 2015
    assert covers(alphabet, "2016-01-04", None)


def test_company_spells_merge_renames_but_not_gaps():
    m = pd.DataFrame([
        {"ticker": "SYMC", "start": "2003-03-31", "end": "2019-11-05", "cik": 1.0},
        {"ticker": "NLOK", "start": "2019-11-05", "end": "2022-11-08", "cik": 1.0},
        {"ticker": "GEN", "start": "2022-11-08", "end": None, "cik": 1.0},
        {"ticker": "X", "start": "2010-01-04", "end": "2011-01-03", "cik": 2.0},
        {"ticker": "X", "start": "2013-01-02", "end": None, "cik": 2.0},
    ])
    cs = company_spells(m)
    one = cs[cs["cik"] == 1]
    assert len(one) == 1
    assert (one["start"].iloc[0], one["tickers"].iloc[0]) == ("2003-03-31", "SYMC,NLOK,GEN")
    assert pd.isna(one["end"].iloc[0])
    assert len(cs[cs["cik"] == 2]) == 2
