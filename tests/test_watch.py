"""The holdings watch: its hours, 8-K red flags, 10% drops, no repeats, quiet hours held until 7 am, and loud
failures that still send what worked. Alerts go to the app only for now (watch.ACTIVE)."""
import json
from datetime import datetime

import pytest

from monitor import alerts, watch
from pipeline.db import connect, ingest_inbox

C = alerts.CENTRAL


def at(*a):
    return datetime(*a, tzinfo=C)


def test_due_on_weekdays_7_am_to_6_pm_central():
    assert watch.due(at(2026, 10, 26, 7, 0)) and watch.due(at(2026, 10, 26, 18, 59))
    assert not watch.due(at(2026, 10, 26, 6, 59)) and not watch.due(at(2026, 10, 26, 19, 0))
    assert not watch.due(at(2026, 10, 31, 10, 0))  # Saturday
    assert watch.due(at(2026, 11, 2, 7, 30))  # after the switch to standard time


def eightk(acc, filed, items, cik=320193):
    return {"cik": cik, "accession": acc, "form": "8-K", "filed": filed, "items": items}


def test_filing_alerts_red_flags_are_urgent_others_informational_old_ones_skipped():
    a = watch.filing_alerts("AAPL", [eightk("1", "2026-10-26", "4.02,9.01"), eightk("2", "2026-10-26", "2.02,9.01"),
                                     eightk("3", "2026-10-01", "1.03")], "2026-10-22")
    assert [(x["key"], x["tier"]) for x in a] == [("8k:1", "urgent"), ("8k:2", "informational")]
    assert "4.02 past financial statements" in a[0]["subject"] and "-index.htm" in a[0]["text"]


def test_drop_alert_at_ten_percent():
    assert watch.drop_alert("AAPL", 90.0, 100.0, "2026-10-26")["severity"] == 0.1
    assert watch.drop_alert("AAPL", 90.01, 100.0, "2026-10-26") is None


def setup(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    ingest_inbox(con, {"001.json": {"kind": "buy", "date": "2026-10-01", "symbol": "AAPL", "cik": 320193,
                                    "shares": 10, "price": 250}})
    return con


def sources(price=88.0):
    return {"quote": lambda s: (price, 100.0), "submissions": lambda cik: [eightk("a", "2026-10-26", "4.02")]}


def test_alerts_go_to_the_app_once(tmp_path):
    con, sent = setup(tmp_path), []
    deliver = lambda ch, msg: sent.append((ch, msg["subject"]))
    log = watch.run(con, at(2026, 10, 26, 10, 0), deliver=deliver, **sources())
    assert log["holdings"] == 1 and log["alerts"] == 2 and log["sent"] == {"app": 2}
    assert sent == [("app", "StockWatch: 2 urgent")]
    sent.clear()
    log = watch.run(con, at(2026, 10, 26, 11, 0), deliver=deliver, **sources())
    assert sent == [] and log["sent"] == {"app": 0}  # repeats are suppressed, still recorded
    assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 4


def test_quiet_hours_hold_until_the_7_am_run(tmp_path):
    con, sent = setup(tmp_path), []
    deliver = lambda ch, msg: sent.append((ch, msg["body"]))
    watch.run(con, at(2026, 10, 26, 22, 0), test=True, deliver=deliver, quote=lambda s: (99.0, 100.0),
              submissions=lambda cik: [])
    assert sent == [] and {h["status"] for h in alerts.history(con)} == {"held"}
    watch.run(con, at(2026, 10, 27, 7, 5), deliver=deliver, quote=lambda s: (99.0, 100.0), submissions=lambda c: [])
    assert len(sent) == 1 and sent[0][0] == "app" and "Test alert" in sent[0][1]


def test_failures_are_loud_but_what_worked_still_goes_out(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "ACTIVE", ("email", "discord"))  # the plan's channels, switched back on
    con, sent = setup(tmp_path), []

    def deliver(ch, msg):
        if ch == "discord":
            raise RuntimeError("discord failed after 3 tries: HTTP 500")
        sent.append(ch)

    def broken(symbol):
        raise KeyError("currentTradingPeriod")

    with pytest.raises(SystemExit) as e:
        watch.run(con, at(2026, 10, 26, 10, 0), deliver=deliver, quote=broken,
                  submissions=lambda cik: [eightk("a", "2026-10-26", "4.02")])
    assert "AAPL quote: KeyError" in str(e.value) and "discord delivery" in str(e.value)
    assert sent == ["email"]
    rows = alerts.history(con)
    assert {h["channel"] for h in rows} == {"email"}  # discord recorded nothing, so the next run tries again
    assert json.loads(con.execute("SELECT body FROM alerts").fetchone()[0])["key"] == "8k:a"
