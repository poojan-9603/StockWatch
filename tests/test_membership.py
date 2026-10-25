import pandas as pd

from pipeline.membership import clean_ticker, extend, fja_spells

FJA = "date,tickers\n" + "\n".join([
    '2010-01-04,"' + ",".join(["AAA", "BBB"] + [f"X{i}" for i in range(498)]) + '"',
    '2011-03-01,"' + ",".join(["AAA", "CCC"] + [f"X{i}" for i in range(498)]) + '"',
    '2012-06-01,"' + ",".join(["BBB", "CCC"] + [f"X{i}" for i in range(498)]) + '"',
])


def spell(df, ticker):
    rows = df.loc[df["ticker"] == ticker, ["start", "end"]].itertuples(index=False)
    return [(s, None if pd.isna(e) else e) for s, e in rows]


def test_fja_spells_track_entry_exit_and_reentry():
    s, last = fja_spells(FJA)
    assert last == "2012-06-01"
    assert spell(s, "AAA") == [("2010-01-04", "2012-06-01")]
    assert spell(s, "BBB") == [("2010-01-04", "2011-03-01"), ("2012-06-01", None)]
    assert spell(s, "CCC") == [("2011-03-01", None)]


def test_extend_closes_open_spell_and_adds_new_one():
    s, last = fja_spells(FJA)
    changes = pd.DataFrame([{"date": "2012-01-01", "added": "OLD", "removed": None},  # before fja's last: ignored
                            {"date": "2012-07-01", "added": "DDD", "removed": "CCC"}])
    out = extend(s, changes, after=last)
    assert spell(out, "CCC") == [("2011-03-01", "2012-07-01")]
    assert spell(out, "DDD") == [("2012-07-01", None)]
    assert spell(out, "OLD") == []


def test_extend_refuses_removal_without_open_spell():
    s, last = fja_spells(FJA)
    try:
        extend(s, pd.DataFrame([{"date": "2012-07-01", "added": None, "removed": "AAA"}]), after=last)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_clean_ticker_strips_wikipedia_markup():
    assert clean_ticker("ITT |") == "ITT"
    assert clean_ticker(" brk.b ") == "BRK.B"
    assert clean_ticker(float("nan")) is None
