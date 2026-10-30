"""Company news from Finnhub (PLAN "Data sources": news headlines, Finnhub free) as panel documents for the news analyst.

Finnhub's company-news endpoint gives a headline and a short summary per article, not the article itself. That pair is
the document, tagged by trust tier from Finnhub's source name (docs/M7.md has the measured source list). Only
articles published on or before the panel's day count, newest first, at most MAX_NEWS from the last NEWS_DAYS.
The key goes in a header, never the URL, and calls stay under the free tier's 60 a minute.

Usage: python -m agents.news SYMBOL... (a check: prints what each symbol gets, no model calls; needs FINNHUB_API_KEY)
"""
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import requests

from agents import docs

URL = "https://finnhub.io/api/v1/company-news"
NEWS_DAYS, MAX_NEWS = 30, 10
GAP = 1.1  # seconds between calls: under 60 a minute
OUTLETS = {"reuters", "bloomberg", "cnbc", "marketwatch", "wsj", "the wall street journal", "dow jones",
           "financial times", "barron's", "barrons", "associated press", "ap", "forbes", "fortune", "the new york times"}
WIRES = {"business wire", "businesswire", "pr newswire", "prnewswire", "globenewswire", "globe newswire",
         "accesswire", "accesswire.com"}
_last = 0.0


def get(symbol, start, end, key, tries=4):
    """Finnhub company news for symbol between two ISO dates, with 4 tries and backoff, then a loud failure."""
    global _last
    for attempt in range(tries):
        time.sleep(max(0.0, GAP - (time.monotonic() - _last)))  # ponytail: single-process limiter
        _last = time.monotonic()
        try:
            r = requests.get(URL, params={"symbol": symbol, "from": start, "to": end},
                             headers={"X-Finnhub-Token": key}, timeout=60)
            r.raise_for_status()
            items = r.json()
            if not isinstance(items, list):
                raise ValueError(f"expected a list, got {str(items)[:200]}")
            return items
        except (requests.RequestException, ValueError) as e:
            print(f"Finnhub request failed (try {attempt + 1}/{tries}): {symbol}: {e}", file=sys.stderr)
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt * 5)


def tier(source):
    s = (source or "").strip().lower()
    return "established outlet" if s in OUTLETS else "company press release" if s in WIRES else "other"


def documents(items, day):
    """Panel documents from Finnhub items: published by the end of `day`, one per headline, newest MAX_NEWS."""
    end = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(days=1)
    seen, out = set(), []
    for a in sorted(items, key=lambda a: -a.get("datetime", 0)):
        when = datetime.fromtimestamp(a.get("datetime", 0), timezone.utc)
        head = (a.get("headline") or "").strip()
        if when >= end or not head or head.lower() in seen:
            continue
        seen.add(head.lower())
        text = head + "\n\n" + (a.get("summary") or "").strip()
        d = docs.document(text, f"news-{a.get('id')}", when.date().isoformat(), f"{a.get('source')} (via Finnhub)",
                          tier(a.get("source")), html=False)
        if d["text"]:
            out.append(d)
        if len(out) == MAX_NEWS:
            break
    return out


def news_docs(symbol, day, key):
    start = (date.fromisoformat(day) - timedelta(days=NEWS_DAYS)).isoformat()
    return documents(get(symbol, start, day, key), day)


if __name__ == "__main__":
    key, day = os.environ["FINNHUB_API_KEY"], date.today().isoformat()
    start = (date.today() - timedelta(days=NEWS_DAYS)).isoformat()
    sources, words = Counter(), []
    for sym in sys.argv[1:]:
        items = get(sym, start, day, key)
        ds = documents(items, day)
        sources.update(a.get("source") for a in items)
        words += [len(d["text"].split()) for d in ds]
        print(f"{sym}: {len(items)} articles in {NEWS_DAYS} days, {len(ds)} kept, tiers "
              f"{dict(Counter(d['tier'] for d in ds))}, quarantined lines {sum(len(d['quarantined']) for d in ds)}")
    print("sources (all articles):", dict(sources.most_common()))
    if words:
        print(f"words per kept document: median {sorted(words)[len(words) // 2]}, max {max(words)}")
