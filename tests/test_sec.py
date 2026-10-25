import json
import zipfile

from pipeline.sec import companies


def filing(cik, name, sic, dates, pages=()):
    return {"cik": str(cik), "name": name, "sic": sic, "sicDescription": "x", "ownerOrg": "o",
            "tickers": ["T"], "exchanges": ["NYSE"], "formerNames": [{"name": "OLD", "from": "2001", "to": "2005"}],
            "filings": {"recent": {"filingDate": dates, "form": ["10-K"] * len(dates)},
                        "files": [{"filingFrom": p} for p in pages]}}


def test_companies_keeps_sic_filers_and_reads_filing_range(tmp_path):
    zp = tmp_path / "s.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("CIK0000000001.json", json.dumps(filing(1, "CO", "3571", ["2020-01-02", "2010-05-01"], ["1996-03-01"])))
        z.writestr("CIK0000000002.json", json.dumps(filing(2, "PERSON", "", ["2020-01-02"])))  # no SIC: dropped
        z.writestr("CIK0000000001-submissions-001.json", "{}")  # paging file: skipped
    c = companies(zp)
    assert list(c["cik"]) == [1]
    row = c.iloc[0]
    assert (row["first_filed"], row["last_filed"]) == ("1996-03-01", "2020-01-02")
    assert json.loads(row["former_names"])[0]["name"] == "OLD"
