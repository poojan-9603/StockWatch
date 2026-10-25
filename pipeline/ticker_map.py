"""Map every S&P 500 membership spell since 2010 to a CIK.

Accepted automatically, first rule with exactly one answer wins:
  override    pipeline/ticker_cik_overrides.csv (checked by hand, with a note on the evidence)
  wikipedia   open spell whose ticker is on Wikipedia's current list (that list carries CIKs)
  sec_ticker  open spell whose ticker is a current SEC ticker of a company filing during the spell.
              Not used for closed spells: tickers get reused (PARA now belongs to Banzai, not Paramount).
  name        a Wikipedia change row for this ticker near the spell's start or end names the company; it is
              matched against SEC current and former names, and the company must be filing near that date
  paired      fja spells the ticker differently from Wikipedia (JOY vs JOYG): the name comes from the one
              unmatched Wikipedia event of the same kind within 7 days
  rename      fja ends this spell and starts another the same day, Wikipedia names neither ticker within 30
              days, the other spell's company was already filing when this one started, and its SEC former
              names show a name change within 60 days of the ticker change (Anthem -> Elevance)
Every accepted match must also cover the spell: the company files from the spell's start (or 2010) to its end.
Anything else goes to review/ticker_map_unresolved.csv with a suggestion, and the run fails.

Usage: python -m pipeline.ticker_map OUT_DIR  (after pipeline.membership and pipeline.sec)
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

from pipeline.membership import wiki_changes, wiki_current

SINCE = "2010-01-01"
WINDOW = pd.Timedelta(days=7)
OVERRIDES = Path(__file__).with_name("ticker_cik_overrides.csv")
SUFFIXES = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "LLC", "LP",
            "NV", "SA", "AG", "THE", "CLASS", "A", "B", "C"}


def norm(name):
    """'Walt Disney Co/DE/' and 'The Walt Disney Company' both become 'WALT DISNEY'."""
    n = re.sub(r"\(.*?\)|/[A-Z ]{1,4}/?\s*$", " ", str(name).upper())
    n = re.sub(r"[^A-Z0-9 ]", " ", n.replace("&", " AND "))
    words = n.split()
    while words and words[-1] in SUFFIXES:
        words.pop()
    if words and words[0] == "THE":
        words.pop(0)
    return " ".join(words)


def s(x, default):
    """Missing dates arrive as None or NaN."""
    return x if isinstance(x, str) else default


def name_index(companies):
    """normalized name -> [(cik, from, to)]; current names run from the last former name's end to today."""
    idx = defaultdict(list)
    for c in companies.itertuples():
        former = json.loads(c.former_names)
        for f in former:
            idx[norm(f["name"])].append((c.cik, f["from"][:10], f["to"][:10]))
        start = max((f["to"][:10] for f in former), default=s(c.first_filed, "1900-01-01"))
        idx[norm(c.name)].append((c.cik, start, "9999-12-31"))
    return idx


