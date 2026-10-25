"""Map every S&P 500 membership spell since 2010 to the SEC companies (CIKs) behind it, as dated segments.

One ticker can sit on top of two SEC companies over time (GOOGL is Google Inc. until Alphabet's first 10-Q on
2015-10-29), so a spell maps to one or more segments [start, end) with one CIK each.

A spell's latest CIK, first rule with exactly one answer wins:
  wikipedia   open spell whose ticker is on Wikipedia's current list (that list carries CIKs)
  sec_ticker  open spell whose ticker is a current SEC ticker of a company filing during the spell.
              Not used for closed spells: tickers get reused (PARA now belongs to Banzai, not Paramount).
  name        a Wikipedia change row for this ticker near the spell's start or end names the company; it is
              matched against SEC current and former names, and the company must be filing near that date
  paired      fja spells the ticker differently from Wikipedia (JOY vs JOYG): the name comes from the one
              unmatched Wikipedia event of the same kind within 7 days
  rename      fja ends this spell and starts another the same day, Wikipedia names neither ticker within 30
              days, the other spell's company was already reporting when this one started, and its SEC former
              names show a name change within 60 days of the ticker change (Anthem -> Elevance)
Then, while the earliest company started reporting after the spell started, its predecessor is prepended: the
one company sharing its name stem (Exxon Mobil Corp -> ExxonMobil Holdings Corp) whose last 10-K/10-Q falls
near the successor's first. The changeover is the successor's first 10-K/10-Q.

pipeline/ticker_cik_overrides.csv replaces all of that for a spell (rows: ticker, start of the spell, cik, from
date of the segment or blank for the spell start, no_sec_reports = yes for companies that never filed 10-K/10-Q
with the SEC, such as banks that reported to the FDIC, link, and a note on the evidence).

Each later segment's link says how it follows the one before: successor (the same business under a new SEC
entity, shares converting: Google Inc. -> Alphabet) or new_company (another business took the ticker: Fox Corp
took FOXA from 21st Century Fox in 2019). Overrides default to successor.

Every segment must be covered by its company's 10-K/10-Q reports. Anything unresolved or uncovered goes to
review/ticker_map_unresolved.csv with a suggestion, and the run fails.

Usage: python -m pipeline.ticker_map OUT_DIR  (after pipeline.membership and pipeline.sec)
"""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pipeline.membership import wiki_changes, wiki_current

SINCE = "2010-01-01"
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
OVERRIDES = Path(__file__).with_name("ticker_cik_overrides.csv")
FIXES = Path(__file__).with_name("membership_fixes.csv")
SUFFIXES = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "LLC", "LP",
            "NV", "SA", "AG", "THE", "CLASS", "A", "B", "C"}
STEM_DROP = {"HOLDINGS", "HOLDING", "GROUP", "GLOBAL", "INTERNATIONAL", "INTL", "ENTERPRISES"}


def norm(name):
    """'Walt Disney Co/DE/' and 'The Walt Disney Company' both become 'WALT DISNEY'."""
    n = re.sub(r"\(.*?\)|/[A-Z ]{1,4}/?\s*$", " ", str(name).upper())
    n = re.sub(r"['’]", "", n)  # Kohl's -> KOHLS, as SEC writes it
    n = re.sub(r"[^A-Z0-9 ]", " ", n.replace("&", " AND "))
    words = n.split()
    while words and words[-1] in SUFFIXES:
        words.pop()
    if words and words[0] == "THE":
        words.pop(0)
    return " ".join(words)


def stem(name):
    """Looser key for successor companies: 'ExxonMobil Holdings Corp' and 'Exxon Mobil Corp' -> 'EXXONMOBIL'."""
    words = norm(name).split()
    while len(words) > 1 and words[-1] in STEM_DROP:
        words.pop()
    return "".join(words)


def s(x, default):
    """Missing dates arrive as None or NaN."""
    return x if isinstance(x, str) and x else default


