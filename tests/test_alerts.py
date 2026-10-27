import json
from datetime import datetime, timedelta, timezone

import pytest

from monitor.alerts import history, message, noise_report, plan, quiet, record, release_at
from pipeline.db import connect


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def alert(key, tier="important", severity=1.0, symbol="AAPL", **kw):
    return {"key": key, "tier": tier, "severity": severity, "symbol": symbol, "subject": f"{key} subject",
            "text": f"{key} text", **kw}


DAY = utc("2026-10-26 15:00")  # 10 am CDT


def run(con, alerts, now, snoozed=None):
    """One run: plan, 'deliver', record. Returns {channel: message}."""
    out = plan(alerts, history(con), now, snoozed)
    for _, rows in out.values():
        record(con, rows)
    return {ch: msg for ch, (msg, _) in out.items()}


@pytest.mark.parametrize("when, expect", [
    ("2026-10-26 01:59", False), ("2026-10-26 02:00", True),  # 8:59 / 9:00 pm CDT
    ("2026-10-26 11:59", True), ("2026-10-26 12:00", False),  # 6:59 / 7:00 am CDT
    ("2026-12-01 12:59", True), ("2026-12-01 13:00", False),  # 6:59 / 7:00 am CST
    ("2026-12-01 02:59", False), ("2026-12-01 03:00", True),  # 8:59 / 9:00 pm CST
    ("2026-03-08 11:59", True), ("2026-03-08 12:00", False),  # spring forward day: 7 am is already CDT
    ("2026-11-01 12:59", True), ("2026-11-01 13:00", False),  # fall back day: 7 am is CST
    ("2026-11-01 06:30", True), ("2026-11-01 07:30", True),  # the 1:30 am that happens twice
])
def test_quiet_hours_follow_central_time_through_dst(when, expect):
    assert quiet(utc(when)) is expect


@pytest.mark.parametrize("held, released", [
    ("2026-10-26 15:00", "2026-10-26 15:00"),  # daytime: no wait
    ("2026-03-07 04:00", "2026-03-07 13:00"),  # Fri 10 pm CST -> Sat 7 am CST
    ("2026-03-08 04:00", "2026-03-08 12:00"),  # Sat 10 pm CST -> Sun 7 am CDT, only 8 hours later
    ("2026-03-08 08:30", "2026-03-08 12:00"),  # 3:30 am CDT, just after the jump
    ("2026-11-01 03:00", "2026-11-01 13:00"),  # Sat 10 pm CDT -> Sun 7 am CST, 10 hours later
    ("2026-11-01 07:30", "2026-11-01 13:00"),  # the second 1:30 am
])
def test_release_at_7am_central(held, released):
    assert release_at(utc(held)) == utc(released)