def window(date, days):
    d = pd.Timestamp(date)
    return (d - pd.Timedelta(days=days)).strftime("%Y-%m-%d"), (d + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def active(c, start, end):
    """Company c filed something in [start, end)."""
    return s(c.first_filed, "9999") < s(end, "9999-12-31") and s(c.last_filed, "") >= start


def covers(c, start, end):
    """Company c was filing from the spell's start (or 2010) through its end, with room for filing gaps."""
    lo, _ = window(max(start, SINCE), -90)  # first filing at most 90 days after the start
    _, hi = window(s(end, s(c.last_filed, "")), -400)  # last filing at most ~13 months before the end
    return s(c.first_filed, "9999") <= lo and s(c.last_filed, "") >= hi


def by_name(name, date, idx, keys):
    """CIKs named `name` within a year of `date`. If no SEC name matches exactly, SEC names that start with it
    ('Discover Financial' on Wikipedia, 'DISCOVER FINANCIAL SERVICES' at SEC)."""
    n = norm(name)
    lo, hi = window(date, 365)
    # ponytail: linear prefix scan over ~100k names, fine for a few hundred lookups per run
    matches = [n] if n in idx else [k for k in keys if k.startswith(n + " ")]
    return {cik for k in matches for cik, f, t in idx[k] if f <= hi and t >= lo}


def near(date, other, days=7):
    return abs(pd.Timestamp(date) - pd.Timestamp(other)) <= pd.Timedelta(days=days)


def wiki_events(changes):
    return ([("add", r.added, r.date, r.added_name) for r in changes.itertuples() if pd.notna(r.added)] +
            [("remove", r.removed, r.date, r.removed_name) for r in changes.itertuples() if pd.notna(r.removed)])


def resolve(spells, changes, current, companies, overrides):
    """Returns spells with cik, method, and for unresolved rows suggested_cik and reason."""
    co = companies.set_index("cik", drop=False)
    idx = name_index(companies)
    keys = sorted(idx)
    holders = defaultdict(list)
    for c in companies.itertuples():
        for t in filter(None, c.tickers.split(",")):
            holders[t.replace("-", ".")].append(c)
    cur = dict(zip(current["ticker"], current["cik"]))
    wiki = wiki_events(changes)
    fja_ev = [("add", r.ticker, r.start) for r in spells.itertuples()] + \
             [("remove", r.ticker, r.end) for r in spells.itertuples() if pd.notna(r.end)]
    unmatched_wiki = [w for w in wiki if w[2] >= SINCE and not any(
        k == w[0] and t == w[1] and near(w[2], d) for k, t, d in fja_ev)]

    out = []
    for sp in spells.itertuples():
        row = {**sp._asdict(), "cik": None, "method": None, "suggested_cik": None, "reason": None}
        row.pop("Index")
        o = overrides[(overrides["ticker"] == sp.ticker) & (overrides["start"] == sp.start)]
        held = {c.cik for c in holders.get(sp.ticker, []) if active(c, sp.start, sp.end)}
        if len(o):
            row.update(cik=int(o["cik"].iloc[0]), method="override")
        elif pd.isna(sp.end) and sp.ticker in cur:
            row.update(cik=int(cur[sp.ticker]), method="wikipedia")
        elif pd.isna(sp.end) and len(held) == 1:
            row.update(cik=held.pop(), method="sec_ticker")
        else:
            names = [(n, d) for k, t, d, n in wiki if t == sp.ticker and isinstance(n, str) and
                     ((k == "add" and near(d, sp.start)) or (k == "remove" and pd.notna(sp.end) and near(d, sp.end)))]
            method = "name"
            if not names:
                for kind, date in [("add", sp.start), ("remove", sp.end)]:
                    cand = [(n, d) for k, t, d, n in unmatched_wiki if k == kind and pd.notna(date) and near(d, date)]
                    if len(cand) == 1 and isinstance(cand[0][0], str):
                        names.append(cand[0])
                method = "paired"
            hits = set()
            for n, d in names:  # the company must have been filing within a year of the add or remove
                lo, hi = window(d, 365)
                hits |= {h for h in by_name(n, d, idx, keys) if h in co.index and active(co.loc[h], lo, hi)}
            if len(hits) == 1:
                row.update(cik=hits.pop(), method=method)
            elif len(held) == 1:
                row.update(suggested_cik=held.pop(), reason="current holder of this ticker (tickers get reused)")
            else:
                row.update(reason=f"no unique match ({len(hits)} by name, {len(held)} by current ticker)")
        out.append(row)
    m = pd.DataFrame(out)
    m = renames(m, wiki, co)
    for i, r in m[m["cik"].notna()].iterrows():  # every match must cover its spell
        if not covers(co.loc[int(r["cik"])], r["start"], r["end"]):
            m.loc[i, ["suggested_cik", "reason", "cik", "method"]] = [
                r["cik"], f"{r['method']} match, but the company isn't filing across the whole spell", None, None]
    return m


def renames(m, wiki, co):
    """See 'rename' in the module docstring. Repeats so SYMC -> NLOK -> GEN resolves back from today's ticker."""
    def quiet(tickers, date):
        return not any(t in tickers and near(d, date, 30) for _, t, d, _ in wiki)

    def renamed_near(c, date):
        return any(near(f["to"][:10], date, 60) for f in json.loads(c.former_names))

    changed = True
    while changed:
        changed = False
        for i, u in m[m["cik"].isna()].iterrows():
            if pd.isna(u["end"]):
                continue
            partner, gone = m[m["start"] == u["end"]], m[m["end"] == u["end"]]
            if len(partner) != 1 or len(gone) != 1 or pd.isna(partner["cik"].iloc[0]):
                continue
            p = partner.iloc[0]
            c = co.loc[int(p["cik"])]
            if not quiet({u["ticker"], p["ticker"]}, u["end"]):
                continue
            if s(c.first_filed, "9999") <= max(u["start"], SINCE) and renamed_near(c, u["end"]):
                m.loc[i, ["cik", "method", "suggested_cik", "reason"]] = [p["cik"], "rename", None, None]
                changed = True
            elif pd.isna(u["suggested_cik"]):
                m.loc[i, ["suggested_cik", "reason"]] = [
                    p["cik"], f"same-day ticker swap with {p['ticker']}, but no matching SEC name change"]
    return m


def company_spells(mapped):
    """Merge back-to-back ticker spells of the same CIK (renames) into one membership spell per company."""
    rows = []
    for cik, g in mapped.dropna(subset=["cik"]).sort_values("start").groupby("cik"):
        cur = None
        for r in g.itertuples():
            end = None if pd.isna(r.end) else r.end
            if cur and cur["end"] is not None and r.start <= cur["end"]:
                cur["end"] = None if end is None else max(cur["end"], end)
                cur["tickers"] += f",{r.ticker}"
            else:
                if cur:
                    rows.append(cur)
                cur = {"cik": int(cik), "start": r.start, "end": end, "tickers": r.ticker}
        rows.append(cur)
    return pd.DataFrame(rows)


def main(out_dir):
    out = Path(out_dir)
    raw = sorted((out / "raw" / "membership").iterdir())[-1]
    spells = pd.read_parquet(out / "market" / "membership.parquet")
    spells = spells[spells["end"].isna() | (spells["end"] >= SINCE)]
    changes = wiki_changes((raw / "wiki_changes.html").read_text(encoding="utf-8"))
    current = wiki_current((raw / "wiki_current.html").read_text(encoding="utf-8"))
    companies = pd.read_parquet(out / "market" / "companies.parquet")
    overrides = pd.read_csv(OVERRIDES, dtype=str, comment="#") if OVERRIDES.exists() else \
        pd.DataFrame(columns=["ticker", "start", "cik"])

    mapped = resolve(spells, changes, current, companies, overrides)
    print(mapped["method"].value_counts(dropna=False).to_string())
    (out / "review").mkdir(parents=True, exist_ok=True)
    names = companies.set_index("cik")["name"]
    unresolved = mapped[mapped["cik"].isna()].copy()
    unresolved["suggested_name"] = unresolved["suggested_cik"].map(lambda c: names.get(int(c)) if pd.notna(c) else None)
    unresolved.to_csv(out / "review" / "ticker_map_unresolved.csv", index=False)
    mapped.to_parquet(out / "market" / "ticker_map.parquet", index=False)

    cs = company_spells(mapped)
    ours, theirs = set(cs.loc[cs["end"].isna(), "cik"]), set(current["cik"])
    print(f"{len(cs)} company spells, {len(ours)} open; current CIKs ours only {sorted(ours - theirs)}, "
          f"Wikipedia only {sorted(theirs - ours)}")
    if len(unresolved) or ours != theirs:
        sys.exit(f"{len(unresolved)} spells without a CIK, current-member CIK match: {ours == theirs}")


if __name__ == "__main__":
    main(sys.argv[1])