def shift(date, days):
    return (pd.Timestamp(date) + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def near(date, other, days=7):
    return abs(pd.Timestamp(date) - pd.Timestamp(other)) <= pd.Timedelta(days=days)


def active(c, start, end):
    """Company c filed anything in [start, end)."""
    return s(c.first_filed, "9999") < s(end, "9999-12-31") and s(c.last_filed, "") >= start


def starts_by(c, date):
    """c files 10-K/10-Q reports by `date`, or within 150 days of it (a first 10-Q after a spin-off)."""
    return s(c.first_periodic, "9999") <= shift(date, 150)


def reports_until(c, date):
    """c's last 10-K/10-Q is no more than about 13 months before `date` (today for an open segment)."""
    return s(c.last_periodic, "") >= shift(s(date, TODAY), -400)


def covers(c, start, end):
    """c's 10-K/10-Q reports span the segment. A segment under 150 days old passes before any report exists."""
    if shift(max(start, SINCE), 150) > TODAY and not s(c.first_periodic, None):
        return True
    return starts_by(c, max(start, SINCE)) and reports_until(c, end)


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


def stem_index(companies):
    idx = defaultdict(set)
    for c in companies.itertuples():
        for n in [c.name] + [f["name"] for f in json.loads(c.former_names)]:
            idx[stem(n)].add(c.cik)
    return idx


def by_name(name, date, idx, keys, ok=lambda cik: True):
    """CIKs named `name` within a year of `date` that pass `ok`. If none match exactly, SEC names that start
    with it ('Discover Financial' on Wikipedia, 'DISCOVER FINANCIAL SERVICES' at SEC)."""
    n = norm(name)
    lo, hi = shift(date, -365), shift(date, 365)
    hits = lambda keys_: {cik for k in keys_ for cik, f, t in idx[k] if f <= hi and t >= lo and ok(cik)}
    # ponytail: linear prefix scan over ~100k names, fine for a few hundred lookups per run
    return hits([n] if n in idx else []) or hits([k for k in keys if k.startswith(n + " ")])


def predecessor(c, co, stems):
    """The one company c took over from (see module docstring), or None."""
    first = s(c.first_periodic, None)
    if not first:
        return None
    keys = {stem(c["name"])} | {stem(f["name"]) for f in json.loads(c.former_names)}  # c.name is the row label
    cands = {k for key in keys for k in stems.get(key, ()) if k != c.cik}
    cands = [k for k in cands if shift(first, -120) <= s(co.loc[k].last_periodic, "") <= shift(first, 400)
             and s(co.loc[k].first_periodic, "9999") < first]
    return co.loc[cands[0]] if len(cands) == 1 else None


def wiki_events(changes):
    return ([("add", r.added, r.date, r.added_name) for r in changes.itertuples() if pd.notna(r.added)] +
            [("remove", r.removed, r.date, r.removed_name) for r in changes.itertuples() if pd.notna(r.removed)])


def resolve(spells, changes, current, companies):
    """Each spell's latest CIK and the rule that found it, or a suggestion and reason."""
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
    for sp in spells.itertuples(index=False):
        row = {**sp._asdict(), "cik": None, "method": None, "suggested_cik": None, "reason": None}
        held = {c.cik for c in holders.get(sp.ticker, []) if active(c, sp.start, sp.end)}
        if pd.isna(sp.end) and sp.ticker in cur:
            row.update(cik=int(cur[sp.ticker]), method="wikipedia")
        elif pd.isna(sp.end) and len(held) == 1:
            row.update(cik=held.pop(), method="sec_ticker")
        else:
            names = [(n, d) for k, t, d, n in wiki if t == sp.ticker and isinstance(n, str) and  # dates differ up to ~8 days
                     ((k == "add" and near(d, sp.start, 14)) or
                      (k == "remove" and pd.notna(sp.end) and near(d, sp.end, 14)))]
            method = "name"
            if not names:
                for kind, date in [("add", sp.start), ("remove", sp.end)]:
                    cand = [(n, d) for k, t, d, n in unmatched_wiki if k == kind and pd.notna(date) and near(d, date)]
                    if len(cand) == 1 and isinstance(cand[0][0], str):
                        names.append(cand[0])
                method = "paired"
            hits = set()
            for n, d in names:  # the company must have been filing within a year of the add or remove
                hits |= by_name(n, d, idx, keys, lambda h, d=d: h in co.index and
                                active(co.loc[h], shift(d, -365), shift(d, 365)))
            if len(hits) > 1:  # 'Unum' matches Unum Group and Unum Therapeutics: keep who reported all along
                hits = {h for h in hits if covers(co.loc[h], sp.start, sp.end)} or hits
            if len(hits) == 1:
                row.update(cik=hits.pop(), method=method)
            elif len(held) == 1:
                row.update(suggested_cik=held.pop(), reason="current holder of this ticker (tickers get reused)")
            else:
                row.update(reason=f"no unique match ({len(hits)} by name, {len(held)} by current ticker)")
        out.append(row)
    return renames(pd.DataFrame(out), wiki, co)


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
            if s(c.first_periodic, "9999") <= max(u["start"], SINCE) and renamed_near(c, u["end"]):
                m.loc[i, ["cik", "method", "suggested_cik", "reason"]] = [p["cik"], "rename", None, None]
                changed = True
            elif pd.isna(u["suggested_cik"]):
                m.loc[i, ["suggested_cik", "reason"]] = [
                    p["cik"], f"same-day ticker swap with {p['ticker']}, but no matching SEC name change"]
    return m


def segments(m, companies, overrides):
    """(segments, unresolved): one segment row per stretch of a spell with one CIK."""
    co = companies.set_index("cik", drop=False)
    stems = stem_index(companies)
    new = lambda date: shift(date, 150) > TODAY  # too young to have a first 10-Q yet
    segs, bad = [], []
    for r in m.itertuples(index=False):
        o = overrides[(overrides["ticker"] == r.ticker) & (overrides["start"] == r.start)]
        # chain: (from, cik, method, link to the previous segment: successor or new_company)
        if len(o):
            o = o.assign(f=o["from"].map(lambda x: s(x, r.start))).sort_values("f")
            chain = [(row.f, int(row.cik) if s(row.cik, None) else None,
                      "override_no_sec_reports" if s(row.no_sec_reports, "") == "yes" else "override",
                      None if i == 0 else s(row.link, "successor"))
                     for i, row in enumerate(o.itertuples())]
        elif pd.notna(r.cik):
            chain = [(r.start, int(r.cik), r.method, None)]
            while chain[0][1] in co.index and not starts_by(co.loc[chain[0][1]], max(r.start, SINCE)):
                p = predecessor(co.loc[chain[0][1]], co, stems)
                if p is None:
                    break
                chain[0] = (co.loc[chain[0][1]].first_periodic, chain[0][1], chain[0][2], "successor")
                chain.insert(0, (r.start, int(p.cik), "predecessor", None))
        else:
            bad.append({**r._asdict()})
            continue
        ends = [f for f, *_ in chain[1:]] + [r.end]
        rows = [{"ticker": r.ticker, "spell_start": r.start, "start": f, "end": e, "cik": k, "method": meth,
                 "link": link} for (f, k, meth, link), e in zip(chain, ends)]
        uncovered = []
        for x in rows:
            if x["method"] == "override_no_sec_reports" or (pd.notna(x["end"]) and x["end"] < SINCE):
                continue
            if x["cik"] not in co.index:
                if not new(x["start"]):
                    uncovered.append((x, f"CIK {x['cik']} isn't in the SEC companies table"))
            elif not covers(co.loc[x["cik"]], x["start"], x["end"]):
                c = co.loc[x["cik"]]
                uncovered.append((x, f"{x['method']} match {c['name']} doesn't report across {x['start']} to "
                                     f"{s(x['end'], 'today')} (10-K/10-Q {c.first_periodic} to {c.last_periodic})"))
        if uncovered:
            x, why = uncovered[0]
            bad.append({**r._asdict(), "suggested_cik": x["cik"], "reason": why})
        else:
            segs += rows
    return pd.DataFrame(segs), pd.DataFrame(bad)


def main(out_dir):
    out = Path(out_dir)
    raw = sorted((out / "raw" / "membership").iterdir())[-1]
    spells = pd.read_parquet(out / "market" / "membership.parquet")
    spells = spells[spells["end"].isna() | (spells["end"] >= SINCE)]
    changes = wiki_changes((raw / "wiki_changes.html").read_text(encoding="utf-8"))
    current = wiki_current((raw / "wiki_current.html").read_text(encoding="utf-8"))
    companies = pd.read_parquet(out / "market" / "companies.parquet")
    overrides = pd.read_csv(OVERRIDES, dtype=str, comment="#") if OVERRIDES.exists() else \
        pd.DataFrame(columns=["ticker", "start", "cik", "from", "no_sec_reports", "link", "note"])
    # Spells fja is missing (reviewed by hand): new spells whose CIK segments are given like overrides.
    fixes = pd.read_csv(FIXES, dtype=str, comment="#")
    spells = pd.concat([spells, fixes[["ticker", "start", "end"]].drop_duplicates().assign(source="fix")],
                       ignore_index=True)
    overrides = pd.concat([overrides, fixes.drop(columns="end")], ignore_index=True)

    m = resolve(spells, changes, current, companies)
    segs, unresolved = segments(m, companies, overrides)
    print(segs.drop_duplicates(["ticker", "spell_start"])["method"].value_counts().to_string())
    print(f"{len(segs)} segments for {segs[['ticker', 'spell_start']].drop_duplicates().shape[0]} spells, "
          f"{(segs['method'] == 'predecessor').sum()} predecessor segments, {len(unresolved)} spells unresolved")
    (out / "review").mkdir(parents=True, exist_ok=True)
    names = companies.set_index("cik")["name"]
    if len(unresolved):
        unresolved["suggested_name"] = unresolved["suggested_cik"].map(
            lambda c: names.get(int(c)) if pd.notna(c) else None)
    unresolved.to_csv(out / "review" / "ticker_map_unresolved.csv", index=False)
    segs.to_parquet(out / "market" / "ticker_map.parquet", index=False)

    ours, theirs = set(segs.loc[segs["end"].isna(), "cik"]), set(current["cik"])
    print(f"current CIKs ours only {sorted(ours - theirs)}, Wikipedia only {sorted(theirs - ours)}")
    if len(unresolved) or ours != theirs:
        sys.exit(f"{len(unresolved)} spells unresolved, current-member CIK match: {ours == theirs}")


if __name__ == "__main__":
    main(sys.argv[1])
