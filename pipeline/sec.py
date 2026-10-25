"""SEC EDGAR downloads and the companies table built from submissions.zip.

Every request carries the SEC_USER_AGENT secret and stays under SEC's 10 requests a second.

Usage: python -m pipeline.sec OUT_DIR
  writes OUT_DIR/raw/sec/{submissions.zip, company_tickers.json} and OUT_DIR/market/companies.parquet
"""
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

SUBMISSIONS_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_last = 0.0


def get(url, **kw):
    """GET with SEC's User-Agent, about 7 requests a second at most, and 4 tries with backoff."""
    global _last
    for attempt in range(4):
        time.sleep(max(0.0, 0.15 - (time.monotonic() - _last)))  # ponytail: single-process limiter
        _last = time.monotonic()
        try:
            r = requests.get(url, headers={"User-Agent": os.environ["SEC_USER_AGENT"]}, timeout=600, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            print(f"SEC request failed (try {attempt + 1}/4): {url}: {e}", file=sys.stderr)
            if attempt == 3:
                raise
            time.sleep(2 ** attempt * 5)


def download(url, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with get(url, stream=True) as r, open(path, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    return r.headers.get("Last-Modified")


PERIODIC = {"10-K", "10-K405", "10-KSB", "10-KT", "10-Q", "10-QSB", "10-QT", "20-F", "40-F"}


def periodic_range(filings):
    """(first, last) filing date of 10-K/10-Q style reports in a column-wise filings block."""
    dates = [d for d, f in zip(filings["filingDate"], filings["form"]) if f in PERIODIC]
    return (min(dates), max(dates)) if dates else (None, None)


def companies(zip_path):
    """One row per filer with a SIC code or 10-K/10-Q reports (operating companies; skips people and most funds).

    Reads the CIKnnnnnnnnnn.json company files plus their CIKnnnnnnnnnn-submissions-NNN.json pages of older
    filings (needed for the first periodic report of companies with over 1,000 filings). Skips anything
    else (the zip holds a placeholder.txt). An unreadable company file or page raises: it would silently
    drop or shorten a company.
    """
    rows, periodic = [], {}
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            m = re.fullmatch(r"CIK(\d{10})(-submissions-\d+)?\.json", name)
            if not m:
                continue
            try:
                d = json.loads(z.read(name))
                if m.group(2):  # page of older filings: same columns as filings.recent
                    lo, hi = periodic_range(d)
                    if lo:
                        old = periodic.get(int(m.group(1)), (lo, hi))
                        periodic[int(m.group(1))] = (min(old[0], lo), max(old[1], hi))
                    continue
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"unreadable company file {name} in submissions.zip: {e!r}") from e
            recent = d["filings"]["recent"]
            if not d.get("sic") and not periodic_range(recent)[0] and not d["filings"].get("files"):
                continue  # people and funds; a filer that lost its SIC but filed 10-Ks (Yahoo) is kept below
            dates = recent["filingDate"] + [p["filingFrom"] for p in d["filings"].get("files", [])]
            lo, hi = periodic_range(recent)
            if lo:
                old = periodic.get(int(d["cik"]), (lo, hi))
                periodic[int(d["cik"])] = (min(old[0], lo), max(old[1], hi))
            rows.append({
                "cik": int(d["cik"]),
                "name": d["name"],
                "former_names": json.dumps(d.get("formerNames", [])),
                "tickers": ",".join(d.get("tickers", [])),
                "exchanges": ",".join(e or "" for e in d.get("exchanges", [])),
                "sic": d["sic"],
                "sic_description": d.get("sicDescription"),
                "owner_org": d.get("ownerOrg"),
                "first_filed": min(dates) if dates else None,
                "last_filed": max(recent["filingDate"]) if recent["filingDate"] else None,
            })
    c = pd.DataFrame(rows)
    c["first_periodic"] = c["cik"].map(lambda k: periodic.get(k, (None, None))[0])
    c["last_periodic"] = c["cik"].map(lambda k: periodic.get(k, (None, None))[1])
    return c[(c["sic"] != "") | c["first_periodic"].notna()].reset_index(drop=True)


def main(out_dir):
    out = Path(out_dir)
    zip_path = out / "raw" / "sec" / "submissions.zip"
    # Raw first (PLAN rule 5). Only the latest copy is kept in R2 (M1 decision 4).
    print("submissions.zip Last-Modified:", download(SUBMISSIONS_URL, zip_path))
    print("company_tickers.json Last-Modified:", download(TICKERS_URL, out / "raw" / "sec" / "company_tickers.json"))
    t = time.monotonic()
    c = companies(zip_path)
    (out / "market").mkdir(parents=True, exist_ok=True)
    c.to_parquet(out / "market" / "companies.parquet", index=False)
    print(f"companies: {len(c)} filers with a SIC code or 10-K/10-Q reports, parsed in {time.monotonic() - t:.0f}s")


if __name__ == "__main__":
    main(sys.argv[1])