def test_no_repeats_unless_worse(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    assert run(con, [alert("drop:AAPL", "urgent", 0.10)], DAY)["email"]
    assert run(con, [alert("drop:AAPL", "urgent", 0.10)], DAY + timedelta(hours=1)) == {"email": None, "discord": None}
    assert run(con, [alert("drop:AAPL", "important", 0.50)], DAY + timedelta(hours=2))["email"] is None  # lower tier
    worse = run(con, [alert("drop:AAPL", "urgent", 0.14)], DAY + timedelta(hours=3))
    assert "drop:AAPL text" in worse["email"]["body"] and "drop:AAPL text" in worse["discord"]["body"]
    reasons = [(h["status"], h["reason"]) for h in history(con) if h["channel"] == "email"]
    assert reasons == [("sent", None), ("suppressed", "repeat"), ("suppressed", "repeat"), ("sent", None)]


def test_one_grouped_message_per_channel_worst_first(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    alerts = [alert("brief", "informational", report=True), alert("summary", "important", symbol=None),
              alert("8k:XYZ", "urgent", symbol="XYZ"), alert("8k:XYZ", "urgent", 0.5, symbol="XYZ")]
    out = run(con, alerts, DAY)
    assert out["email"]["subject"] == "StockWatch: 1 urgent, 1 important, 1 informational"
    assert out["email"]["body"].index("8k:XYZ") < out["email"]["body"].index("summary") < out["email"]["body"].index("brief")
    assert "brief text" in out["email"]["body"] and "brief text" not in out["discord"]["body"]
    assert "(full report by email)" in out["discord"]["body"]
    assert len([h for h in history(con) if h["status"] == "sent"]) == 6  # 3 alerts x 2 channels, one message each


def test_quiet_hours_hold_then_release_at_7am(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    night = utc("2026-11-01 03:30")  # Sat 10:30 pm CDT; the clocks go back overnight
    assert run(con, [alert("drop:AAPL", "urgent", 0.10), alert("summary", "important")], night) == \
        {"email": None, "discord": None}
    assert run(con, [alert("drop:AAPL", "urgent", 0.10)], night + timedelta(hours=2))["email"] is None  # no double hold
    assert run(con, [alert("drop:AAPL", "urgent", 0.12)], night + timedelta(hours=3))["email"] is None  # held, worse
    assert run(con, [], utc("2026-11-01 12:59"))["email"] is None  # 6:59 am CST: still quiet
    out = run(con, [], utc("2026-11-01 13:05"))  # 7:05 am CST
    assert out["email"]["body"].count("drop:AAPL text") == 1
    sent = [h for h in history(con) if h["status"] == "sent" and h["channel"] == "email"]
    assert sorted((h["key"], h["severity"], h["reason"]) for h in sent) == [("drop:AAPL", 0.12, "released"),
                                                                           ("summary", 1.0, "released")]
    held_ids = {h["id"] for h in history(con) if h["status"] == "held"}
    assert all(h["held_id"] in held_ids for h in sent)
    assert run(con, [], utc("2026-11-01 14:05")) == {"email": None, "discord": None}  # released once only


def test_held_alert_superseded_by_worse_daytime_alert(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    run(con, [alert("drop:AAPL", "urgent", 0.10)], utc("2026-10-27 04:00"))  # 11 pm CDT
    out = run(con, [alert("drop:AAPL", "urgent", 0.15)], utc("2026-10-27 12:30"))  # 7:30 am
    assert out["email"]["body"].count("drop:AAPL text") == 1
    assert [h["severity"] for h in history(con) if h["status"] == "sent" and h["channel"] == "email"] == [0.15]
    assert run(con, [], utc("2026-10-27 13:30"))["email"] is None


def test_snooze_lets_urgent_through(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    snoozed = {"AAPL": DAY + timedelta(days=3)}
    out = run(con, [alert("news:AAPL", "important"), alert("8k:AAPL", "urgent"), alert("news:MSFT", symbol="MSFT")],
              DAY, snoozed)
    assert "8k:AAPL" in out["email"]["body"] and "news:MSFT" in out["email"]["body"]
    assert "news:AAPL" not in out["email"]["body"]
    later = run(con, [alert("news:AAPL", "important")], DAY + timedelta(days=4), snoozed)  # snooze over
    assert "news:AAPL" in later["discord"]["body"]


def test_alert_rows_are_append_only_and_need_aware_time(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    run(con, [alert("x")], DAY)
    with pytest.raises(Exception, match="append-only"):
        con.execute("UPDATE alerts SET sent_at = NULL")
    with pytest.raises(ValueError):
        plan([alert("x")], [], datetime(2026, 10, 26, 10))
    with pytest.raises(ValueError):
        plan([alert("x", "loud")], [], DAY)
    assert json.loads(con.execute("SELECT body FROM alerts").fetchone()[0])["status"] == "sent"


def test_monthly_noise_report(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    run(con, [alert("a", "urgent"), alert("b")], DAY)
    run(con, [alert("a", "urgent")], DAY + timedelta(hours=1))
    run(con, [alert("c", "informational")], utc("2026-10-27 04:00"))  # held overnight
    run(con, [], utc("2026-10-27 13:00"))
    run(con, [alert("d")], utc("2026-11-01 04:30"))  # Oct 31, 11:30 pm CDT: counts in October
    run(con, [alert("e")], utc("2026-11-02 15:00"))
    r = noise_report(history(con), "2026-10")
    assert r == {"month": "2026-10", "sent": 6, "by_tier": {"urgent": 2, "important": 2, "informational": 2},
                 "by_channel": {"email": 3, "discord": 3}, "messages": 4, "held": 4, "suppressed": {"repeat": 2}}
    assert noise_report(history(con), "2026-11")["sent"] == 4  # e, plus d released Nov 2


def test_message_counts():
    m = message([alert("a", "important"), alert("b", "important")], "email")
    assert m["subject"] == "StockWatch: 2 important"
