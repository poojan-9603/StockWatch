"""Alert rules: tiers, no repeats unless worse, one grouped message per channel per run, quiet hours, per-holding
snooze and the monthly noise report. Pure functions over plain data, plus reading and appending the alerts table.

An alert is a dict: key (what it is about; the same key is a repeat, so "drop:AAPL:2026-10-26" repeats within a day
and "drawdown:AAPL" until it clears), tier (urgent, important, informational), severity (a number, bigger is worse,
like the size of a drop), symbol (or None), subject, text, and report=True for a full report (email gets it whole,
Discord a pointer). Worse means a higher tier, or the same tier with a higher severity.

Every decision is an alerts row: tier, channel, subject, and body = the alert as JSON plus status (sent, held or
suppressed) and reason; sent_at is set on sent rows only. Alerts made in quiet hours (9 pm to 7 am Central) are held
and go out on the first run at or after 7 am, unless something worse for the same key has gone out since.
"""
import json
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
TIERS = ("informational", "important", "urgent")
CHANNELS = ("email", "discord")
FIELDS = ("key", "tier", "severity", "symbol", "subject", "text", "report")


def quiet(now):
    h = now.astimezone(CENTRAL).hour
    return h >= 21 or h < 7


def release_at(now):
    """When an alert made at `now` may go out: now, or the next 7:00 am Central in quiet hours."""
    if not quiet(now):
        return now
    local = now.astimezone(CENTRAL)
    return datetime.combine(local.date() + timedelta(days=local.hour >= 21), time(7), CENTRAL)


def rank(a):
    return TIERS.index(a["tier"]), a["severity"]


def history(con):
    """Every alerts row as a dict: id, created_at, channel, sent_at, and the alert fields, status and reason."""
    return [{"id": i, "created_at": c, "channel": ch, "sent_at": s, **json.loads(b)}
            for i, c, ch, s, b in con.execute("SELECT id, created_at, channel, sent_at, body FROM alerts ORDER BY id")]


def row(a, channel, stamp, status, reason=None, held_id=None):
    body = {**{k: a.get(k) for k in FIELDS}, "status": status, "reason": reason, "held_id": held_id}
    return {"created_at": stamp, "tier": a["tier"], "channel": channel, "subject": a["subject"],
            "body": json.dumps(body), "sent_at": stamp if status == "sent" else None}


def message(alerts, channel):
    """One message for a channel: worst first. Email carries full text, Discord points full reports to email."""
    alerts = sorted(alerts, key=rank, reverse=True)
    n = Counter(a["tier"] for a in alerts)
    subject = "StockWatch: " + ", ".join(f"{n[t]} {t}" for t in reversed(TIERS) if n[t])
    if channel == "email":
        body = "\n\n".join(f"[{a['tier'].upper()}] {a['subject']}\n{a['text']}" for a in alerts)
    else:
        body = "\n".join(f"**{a['tier'].upper()}** {a['subject']}" +
                         (" (full report by email)" if a.get("report") else f"\n{a['text']}") for a in alerts)
    return {"subject": subject, "body": body}


def plan(alerts, past, now, snoozed=None):
    """alerts: this run's alerts. past: history(con). now: aware datetime. snoozed: {symbol: until (aware datetime)},
    which urgent alerts ignore. Returns {channel: (message or None, rows)}. Deliver each message, then record its rows;
    if delivery fails, record nothing for that channel, so the next run tries again."""
    if now.tzinfo is None:
        raise ValueError("now must carry a time zone")
    for a in alerts:
        if a["tier"] not in TIERS:
            raise ValueError(f"unknown tier {a['tier']} on {a['key']}")
    stamp, snoozed, out = now.astimezone(timezone.utc).isoformat(timespec="seconds"), snoozed or {}, {}
    for ch in CHANNELS:
        sent, pending, rows, new = {}, {}, [], {}
        for h in (h for h in past if h["channel"] == ch):
            if h["status"] == "sent":
                sent[h["key"]] = h
                pending.pop(h["key"], None)
            elif h["status"] == "held" and (h["key"] not in pending or rank(h) > rank(pending[h["key"]])):
                pending[h["key"]] = h
        for a in sorted(alerts, key=rank, reverse=True):
            prior = [x for x in (sent.get(a["key"]), pending.get(a["key"]), new.get(a["key"])) if x]
            if a["tier"] != "urgent" and now < snoozed.get(a.get("symbol"), now):
                rows.append(row(a, ch, stamp, "suppressed", "snoozed"))
            elif prior and rank(a) <= max(map(rank, prior)):
                rows.append(row(a, ch, stamp, "suppressed", "repeat"))
            else:
                new[a["key"]] = a
        if quiet(now):
            out[ch] = (None, rows + [row(a, ch, stamp, "held") for a in new.values()])
            continue
        release = [h for k, h in pending.items() if k not in new]
        rows += [row(a, ch, stamp, "sent") for a in new.values()]
        rows += [row(h, ch, stamp, "sent", "released", h["id"]) for h in release]
        items = list(new.values()) + release
        out[ch] = (message(items, ch) if items else None, rows)
    return out


def record(con, rows):
    con.executemany("INSERT INTO alerts (created_at, tier, channel, subject, body, sent_at) "
                    "VALUES (:created_at, :tier, :channel, :subject, :body, :sent_at)", rows)
    con.commit()


def noise_report(past, month):
    """Counts for one Central-time month (YYYY-MM): sent by tier and channel, messages, held, suppressed by reason."""
    rows = [h for h in past if datetime.fromisoformat(h["created_at"]).astimezone(CENTRAL).strftime("%Y-%m") == month]
    sent = [h for h in rows if h["status"] == "sent"]
    return {"month": month, "sent": len(sent), "by_tier": dict(Counter(h["tier"] for h in sent)),
            "by_channel": dict(Counter(h["channel"] for h in sent)),
            "messages": len({(h["channel"], h["sent_at"]) for h in sent}),
            "held": sum(h["status"] == "held" for h in rows),
            "suppressed": dict(Counter(h["reason"] for h in rows if h["status"] == "suppressed"))}
