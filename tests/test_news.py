"""Finnhub news as panel documents: only what was out by the panel's day, one per headline, newest first, capped,
tiered by source; requests carry the key in a header and fail loudly after their tries."""
from datetime import datetime, timezone

import pytest
import requests

from agents import news


def item(i, when, head, source="Yahoo", summary="Sales rose."):
    t = datetime.fromisoformat(when).replace(tzinfo=timezone.utc).timestamp()
    return {"id": i, "datetime": int(t), "headline": head, "summary": summary, "source": source, "url": "u"}


def test_documents_point_in_time_deduped_newest_first_and_tiered():
    items = [item(1, "2026-10-28T12:00", "Old news"), item(2, "2026-10-30T23:59", "Late Friday", "CNBC"),
             item(3, "2026-10-31T00:01", "After the day"), item(4, "2026-10-29T10:00", "old news", "Benzinga"),
             item(5, "2026-10-29T09:00", "Wire", "Business Wire"), item(6, "2026-10-29T08:00", "")]
    ds = news.documents(items, "2026-10-30")
    assert [d["id"] for d in ds] == ["news-2", "news-4", "news-5"]  # "Old news" repeats "old news"; blank dropped
    assert [d["tier"] for d in ds] == ["established outlet", "other", "company press release"]
    assert ds[0]["date"] == "2026-10-30" and ds[0]["source"] == "CNBC (via Finnhub)"
    assert ds[0]["text"] == "Late Friday\nSales rose."


def test_documents_are_capped():
    items = [item(i, f"2026-10-{1 + i:02d}T12:00", f"h{i}") for i in range(20)]
    assert [d["id"] for d in news.documents(items, "2026-10-30")] == [f"news-{i}" for i in range(19, 9, -1)]


def test_get_sends_the_key_in_a_header_and_fails_loudly(monkeypatch):
    calls = []

    class R:
        def __init__(self, ok): self.ok = ok
        def raise_for_status(self):
            if not self.ok: raise requests.HTTPError("429")
        def json(self): return []

    answers = [False, True]
    monkeypatch.setattr(news.requests, "get", lambda url, **kw: calls.append(kw) or R(answers.pop(0)))
    monkeypatch.setattr(news.time, "sleep", lambda s: None)
    assert news.get("AAPL", "2026-10-01", "2026-10-30", "secret") == []
    assert calls[1]["headers"] == {"X-Finnhub-Token": "secret"} and "token" not in calls[1]["params"]
    answers[:] = [False] * 4
    with pytest.raises(requests.HTTPError):
        news.get("AAPL", "2026-10-01", "2026-10-30", "secret")
