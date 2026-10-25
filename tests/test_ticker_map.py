import json

import pandas as pd

from pipeline.ticker_map import covers, norm, predecessor, stem, stem_index


def test_norm_matches_sec_and_wikipedia_spellings():
    assert norm("Walt Disney Co/DE/") == norm("The Walt Disney Company") == "WALT DISNEY"
    assert norm("AETNA INC /PA/") == norm("Aetna") == "AETNA"
    assert norm("Alphabet Inc. (Class A)") == "ALPHABET"
    assert norm("Kohl's") == norm("KOHLS Corp") == "KOHLS"


def test_stem_lines_up_holding_company_successors():
    assert stem("ExxonMobil Holdings Corp") == stem("EXXON MOBIL CORP") == "EXXONMOBIL"
    assert stem("Cigna Group") == stem("CIGNA CORP")


def test_covers_rejects_company_that_started_reporting_mid_spell():
    alphabet = pd.Series({"first_periodic": "2015-10-29", "last_periodic": "2026-07-23"})
    assert not covers(alphabet, "2006-04-03", None)  # GOOGL: Google Inc. reported until 2015
    assert covers(alphabet, "2016-01-04", None)


def test_predecessor_finds_same_name_company_that_stopped_reporting_at_the_switch():
    companies = pd.DataFrame([
        {"cik": 1, "name": "EATON CORP", "former_names": "[]", "first_periodic": "1994-03-28", "last_periodic": "2012-10-31"},
        {"cik": 2, "name": "Eaton Corp plc", "former_names": "[]", "first_periodic": "2012-11-14", "last_periodic": "2026-07-31"},
        {"cik": 3, "name": "EATON VANCE CORP", "former_names": "[]", "first_periodic": "1995-01-01", "last_periodic": "2012-11-01"},
    ])
    co = companies.set_index("cik", drop=False)
    assert predecessor(co.loc[2], co, stem_index(companies)).cik == 1
    assert predecessor(co.loc[1], co, stem_index(companies)) is None
