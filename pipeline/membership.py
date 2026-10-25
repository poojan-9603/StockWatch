"""S&P 500 membership spells, by ticker, from fja05680/sp500 snapshots, extended with Wikipedia's newer changes.

Spells are [start, end): start is the first snapshot date a ticker is listed, end is the first date it isn't
(None while still a member). Tickers are as each source spells them; mapping to CIK, and the by-company
reconciliation of fja against Wikipedia, come in M1 step 3.

Usage: python -m pipeline.membership OUT_DIR
"""
import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

FJA_URL = ("https://raw.githubusercontent.com/fja05680/sp500/master/"
           "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv")
WIKI_CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
WIKI_CURRENT_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
HEADERS = {"User-Agent": "StockWatch membership backfill (personal research)"}


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.text


def clean_ticker(t):
    """Wikipedia cells carry stray markup like 'ITT |'. Missing cells (NaN) become None."""
    return t.split()[0].upper() if isinstance(t, str) and t.split() else None


def fja_spells(text):
    """Returns (spells, date of the last snapshot)."""
    spells, open_, date = [], {}, ""
    for row in csv.DictReader(io.StringIO(text)):
        if row["date"] <= date:
            raise ValueError(f"fja snapshots out of order at {row['date']}")
        date, members = row["date"], set(row["tickers"].split(","))
        if not 480 <= len(members) <= 520:
            raise ValueError(f"fja snapshot {date} has {len(members)} members")
        for t in members - open_.keys():
            open_[t] = date
        for t in open_.keys() - members:
            spells.append({"ticker": t, "start": open_.pop(t), "end": date, "source": "fja"})
    spells += [{"ticker": t, "start": s, "end": None, "source": "fja"} for t, s in open_.items()]
    return pd.DataFrame(spells).sort_values(["start", "ticker"], ignore_index=True), date


def wiki_changes(html):
    t = next(t for t in pd.read_html(io.StringIO(html)) if "Effective Date" in str(t.columns[0]))
    out = pd.DataFrame({
        "date": pd.to_datetime(t.iloc[:, 0], errors="coerce", format="mixed"),
        "added": t.iloc[:, 1].map(clean_ticker),
        "removed": t.iloc[:, 3].map(clean_ticker),
    })
    if out["date"].isna().any():
        raise ValueError(f"{int(out['date'].isna().sum())} Wikipedia change rows have unreadable dates")
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out.sort_values("date", ignore_index=True)


def wiki_current(html):
    t = next(t for t in pd.read_html(io.StringIO(html)) if "Symbol" in t.columns)
    return {clean_ticker(s) for s in t["Symbol"]}


def extend(spells, changes, after):
    """Apply Wikipedia changes dated after fja's last snapshot. Removals must close an open spell."""
    spells = spells.copy()
    for c in changes[changes["date"] > after].itertuples():
        if pd.notna(c.removed):
            hit = (spells["ticker"] == c.removed) & spells["end"].isna()
            if hit.sum() != 1:
                raise ValueError(f"Wikipedia removes {c.removed} on {c.date} but it has {hit.sum()} open spells")
            spells.loc[hit, "end"] = c.date
        if pd.notna(c.added):
            spells.loc[len(spells)] = {"ticker": c.added, "start": c.date, "end": None, "source": "wikipedia"}
    return spells


def main(out_dir):
    out = Path(out_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = {"fja.csv": fetch(FJA_URL), "wiki_changes.html": fetch(WIKI_CHANGES_URL),
           "wiki_current.html": fetch(WIKI_CURRENT_URL)}
    # Raw first (PLAN rule 5). ponytail: written locally; the backfill workflow copies raw/ to R2.
    for name, text in raw.items():
        p = out / "raw" / "membership" / stamp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    spells, fja_last = fja_spells(raw["fja.csv"])
    spells = extend(spells, wiki_changes(raw["wiki_changes.html"]), after=fja_last)
    ours = set(spells.loc[spells["end"].isna(), "ticker"])
    theirs = wiki_current(raw["wiki_current.html"])

    spells.to_parquet(out / "membership.parquet", index=False)
    print(f"fja last snapshot {fja_last}, {len(spells)} spells, {len(ours)} current members")
    print(f"current members missing vs Wikipedia list: {sorted(theirs - ours)}")
    print(f"current members extra vs Wikipedia list: {sorted(ours - theirs)}")
    if ours != theirs:
        sys.exit("current membership does not match Wikipedia's list")


if __name__ == "__main__":
    main(sys.argv[1])
