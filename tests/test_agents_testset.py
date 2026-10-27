"""Agent test set: balanced, seeded, 2013 to 2019 only, single-item labels, text from the 8-K document only."""
import json

import pandas as pd
import pytest

from agents import testset
from quant import lock


def filings():
    rows = []
    for i, items in enumerate(["1.01,9.01", "1.03", "2.01,9.01", "2.02,9.01", "4.02", "5.02", "8.01,9.01"] * 3):
        for year in (2012, 2014, 2018, 2020):
            rows.append({"cik": i, "accession": f"0000-{year}-{i:04d}", "form": "8-K", "filed": f"{year}-06-01",
                         "items": items})
    rows += [{"cik": 99, "accession": "0000-2015-9999", "form": "8-K", "filed": "2015-06-01", "items": "1.01,2.03"},
             {"cik": 98, "accession": "0000-2015-9998", "form": "8-K/A", "filed": "2015-06-01", "items": "1.03"}]
    return pd.DataFrame(rows)


def test_sample_is_balanced_seeded_and_never_touches_test_years():
    s = testset.sample(filings(), per_label=4)
    assert s["label"].value_counts().to_dict() == dict.fromkeys(testset.LABELS, 4)
    assert s["filed"].between("2013-01-01", "2019-12-31").all() and (s["form"] == "8-K").all()
    assert s.equals(testset.sample(filings(), per_label=4))
    assert not s.equals(testset.sample(filings(), per_label=4, seed=1))
    with pytest.raises(ValueError):
        testset.sample(filings(), per_label=7)


def test_sample_respects_the_lock(monkeypatch):
    monkeypatch.setattr(testset, "END", "2021-01-01")
    with pytest.raises(PermissionError):
        testset.sample(filings(), per_label=1)
    assert not lock.unlocked()


SUBMISSION = """<SEC-DOCUMENT>
<DOCUMENT>
<TYPE>8-K
<TEXT>
<html><body><p>Item 2.02 Results of Operations</p><p style="display:none">x</p><p>See Exhibit 99.1.</p></body></html>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-99.1
<TEXT><html><body><p>Press release</p></body></html></TEXT>
</DOCUMENT>
</SEC-DOCUMENT>"""


def test_build_fetches_through_sec_get_and_writes_json_lines(tmp_path, monkeypatch):
    urls = []

    class Response:
        content = SUBMISSION.encode()

    monkeypatch.setattr(testset.sec, "get", lambda url: urls.append(url) or Response())
    testset.build(filings(), tmp_path / "t.jsonl", per_label=1)
    lines = [json.loads(x) for x in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(urls) == 7
    first = lines[0]
    assert set(first) == {"id", "cik", "accession", "filed", "items", "label", "text"}
    assert first["id"] == "8k-" + first["accession"]
    assert first["text"] == "[heading removed] Results of Operations\nSee Exhibit 99.1."
    acc = first["accession"]
    assert urls[0] == f"https://www.sec.gov/Archives/edgar/data/{first['cik']}/{acc.replace('-', '')}/{acc}.txt"


def test_main_document_needs_an_8k():
    with pytest.raises(ValueError):
        testset.main_document("<DOCUMENT>\n<TYPE>EX-99.1\n<TEXT>x</TEXT>\n</DOCUMENT>")
    plain = "<DOCUMENT>\n<TYPE>8-K\n<TEXT>\nPlain  text​ 8-K\n</TEXT>\n</DOCUMENT>"
    assert testset.main_document(plain) == "Plain text 8-K"


def test_mask_removes_item_numbers_and_titles_but_not_amounts():
    raw = ("ITEM 5.02. Departure of Directors or Certain Officers; Election of Directors\n"
           "Items 9.01 and 2.02 Results of Operations and Financial Condition. EPS was $2.02.\n"
           "Item 8.01  Other\n Events")
    text = testset.mask(raw)
    assert "5.02" not in text and "2.02 " not in text and "Departure" not in text and "Election" not in text
    assert "Results of Operations" not in text and "Other" not in text and "$2.02" in text
    assert testset.leaks(raw, "5.02") and not testset.leaks(text, "5.02")
    assert not testset.leaks("EPS was $2.02 and 12.025", "2.02")
    assert testset.leaks("2.02 Results of operations", "2.02")
    assert testset.mask("Item 2.02Results of Operations and Financial Condition. On") == "[heading removed][heading removed]. On"
