import pandas as pd

from pipeline.reconcile import entities, memberships, unreviewed


def seg(ticker, spell_start, start, end, cik, link=None):
    return {"ticker": ticker, "spell_start": spell_start, "start": start, "end": end, "cik": cik, "link": link}


SEGS = pd.DataFrame([
    seg("GOOGL", "2006-04-03", "2006-04-03", "2015-10-29", 1.0),  # Google Inc. ...
    seg("GOOGL", "2006-04-03", "2015-10-29", None, 2.0, "successor"),  # ... continues as Alphabet
    seg("FOXA", "2004-12-20", "2004-12-20", "2019-03-19", 3.0),  # 21st Century Fox ...
    seg("FOXA", "2004-12-20", "2019-03-19", None, 4.0, "new_company"),  # ... ticker goes to Fox Corp
    seg("SYMC", "2003-03-31", "2003-03-31", "2019-11-05", 5.0),  # Symantec renamed: next spell, same CIK
    seg("NLOK", "2019-11-05", "2019-11-05", None, 5.0),
])


def test_successor_joins_company_new_company_does_not():
    ent = entities(SEGS)
    assert ent[1] == ent[2]
    assert ent[3] != ent[4]


def test_memberships_split_at_new_company_and_merge_renames():
    ent = entities(SEGS)
    mem = memberships(SEGS, ent)
    assert [(s, e) for s, e, _ in mem[ent[1]]] == [("2006-04-03", None)]
    assert [(s, e) for s, e, _ in mem[ent[3]]] == [("2004-12-20", "2019-03-19")]
    assert [(s, e) for s, e, _ in mem[ent[4]]] == [("2019-03-19", None)]
    assert [(s, e, t) for s, e, t in mem[ent[5]]] == [("2003-03-31", None, {"SYMC", "NLOK"})]


def test_unreviewed_flags_new_disagreements_regardless_of_side():
    d = pd.DataFrame([{"date": "2012-05-01", "side": "ours_only", "kind": "add", "tickers": "PSX"},
                      {"date": "2030-01-02", "side": "ours_only", "kind": "add", "tickers": "NEW"}])
    reviewed = pd.DataFrame([{"date": "2012-05-01", "side": "fja_only", "kind": "add", "tickers": "PSX"}])
    assert list(unreviewed(d, reviewed)["tickers"]) == ["NEW"]
