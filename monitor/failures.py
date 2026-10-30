"""Broken-app failures (PLAN step 7, and M7's finish line: none in the last month), from GitHub's own run history
(`gh run list --json workflowName,event,status,conclusion,createdAt,url`) and the month's LLM spend:

- a scheduled run of a core job that failed or timed out (a data block fails the nightly, so it counts here)
- a trading day with no successful nightly at all (GitHub dropped the cron, or every try failed)
- LLM spend over the monthly cap

Cancelled runs alone don't count: the shared R2 lock cancels a queued run when a newer one arrives (docs/M2.md).
A cancelled nightly still shows up as a missing night. Manual runs never count; they're fixes and experiments."""
from datetime import datetime, timedelta, timezone

from quant.live import next_trading_day, trading_day

JOBS = ("nightly", "watch", "weekly", "restore", "prices")
BAD = ("failure", "timed_out", "startup_failure")
NIGHT_STARTS = 20  # UTC hour: a trading day's nightly counts from 20:00 that day to 20:00 on the next trading day
START = datetime(2026, 10, 30, tzinfo=timezone.utc)  # the paper period (M7) starts at the first paper month-end


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def find(runs, now, llm_usd=0.0, cap=10.0, days=30):
    """Failures in the `days` before `now` (from START at the earliest), oldest first: [{"date", "job", "what", "url"}]."""
    start = max(now - timedelta(days=days), START)
    out = [{"date": r["createdAt"][:10], "job": r["workflowName"], "what": f"scheduled run ended in {r['conclusion']}",
            "url": r.get("url")}
           for r in runs if r["event"] == "schedule" and r["workflowName"] in JOBS
           and r.get("status") == "completed" and r.get("conclusion") in BAD and ts(r["createdAt"]) >= start]
    good = [ts(r["createdAt"]) for r in runs if r["workflowName"] == "nightly" and r.get("conclusion") == "success"]
    d = start.date()
    while d < now.date():
        if trading_day(d):
            a = datetime(d.year, d.month, d.day, NIGHT_STARTS, tzinfo=timezone.utc)
            n = next_trading_day(d)
            b = datetime(n.year, n.month, n.day, NIGHT_STARTS, tzinfo=timezone.utc)
            if b <= now and not any(a <= t < b for t in good):
                out.append({"date": d.isoformat(), "job": "nightly", "what": f"no successful nightly for {d}",
                            "url": None})
        d += timedelta(days=1)
    if llm_usd > cap:
        out.append({"date": now.date().isoformat(), "job": "llm", "what": f"LLM spend ${llm_usd:.2f} is over the "
                    f"${cap:.2f} cap", "url": None})
    return sorted(out, key=lambda f: f["date"])


def summary(found):
    if not found:
        return "None in the last 30 days."
    return f"{len(found)} in the last 30 days: " + "; ".join(f"{f['date']} {f['job']}, {f['what']}" for f in found)
