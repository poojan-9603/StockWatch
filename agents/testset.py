"""The labeled agent test set (PLAN Step 4 "Measurement"): 8-Ks labeled free by their item number.

Only plain 8-Ks filed 2013 to 2019 (hard rule 1) whose one item, besides 9.01 exhibits, is a label below. A fixed
seed picks the same number of each label. The text is the 8-K document itself (not its exhibits), cleaned to
visible text with item headings masked, fetched only through pipeline.sec.get (User-Agent and rate limit).

Usage: python -m agents.testset FILINGS_8K_PARQUET OUT.jsonl
  writes one JSON line per document: id, cik, accession, filed, items, label, text
"""
import json
import random
import re
import sys

import pandas as pd

from agents import docs
from pipeline import sec
from quant import lock

LABELS = {"1.01": "material agreement", "1.03": "bankruptcy or receivership",
          "2.01": "acquisition or disposition completed", "2.02": "results of operations",
          "4.02": "non-reliance on past financial statements",
          "5.02": "director or officer departure or appointment", "8.01": "other events"}
# Each 8-K heads its sections with the item number and SEC title ("Item 2.02 Results of Operations and Financial
# Condition"), which would hand a model its label. Both are masked; the body has to carry the answer.
TITLES = {"1.01": r"entry into a material definitive agreement", "1.03": r"bankruptcy or receivership",
          "2.01": r"completion of acquisition or disposition of assets",
          "2.02": r"results of operations and financial condition",
          "4.02": r"non-?reliance on previously issued financial statements(,? or a related audit report or "
                  r"completed interim review)?",
          "5.02": r"departure of directors or (certain )?officers|election of directors|appointment of (certain )?"
                  r"officers|compensatory arrangements of certain officers",
          "8.01": r"other events", "9.01": r"financial statements and exhibits"}
ITEM = re.compile(r"\bitems?\s*\d{1,2}\s*\.\s*\d{2}\b(?:\s*(?:,|and|&)\s*\d{1,2}\s*\.\s*\d{2}\b)*", re.I)
MASK = "[heading removed]"
# Looser than the titles, to measure what still gives the label away after masking (headings that skip "Item",
# shortened titles, or the body saying it outright)
KEYS = {"1.01": r"material definitive agreement", "1.03": r"bankruptcy|receivership",
        "2.01": r"completion of (the )?acquisition|disposition of assets", "2.02": r"results of operations",
        "4.02": r"non-?reliance", "5.02": r"departure of|election of directors|appointment of", "8.01": r"other events"}
START, END = "2013-01-01", "2020-01-01"


def _words(pattern):
    return re.compile(r"\b(?:" + pattern.replace(" ", r"\s+") + r")\b", re.I)


def _title(item):
    return _words(TITLES[item])


def mask(text):
    """The text with every item number and SEC item title replaced by MASK."""
    text = ITEM.sub(MASK, text)
    for item in TITLES:
        text = _title(item).sub(MASK, text)
    return text


def leaks(text, item):
    """True if the label's item number (not inside an amount like $2.02 or 12.025) or a key phrase shows."""
    return bool(re.search(r"(?<![\d.$])" + re.escape(item) + r"(?![\d])", text) or _words(KEYS[item]).search(text))


PER_LABEL = 14
SEED = 20130101


def label(items):
    """The label of an 8-K whose only item besides 9.01 is one of LABELS, else None."""
    found = {i.strip() for i in (items or "").split(",")} - {"", "9.01"}
    return found.pop() if len(found) == 1 and found <= LABELS.keys() else None


def sample(filings, per_label=PER_LABEL, seed=SEED):
    """per_label filings of each label, chosen with a fixed seed from the 8-K table (pipeline.sec.companies)."""
    f = filings[(filings["form"] == "8-K") & (filings["filed"] >= START) & (filings["filed"] < END)]
    lock.check(f["filed"])
    f = f.assign(label=f["items"].map(label)).dropna(subset=["label"]).sort_values("accession")
    rng, picks = random.Random(seed), []
    for lab in sorted(LABELS):
        rows = list(f.index[f["label"] == lab])
        if len(rows) < per_label:
            raise ValueError(f"only {len(rows)} filings labeled {lab}, need {per_label}")
        picks += rng.sample(rows, per_label)
    return f.loc[picks].reset_index(drop=True)


def main_document(submission):
    """Visible text of the 8-K document in a full submission .txt (exhibits skipped)."""
    for d in re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", submission, re.S):
        if re.search(r"<TYPE>\s*8-K\s", d):
            m = re.search(r"<TEXT>(.*?)</TEXT>", d, re.S)
            body = m.group(1) if m else ""
            html = re.search(r"<(html|body|div|p|table|font|pre|br)\b", body, re.I)
            return docs.visible_text(body) if html else docs.clean(body)
    raise ValueError("no 8-K document in the submission")


def fetch(cik, accession):
    r = sec.get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}.txt")
    try:
        raw = r.content.decode("utf-8")
    except UnicodeDecodeError:
        raw = r.content.decode("cp1252", errors="strict")
    text = main_document(raw)
    if not text:
        raise ValueError(f"8-K {accession} has no visible text")
    return text


def build(filings, out_path, per_label=PER_LABEL):
    rows = sample(filings, per_label)
    before = after = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for r in rows.itertuples():
            raw = fetch(r.cik, r.accession)
            text = mask(raw)
            before, after = before + leaks(raw, r.label), after + leaks(text, r.label)
            out.write(json.dumps({"id": f"8k-{r.accession}", "cik": int(r.cik), "accession": r.accession,
                                  "filed": str(r.filed), "items": r.items, "label": r.label,
                                  "text": text}) + "\n")
    print(f"test set: {len(rows)} 8-Ks, {per_label} per label, written to {out_path}")
    print(f"label's item number or key phrase shows: {before} of {len(rows)} before masking, {after} after")


if __name__ == "__main__":
    build(pd.read_parquet(sys.argv[1]), sys.argv[2])
