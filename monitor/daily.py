"""The nightly alerts after live scoring (PLAN Step 5 tiers): the daily summary (important) and the drawdown alarm
(urgent, 20% below the peak: a review, never an automatic sell; one alert per peak, again only if it gets worse).
Both go through monitor.watch.send, so they follow the same rules and channels as the holdings watch. Text is code-built from the numbers (PLAN Step 4).

For now the book is the paper one (quant.live); real holdings get the same alarm once trades exist.
"""
from monitor import watch

DRAWDOWN = -20.0  # percent below the peak


def alerts_for(log):
    """Alerts from a quant.live run log."""
    day, p = log["day"], log.get("paper")
    lines = [f"Scores updated for {log['scored']:,} stocks (close of {day})."]
    if p:
        lines.append(f"Paper book since {p['since']}: {p['return_pct']:+.2f}% against SPY's {p['spy_return_pct']:+.2f}% "
                     f"({p['day_change_pct']:+.2f}% today), {p['positions']} positions, "
                     f"{-p['drawdown_pct']:.1f}% below its peak.")
    else:
        lines.append("The paper book starts at the first month-end.")
    if log.get("decisions"):
        d = log["decisions"]
        lines.append(f"Month-end decisions: {d['Buy']} buy, {d['Sell']} sell, {d['Trim']} trim, {d['Hold']} hold. "
                     "They fill at the next open.")
    lines += log.get("notes", [])
    out = [{"key": f"summary:{day}", "tier": "important", "severity": 0, "symbol": None,
            "subject": f"Daily summary for {day}", "text": "\n".join(lines), "report": False}]
    if p and p["drawdown_pct"] <= DRAWDOWN:
        out.append({"key": f"drawdown:paper:{p['peak_day']}", "tier": "urgent",
                    "severity": round(-p["drawdown_pct"], 2), "symbol": None,
                    "subject": f"Paper book {-p['drawdown_pct']:.1f}% below its peak",
                    "text": f"The paper book is {-p['drawdown_pct']:.1f}% below its peak (the alarm is at 20%). "
                            "Time for a review. Nothing is sold automatically.", "report": False})
    return out


def run(con, log, now):
    failures = []
    found = alerts_for(log)
    sent = watch.send(con, found, now, failures)
    if failures:
        raise SystemExit("nightly alerts failed:\n" + "\n".join(failures))
    return {"alerts": len(found), "sent": sent}
