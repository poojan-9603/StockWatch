"""Compare fja's S&P 500 membership with Wikipedia's change table, by company rather than by ticker.

A company here is a CIK plus its successors (Google Inc. and Alphabet are one company; 21st Century Fox and
Fox Corp, which took its tickers, are two). Its membership
is the union of its spells' dates, with gaps of 3 days or less closed (a ticker change ends one spell and starts
the next). Each start or end since 2010 is an event. Wikipedia events are mapped to the same companies, by the
ticker's spells near that date or else by company name. An event with no same-kind match within 7 days on the
other side is a disagreement, written to review/membership_disagreements.csv for review by hand. A Wikipedia
row for a company that is a member the week before and after is marked wikipedia_same_company: usually a share
class or ticker change, but it can also be a name that matched the wrong company (Wikipedia's 2010 'Chubb' add
is ACE under today's name), so those rows are listed too.

Usage: python -m pipeline.reconcile OUT_DIR  (after pipeline.ticker_map)
"""
import sys
from pathlib import Path

import pandas as pd

from pipeline.membership import wiki_changes
from pipeline.ticker_map import SINCE, active, by_name, name_index, near, shift, wiki_events


def key(r):
    """A segment's company key: its CIK, or 'ticker:X' for a spell with no SEC company (First Republic)."""
    return int(r.cik) if pd.notna(r.cik) else f"ticker:{r.ticker}"


def entities(segs):
    """CIK -> entity id. A successor segment joins the company before it; a new_company segment doesn't."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    for _, g in segs.sort_values("start").groupby(["ticker", "spell_start"]):
        prev = None
        for r in g.itertuples():
            k = find(key(r))
            if prev is not None and r.link == "successor":
                parent[k] = find(prev)
            prev = key(r)
    return {k: find(k) for k in list(parent)}


def memberships(segs, ent):
    """Entity -> merged [start, end, tickers] intervals (end None while a member). A spell is split where a
    new_company segment begins."""
    rows = []
    for (ticker, _), g in segs.sort_values("start").groupby(["ticker", "spell_start"]):
        run = None
        for r in g.itertuples():
            end = None if pd.isna(r.end) else r.end
            if run and r.link != "new_company":
                run[2] = end
            else:
                if run:
                    rows.append(tuple(run))
                run = [ent[key(r)], r.start, end, ticker]
        rows.append(tuple(run))
    out = {}
    for e, start, end, ticker in sorted(rows, key=lambda x: (str(x[0]), x[1])):
        iv = out.setdefault(e, [])
        if iv and (iv[-1][1] is None or start <= shift(iv[-1][1], 3)):
            last = iv[-1]
            iv[-1] = [last[0], None if last[1] is None or end is None else max(last[1], end), last[2] | {ticker}]
        else:
            iv.append([start, end, {ticker}])
    return out


def compare(segs, changes, companies):
    ent = entities(segs)
    mem = memberships(segs, ent)
    ours = [(e, "add", s, t) for e, ivs in mem.items() for s, _, t in ivs if s >= SINCE] + \
           [(e, "remove", x, t) for e, ivs in mem.items() for _, x, t in ivs if x is not None and x >= SINCE]

    co = companies.set_index("cik", drop=False)
    idx = name_index(companies)
    keys = sorted(idx)
    names = companies.set_index("cik")["name"]
    theirs = []
    for kind, ticker, date, name in wiki_events(changes):
        if date < SINCE:
            continue
        col = "start" if kind == "add" else "end"  # segment dates: IR's 2020 switch to Gardner Denver is mid-spell
        hit = {key(r) for r in segs.itertuples() if r.ticker == ticker and pd.notna(getattr(r, col)) and
               near(getattr(r, col), date, 14)}
        if not hit and isinstance(name, str):
            hit = by_name(name, date, idx, keys, lambda h: h in co.index and
                          active(co.loc[h], shift(date, -365), shift(date, 365)))
        hit = {ent.get(h, h) for h in hit}
        theirs.append((hit.pop() if len(hit) == 1 else None, kind, date, ticker, name))

    def stays(e, date):
        """e is a member from a week before `date` to a week after: a share class or ticker change, not a
        membership change (Wikipedia lists GOOGL's 2014 class split as an add)."""
        lo, hi = shift(date, -7), shift(date, 7)
        return any(s < lo and (x is None or x > hi) for s, x, _ in mem.get(e, []))

    rows, same_company = [], 0
    for e, kind, date, tickers in ours:
        if not any(e2 == e and k2 == kind and near(d2, date) for e2, k2, d2, _, _ in theirs):
            other = [d2 for e2, k2, d2, _, _ in theirs if e2 == e and k2 == kind]
            rows.append({"side": "fja_only", "kind": kind, "date": date, "tickers": ",".join(sorted(tickers)),
                         "company": names.get(e) if isinstance(e, int) else e, "entity": e,
                         "wikipedia_same_kind_dates": ",".join(other)})
    for e, kind, date, ticker, name in theirs:
        if e is None:
            rows.append({"side": "wikipedia_unmapped", "kind": kind, "date": date, "tickers": ticker,
                         "company": name, "entity": None, "wikipedia_same_kind_dates": ""})
        elif not any(e2 == e and k2 == kind and near(d2, date) for e2, k2, d2, _ in ours):
            other = [d2 for e2, k2, d2, _ in ours if e2 == e and k2 == kind]
            same_company += stays(e, date)
            rows.append({"side": "wikipedia_same_company" if stays(e, date) else "wikipedia_only",
                         "kind": kind, "date": date, "tickers": ticker,
                         "company": name, "entity": e, "fja_same_kind_dates": ",".join(other)})
    out = pd.DataFrame(rows)
    out["period"] = out["date"].map(lambda d: "tuning 2010-2019" if d < "2020-01-01" else "test/live 2020+")
    return out.sort_values(["date", "side"], ignore_index=True), len(ours), len(theirs), same_company


def main(out_dir):
    out = Path(out_dir)
    raw = sorted((out / "raw" / "membership").iterdir())[-1]
    segs = pd.read_parquet(out / "market" / "ticker_map.parquet")
    changes = wiki_changes((raw / "wiki_changes.html").read_text(encoding="utf-8"))
    companies = pd.read_parquet(out / "market" / "companies.parquet")
    d, n_ours, n_theirs, same = compare(segs, changes, companies)
    (out / "review").mkdir(parents=True, exist_ok=True)
    d.to_csv(out / "review" / "membership_disagreements.csv", index=False)
    print(f"company events since {SINCE}: fja {n_ours}, Wikipedia {n_theirs}; "
          f"{same} unmatched Wikipedia rows are for a company that stays in (likely share class or ticker changes)")
    print(d.groupby(["period", "side"]).size().to_string())


if __name__ == "__main__":
    main(sys.argv[1])
