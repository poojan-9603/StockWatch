"""Point-in-time fundamentals: what was public on a given day.

A fact counts on day D only if it was filed on or before D (rule 2: no number before its filing date). Among those,
the most recent period wins, and within a period the latest filed copy (a restatement already public by D). Values
older than max_age days (by period end) are treated as missing rather than stale.
"""
import pandas as pd

from quant import lock


def as_of(facts, day, tags, annual=None, max_age=550, field="value"):
    """facts: cik, tag, start, end, value, form, filed. Returns {tag: Series cik -> value} as known on `day`.
    annual: True keeps only ~1-year durations (income and cash flow, from 10-Ks), False only instants (balance
    sheet), None either. field="end" returns each value's period end instead."""
    lock.check([day])
    f = facts[(facts["filed"] <= day) & facts["tag"].isin(tags)]
    if annual is not None:
        dur = (pd.to_datetime(f["end"]) - pd.to_datetime(f["start"])).dt.days
        f = f[dur.between(330, 380)] if annual else f[f["start"].isna()]
    oldest = (pd.Timestamp(day) - pd.Timedelta(days=max_age)).strftime("%Y-%m-%d")
    f = f[f["end"] >= oldest].sort_values(["end", "filed"])
    last = f.groupby(["cik", "tag"]).tail(1)
    return {t: g.set_index("cik")[field] for t, g in last.groupby("tag")}


def first_available(found, chain):
    """Fallback chain over tags: the first tag in `chain` that a company has. Returns Series cik -> value."""
    out = pd.Series(dtype=float)
    for t in chain:
        s = found.get(t)
        if s is not None:
            out = pd.concat([out, s[~s.index.isin(out.index)]])
    return out
