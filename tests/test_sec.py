import json
import zipfile

from pipeline.sec import companies


def block(dates, forms, items=None):
    n = len(dates)
    return {"accessionNumber": [f"acc-{d}-{f}" for d, f in zip(dates, forms)], "filingDate": dates, "form": forms,
            "reportDate": dates, "acceptanceDateTime": [f"{d}T16:30:00.000Z" for d in dates],
            "items": items or [""] * n}


def filing(cik, name, sic, dates, forms, pages=(), items=None):
    return {"cik": str(cik), "name": name, "sic": sic, "sicDescription": "x", "ownerOrg": "o",
            "tickers": ["T"], "exchanges": ["NYSE"], "formerNames": [{"name": "OLD", "from": "2001", "to": "2005"}],
            "filings": {"recent": block(dates, forms, items), "files": [{"filingFrom": p} for p in pages]}}


def test_companies_reads_filing_and_periodic_ranges_including_older_pages(tmp_path):
    zp = tmp_path / "s.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("CIK0000000001.json", json.dumps(filing(
            1, "CO", "3571", ["2020-01-02", "2015-05-01", "2010-05-01"], ["4", "10-Q", "8-K"], ["1996-03-01"],
            ["", "", "2.02,9.01"])))
        z.writestr("CIK0000000001-submissions-001.json", json.dumps(
            block(["2009-02-01", "1997-02-01", "1996-03-01"], ["8-K", "10-K", "S-1"], ["5.02", "", ""])))
        z.writestr("CIK0000000002.json", json.dumps(filing(2, "PERSON", "", ["2020-01-02"], ["4"])))  # dropped
        z.writestr("CIK0000000004.json", json.dumps(filing(4, "EX-CO NOW FUND", "", ["2016-01-02"], ["10-K"])))
        z.writestr("placeholder.txt", "not a company")  # SEC ships one of these: skipped
    c, e = companies(zp)
    assert sorted(c["cik"]) == [1, 4]  # no SIC but filed a 10-K (Yahoo after it became Altaba): kept
    row = c[c["cik"] == 1].iloc[0]
    assert (row["first_filed"], row["last_filed"]) == ("1996-03-01", "2020-01-02")
    assert (row["first_periodic"], row["last_periodic"]) == ("1997-02-01", "2015-05-01")
    assert json.loads(row["former_names"])[0]["name"] == "OLD"
    # 8-Ks since 2009, from the recent block and the older page, with their item numbers
    assert list(zip(e["filed"], e["items"])) == [("2009-02-01", "5.02"), ("2010-05-01", "2.02,9.01")]


def test_unreadable_company_file_or_page_raises(tmp_path):
    for name, body in [("CIK0000000003.json", ""), ("CIK0000000003-submissions-001.json", "{}")]:
        zp = tmp_path / "s.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr(name, body)
        try:
            companies(zp)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {name}")


def test_download_restarts_a_body_cut_off_midway(tmp_path, monkeypatch):
    import requests
    from pipeline import sec

    class Cut:
        headers = {"Last-Modified": "Thu"}

        def __init__(self, ok):
            self.ok = ok

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, n):
            yield b"part"
            if not self.ok:
                raise requests.exceptions.ChunkedEncodingError("IncompleteRead")
            yield b"-rest"

    calls = []
    monkeypatch.setattr(sec, "get", lambda url, **kw: calls.append(1) or Cut(len(calls) > 1))
    monkeypatch.setattr(sec.time, "sleep", lambda s: None)
    assert sec.download("u", tmp_path / "f.zip") == "Thu"
    assert (tmp_path / "f.zip").read_bytes() == b"part-rest" and len(calls) == 2
