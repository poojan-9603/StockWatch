"""The holdings watch (ARCHITECTURE "Jobs"): every hour on weekdays, 7 am to 6 pm Central.

For each holding (derived from trades, monitor.holdings):
  8-Ks      new filings from SEC submissions. Red-flag items are urgent; any other 8-K is informational.
  quotes    the live price against yesterday's close; a drop of 10% or more is urgent.
Alerts go through monitor.alerts.plan (no repeats unless worse, quiet hours, snooze) and out on both channels
(monitor.delivery); every decision is appended to the alerts table. The 7 am run is there to release what quiet
hours held (PLAN: held until 7 am). A source that fails is printed loudly and fails the run after the alerts that
did work are sent; nothing is guessed around.

Usage: python -m monitor.watch OUT_DIR [test]
  test  also sends one test alert, to prove both channels (held if it's quiet hours, released at 7 am)
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitor import alerts, delivery, holdings
from pipeline import db, sec

HOURS = range(7, 19)  # Central; GitHub's cron is UTC, so the schedule over-fires and due() decides
DROP = -0.10
LOOKBACK_DAYS = 4  # 8-Ks this recent are checked each run; already-sent ones are repeats (covers weekends, delays)
RED_FLAGS = {"1.03": "bankruptcy or receivership", "1.05": "material cybersecurity incident",
             "2.06": "material impairment", "3.01": "delisting notice or listing rule failure",
             "4.01": "change of auditor", "4.02": "past financial statements can no longer be relied on"}


def due(now):
    local = now.astimezone(alerts.CENTRAL)
    return local.weekday() < 5 and local.hour in HOURS


def filing_alerts(symbol, filings, since):
    out = []
    for f in filings:
        if f["filed"] < since:
            continue
        items = [i.strip() for i in (f["items"] or "").split(",") if i.strip()]
        flags = [i for i in items if i in RED_FLAGS]
        acc = f["accession"]
        url = f"https://www.sec.gov/Archives/edgar/data/{f['cik']}/{acc.replace('-', '')}/{acc}-index.htm"
        what = "; ".join(f"{i} {RED_FLAGS[i]}" for i in flags) or f"items {', '.join(items) or 'none listed'}"
        out.append({"key": f"8k:{acc}", "tier": "urgent" if flags else "informational", "severity": len(flags),
                    "symbol": symbol, "subject": f"{symbol}: {'red flag 8-K' if flags else 'new 8-K'} ({what})",
                    "text": f"{symbol} filed a {f['form']} on {f['filed']}: {what}.\n{url}", "report": False})
    return out


def drop_alert(symbol, last, prev, day):
    move = round(last / prev - 1, 6)  # exactly 10% must count; 90 / 100 - 1 is -0.0999...
    if move > DROP:
        return None
    return {"key": f"drop:{symbol}:{day}", "tier": "urgent", "severity": round(-move, 4), "symbol": symbol,
            "subject": f"{symbol} down {-move:.1%} today",
            "text": f"{symbol} is at {last:.2f}, down {-move:.1%} from yesterday's close of {prev:.2f}. "
                    "A big drop is a reason to review, not an automatic sell.", "report": False}


def quote(symbol):
    import yfinance  # only here: tests pass their own quotes
    q = yfinance.Ticker(symbol).fast_info
    last, prev = float(q.last_price), float(q.previous_close)
    if not (last > 0 and prev > 0):
        raise ValueError(f"bad quote {last} / {prev}")
    return last, prev


def submissions(cik):
    return sec.eight_ks(cik, sec.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
                        ["filings"]["recent"])


def gather(held, now, quote=quote, submissions=submissions):
    """(alerts, failures). held: {symbol: cik}."""
    day = now.astimezone(alerts.CENTRAL).date()
    since = (day - timedelta(days=LOOKBACK_DAYS)).isoformat()
    found, failures = [], []
    for symbol, cik in sorted(held.items()):
        try:
            if cik is None:
                raise ValueError("no CIK on the trade, so its filings can't be checked")
            found += filing_alerts(symbol, submissions(int(cik)), since)
        except Exception as e:
            failures.append(f"{symbol} 8-Ks: {type(e).__name__}: {e}")
        try:
            a = drop_alert(symbol, *quote(symbol), day.isoformat())
            found += [a] if a else []
        except Exception as e:
            failures.append(f"{symbol} quote: {type(e).__name__}: {e}")
    return found, failures


def test_alert(now):
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    return {"key": f"test:{stamp}", "tier": "important", "severity": 0, "symbol": None,
            "subject": "Test alert", "text": f"Made at {stamp} to check this channel works. Nothing to do.",
            "report": False}


def run(con, now, test=False, deliver=delivery.deliver, **sources):
    """Gather, plan, deliver, record. Returns a run log; raises after recording if anything failed."""
    as_of = now.astimezone(alerts.CENTRAL).date().isoformat()
    lots = holdings.holdings(holdings.trades(con), [], as_of)["lots"]
    held = {lot["symbol"]: lot.get("cik") for lot in lots}
    found, failures = gather(held, now, **sources) if held else ([], [])
    found += [test_alert(now)] if test else []
    log = {"at": now.isoformat(timespec="seconds"), "holdings": len(held), "alerts": len(found), "sent": {},
           "failures": failures}
    for ch, (msg, rows) in alerts.plan(found, alerts.history(con), now).items():
        if msg:
            try:
                deliver(ch, msg)
            except Exception as e:  # record nothing for this channel, so the next run tries again
                failures.append(f"{ch} delivery: {e}")
                continue
        alerts.record(con, rows)
        log["sent"][ch] = sum(json.loads(r["body"])["status"] == "sent" for r in rows)
    print(json.dumps(log, indent=1), flush=True)
    if failures:
        raise SystemExit("holdings watch failures:\n" + "\n".join(failures))
    return log


def main(out_dir, mode=""):
    now = datetime.now(timezone.utc)
    if mode != "test" and not due(now):
        print(f"outside the watch hours ({now.astimezone(alerts.CENTRAL):%a %H:%M} Central); nothing to do")
        return
    con = db.connect(Path(out_dir) / "db" / "app.sqlite")
    run(con, now, test=mode == "test")


if __name__ == "__main__":
    main(*sys.argv[1:3])
