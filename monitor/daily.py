"""The nightly alerts after live scoring (PLAN Step 5 tiers): the daily summary (important) and the drawdown alarm
(urgent, 20% below the peak: a review, never an automatic sell; one alert per peak, again only if it gets worse).
Both go through monitor.watch.send, so they follow the same rules and channels as the holdings watch. Text is
code-built from the numbers (PLAN Step 4).

Also, for real holdings: a split in the last 14 days of prices is informational; a ratio that isn't a clean split
(yfinance carries spin-offs that way, monitor.holdings) is important, for you to confirm what you received.

For now the book is the paper one (quant.live); real holdings get the same alarm once trades exist.
"""
from datetime import date, timedelta

import pyarrow.compute as pc
import pyarrow.dataset as ds

from monitor import holdings, watch

DRAWDOWN = -20.0  # percent below the peak


def alerts_for(log):
    """Alerts from a quant.live run log."""
    day, p = log["day"], log.get("paper")
    lines = [f"Scores updated for {log['scored']:,} stocks (close of {day})."]
    if p:
        lines.append(f"Paper book since {p['since']}: {p['return_pct']:+.2f}% against SPY's "
                     f"{p['spy_return_pct']:+.2f}% "
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


def action_alerts(con, prices_dir, day):
    """Splits and possible spin-offs on held stocks in the last 14 days of prices."""
    held = {lot["symbol"] for lot in holdings.holdings(holdings.trades(con), [], day)["lots"]}
    if not held:
        return []
    since = (date.fromisoformat(day) - timedelta(days=14)).isoformat()
    t = ds.dataset(prices_dir, partitioning="hive").to_table(
        columns=["symbol", "date", "split"], filter=pc.field("symbol").isin(sorted(held)) & (pc.field("date") >= since)
        & pc.field("split").is_valid() & (pc.field("split") != 1.0)).to_pandas()
    out = []
    for r in t.itertuples():
        clean = holdings.clean_ratio(r.split)
        out.append({"key": f"action:{r.symbol}:{r.date}", "tier": "informational" if clean else "important",
                    "severity": 0 if clean else 1, "symbol": r.symbol,
                    "subject": f"{r.symbol}: {'split' if clean else 'possible spin-off'} on {r.date}",
                    "text": f"{r.symbol} shows a {r.split:g} share factor on {r.date}. " + (
                        "That's a stock split." if clean else
                        "That isn't a clean split, which is how a spin-off shows up. Log what you received."),
                    "report": False})
    return out


def run(con, log, now, prices_dir=None):
    failures = []
    found = alerts_for(log) + (action_alerts(con, prices_dir, log["day"]) if prices_dir else [])
    sent = watch.send(con, found, now, failures)
    if failures:
        raise SystemExit("nightly alerts failed:\n" + "\n".join(failures))
    return {"alerts": len(found), "sent": sent}
