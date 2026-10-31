"""The site's data files (ARCHITECTURE "Data contract"), built from the records and the nightly's outputs, checked
against contract/ before anything is published (pipeline.contract.validate_dir).

Reads:  db/app.sqlite (paper verdicts, alerts, panel_opinions), market/scores/latest.parquet, market/paper/state.json
        (quant.live), market/panel/latest.json (agents.weekly, if any), market/quality/daily/<date>.parquet,
        logs/actions.json (GitHub's run history, for the broken-app count), logs/restore/latest.json (restore.yml),
        config/settings.json (managed money and mode, edited in the repo for now: docs/M6.md)
Writes: OUT_DIR/site/data/{today,picks,holdings,settings,health}.json, holding/<cik>.json, reports/{weekly,monthly}.json

The book shown is the paper book while settings say paper. All words are templates over the numbers (PLAN Step 4).

Usage: python -m pipeline.site OUT_DIR [blocked-reason]
"""
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from monitor import failures
from pipeline import contract
from pipeline.db import connect
from quant import backtest as bt, signals as sg

ROOT = Path(__file__).resolve().parent.parent
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
TIER = {"informational": "info", "important": "important", "urgent": "urgent"}
SIGNAL_NAMES = {"momentum": "momentum", "quality": "quality", "value": "value", "low_volatility": "low volatility",
                "earnings_surprise": "earnings surprise", "issuance": "issuance", "asset_growth": "asset growth"}
CAP_PCT = 100 * min(bt.FROZEN["cap"], bt.FROZEN["cap_x"] * bt.FROZEN["avg"])
PICKS = 10


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def symbol_or_none(s):
    return s if isinstance(s, str) and SYMBOL.match(s) else None


def text_or(v, default):
    """v when it's real text; a missing value from pandas is NaN, which `or` would let through."""
    return v if isinstance(v, str) and v.strip() else default


def paper_verdicts(con):
    rows = []
    for d, c, s, v, sc, g in con.execute("SELECT date, cik, symbol, verdict, score, gates FROM verdicts ORDER BY id"):
        g = json.loads(g or "{}")
        if g.get("book") == "paper":
            rows.append({"date": d, "cik": c, "symbol": s, "verdict": v, "score": sc, "gates": g})
    return rows


def math_block(r, bar, bad_mood, room):
    """The contract's math object from one scores row (FROZEN's buy rule; no news source yet, no timing layer)."""
    ranks = {k: (None if pd.isna(r.get(k)) else round(float(r[k]))) for k in sg.SIGNALS}
    present = [k for k, v in ranks.items() if v is not None]
    score = None if pd.isna(r.get("score")) else round(float(r["score"]), 1)
    above = sorted(((v, k) for k, v in ranks.items() if v is not None and v > 60), reverse=True)
    reasons = [f"Score {score:.0f} of 100, {'above' if score >= bar else 'below'} the buy line of {bar:g}."
               if score is not None else "Not scored: fewer than 5 of 7 signals available."]
    if above:
        top = ", ".join(f"{SIGNAL_NAMES[k]} {v}" for v, k in above[:2])
        reasons.append(f"{len(above)} of 7 signals above 60. Strongest: {top}.")
    else:
        reasons.append("No signal above 60.")
    if bad_mood:
        reasons.append("Market filter: bad mood, so the buy line is raised and new positions are half size.")
    reasons.append("No news check yet (no news source).")
    return {"score": score, "ranks": ranks, "signals_present": len(present), "signals_above_60": len(above),
            "missing": [f"{SIGNAL_NAMES[k]}: not enough data" for k in sg.SIGNALS if k not in present],
            "news_effect": "none", "news_note": "No news source yet.",
            "gates": {"score": score is not None and score >= bar,
                      "breadth": len(above) >= bt.FROZEN["n60"], "news": None, "timing": None,
                      "market": True, "room": room},
            "reasons": reasons}


def horizon(r):
    ranks = {k: r.get(k) for k in sg.SIGNALS if pd.notna(r.get(k))}
    if not ranks:
        return None
    top = max(ranks, key=ranks.get)
    return "3 to 9 months (momentum-led)" if top in ("momentum", "earnings_surprise", "low_volatility") \
        else "1 to 3 years (quality and value-led)"


def review(day):
    return (date.fromisoformat(day) + timedelta(days=91)).isoformat()


def ratio(series, frm, to):
    s = dict(series)
    keys = [k for k in s if k >= frm]
    return (s[to] / s[keys[0]] - 1) * 100 if keys and to in s else 0.0


def build(out, blocked=None):
    m = out / "market"
    gen = now_ts()
    settings = json.loads((ROOT / "config" / "settings.json").read_text())
    con = connect(out / "db" / "app.sqlite")
    sc = pd.read_parquet(m / "scores" / "latest.parquet")
    state = json.loads((m / "paper" / "state.json").read_text())
    day, mkt = state["day"], state["market"]
    panels = {}
    pf = m / "panel" / "latest.json"
    if pf.exists():
        panels = {s["cik"]: s["panel"] for s in json.loads(pf.read_text())["stocks"]}
    rows = {int(r["cik"]): r for r in sc.to_dict("records")}
    bar = bt.FROZEN["buy"] + (bt.FROZEN["soft_raise"] if mkt["bad_mood"] else 0)
    verdicts = paper_verdicts(con)
    last_decision = max((v["date"] for v in verdicts), default=None)
    latest_v = {v["cik"]: v for v in verdicts if v["date"] == last_decision}
    positions = state.get("positions", [])
    held = {p["cik"] for p in positions}
    managed = float(settings["managed_usd"])
    nav = state.get("nav", [])
    value = managed * (nav[-1][1] if nav else 1.0)
    files = {}

    # holdings and one detail file each
    hold_items, peak = [], max((v for _, v in nav), default=1.0)
    for p in positions:
        r = rows.get(p["cik"], {})
        v = latest_v.get(p["cik"], {})
        verdict = v.get("verdict") if v.get("verdict") in ("Hold", "Trim") else "Hold"
        val = value * p["weight_pct"] / 100
        shares = val / p["close"] if p.get("close") else 0.0
        spy_ret = ratio(state["spy"], p["bought"], day) if p.get("bought") else 0.0
        mb = math_block(r, bar, mkt["bad_mood"], True)
        item = {"symbol": symbol_or_none(p["symbol"]) or "UNKNOWN", "cik": p["cik"],
                "name": text_or(r.get("name"), str(p["symbol"])),
                "verdict": verdict, "own_pick": False, "shares": round(shares, 4), "value_usd": round(val, 2),
                "weight_pct": p["weight_pct"], "target_pct": p.get("target_pct"),
                "day_change_pct": p["day_change_pct"], "return_pct": p["return_pct"],
                "sp500_return_pct": round(spy_ret, 2), "math": mb, "panel": panels.get(p["cik"]),
                "flags": []}
        hold_items.append(item)
        bought = p.get("bought") or day
        files[f"holding/{p['cik']}"] = {
            "generated_at": gen, "as_of": day, "symbol": item["symbol"], "cik": p["cik"], "name": item["name"],
            "sector": text_or(r.get("sector"), "Unknown"), "verdict": verdict, "own_pick": False, "math": mb,
            "panel": item["panel"], "flags": [], "horizon": horizon(r), "review_date": review(bought),
            "shares": item["shares"], "value_usd": item["value_usd"], "weight_pct": item["weight_pct"],
            "target_pct": item["target_pct"], "cap_pct": CAP_PCT, "return_pct": item["return_pct"],
            "sp500_return_pct": item["sp500_return_pct"],
            "why_bought": {"date": bought, "score": p.get("buy_score"),
                           "reasons": [f"Paper buy at the {bought} month-end close: score {p['buy_score']:.0f}, "
                                       f"{p.get('target_pct')}% of the book."] if p.get("buy_score") else []},
            "lots": [{"lot_id": f"paper-{p['cik']}", "bought": bought, "shares": item["shares"],
                      "cost_per_share": round(p["close"] / (1 + p["return_pct"] / 100), 4) if p.get("close") else 0.0,
                      "return_pct": p["return_pct"],
                      "long_term_from": (date.fromisoformat(bought) + timedelta(days=366)).isoformat(),
                      "long_term": False}],
            "prices": [{"date": t, "close": c} for t, c in p.get("prices", [])], "news": []}
    dd = 100 * (1 - (nav[-1][1] / peak)) if nav else 0.0
    files["holdings"] = {"generated_at": gen, "as_of": day, "managed_usd": managed, "value_usd": round(value, 2),
                         "cash_usd": round(value * state.get("cash_pct", 100.0) / 100, 2),
                         "since": state.get("since", day),
                         "return_pct": round(ratio(nav, state.get("since", day), day), 2) if nav else 0.0,
                         "sp500_return_pct": round(ratio(state.get("spy", []), state.get("since", day), day), 2)
                         if nav else 0.0,
                         "drawdown_pct": round(dd, 2), "drawdown_alarm": dd >= 20, "holdings": hold_items}

    # picks: the last month-end's paper buys, then the best-scored stocks passing the rule, not held
    picks = []
    for v in (v for v in verdicts if v["date"] == last_decision and v["verdict"] == "Buy"):
        picks.append((v["cik"], "Buy", v["gates"].get("size_pct")))
    ok = sc[(sc["score"] >= bar) & (sc["n60"] >= bt.FROZEN["n60"]) & ~sc["cik"].isin(list(held))]
    for c in ok.nlargest(PICKS, "score")["cik"]:
        if int(c) not in {x[0] for x in picks}:
            picks.append((int(c), "Wait", None))
    pick_items = []
    for c, verdict, size in picks:
        r = rows.get(c)
        if r is None or not symbol_or_none(r.get("symbol")):
            continue
        mb = math_block(r, bar, mkt["bad_mood"], True)
        if verdict == "Wait":
            mb["reasons"].append("Decisions are made at the month-end close (the frozen design rebalances monthly).")
        pick_items.append({"symbol": r["symbol"], "cik": c, "name": text_or(r.get("name"), r["symbol"]),
                           "sector": text_or(r.get("sector"), "Unknown"), "verdict": verdict, "small_cap": not r.get("sp500"),
                           "catalyst": False, "math": mb, "panel": panels.get(c), "horizon": horizon(r),
                           "review_date": review(day), "size_pct": size, "flags": []})
    files["picks"] = {"generated_at": gen, "as_of": day, "market_bad_mood": mkt["bad_mood"], "picks": pick_items}

    # today
    sym_cik = {r["symbol"]: int(c) for c, r in rows.items() if isinstance(r.get("symbol"), str)}
    recent = (date.fromisoformat(day) - timedelta(days=3)).isoformat()
    alerts = []
    for created, tier, ch, subj, body, sent in con.execute(
            "SELECT created_at, tier, channel, subject, body, sent_at FROM alerts WHERE channel = 'app' "
            "AND sent_at IS NOT NULL ORDER BY id DESC LIMIT 200"):
        b = json.loads(body)
        if sent[:10] < recent or b.get("status") != "sent":
            continue
        s = symbol_or_none(b.get("symbol"))
        alerts.append({"tier": TIER[tier], "at": sent.replace("+00:00", "Z"), "symbol": s,
                       "cik": sym_cik.get(s), "title": subj, "body": b.get("text") or "", "catalyst": False})
        if len(alerts) == 20:
            break
    acts = [{"symbol": v["symbol"], "cik": v["cik"], "verdict": v["verdict"], "summary": action_text(v)}
            for v in verdicts if v["date"] >= recent and v["verdict"] in ("Buy", "Sell", "Trim")
            and symbol_or_none(v["symbol"])]
    quiet = [{"kind": "market", "text": mkt["note"], "outcome": None}]
    for p in sorted(positions, key=lambda x: -abs(x["day_change_pct"]))[:5]:
        quiet.append({"kind": "holding", "text": f"{p['symbol']} {p['day_change_pct']:+.1f}% today, "
                                                 f"{p['return_pct']:+.1f}% since bought.", "outcome": None})
    if not state.get("started"):
        quiet.append({"kind": "change", "text": "The paper book starts at the first month-end close.",
                      "outcome": None})
    files["today"] = {"generated_at": gen, "as_of": day, "status": "blocked" if blocked else
                      ("action" if acts else "quiet"), "blocked_reason": blocked, "market": mkt,
                      "alerts": alerts, "actions": acts, "quiet": quiet[:10]}

    files["settings"] = {"generated_at": gen, "as_of": day, "mode": settings["mode"], "managed_usd": managed,
                         "quiet_hours": {"start": "21:00", "end": "07:00", "tz": "America/Chicago"},
                         "channels": {"email": False, "discord": False}, "snoozed": [],
                         "llm_cap_usd": float(settings["llm_cap_usd"]), "test_years_locked": True}
    broken = broken_app(out, con, gen, float(settings["llm_cap_usd"]))
    files["health"] = health(out, con, gen, day, blocked, sym_cik, broken)
    files["reports/weekly"] = weekly(gen, day, verdicts, panels, latest_v)
    files["reports/monthly"] = monthly(con, gen, day, state, settings, broken)
    site = out / "site" / "data"
    for name, obj in files.items():
        p = site / f"{name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=1, default=float))
    n = contract.validate_dir(site)
    print(f"site data: {n} files checked against the contract, as of {day}")
    return files


def action_text(v):
    g = v["gates"]
    if v["verdict"] == "Buy":
        return f"Paper buy at the {v['date']} close, {g.get('size_pct')}% of the book. Fills at the next open."
    if v["verdict"] == "Sell":
        return f"Paper sell at the {v['date']} close: {g.get('reason', 'see the verdict log')}."
    return f"Paper trim to {g.get('to_pct')}% of the book at the {v['date']} close."


def llm_month(con, month):
    total = 0.0
    for created, claims in con.execute("SELECT created_at, claims FROM panel_opinions"):
        if created[:7] == month:
            total += json.loads(claims or "{}").get("cost") or 0.0
    return round(total, 4)


def broken_app(out, con, gen, cap):
    """The broken-app check (monitor.failures) as a Health check row."""
    name = "Broken-app failures, last 30 days"
    runs = out / "logs" / "actions.json"
    if not runs.exists():
        return {"name": name, "outcome": "log", "detail": "Not measured tonight: GitHub's run history couldn't be read."}
    found = failures.find(json.loads(runs.read_text()), datetime.fromisoformat(gen.replace("Z", "+00:00")),
                          llm_month(con, gen[:7]), cap)
    return {"name": name, "outcome": "log" if found else "pass", "detail": failures.summary(found)}


def health(out, con, gen, day, blocked, sym_cik, broken):
    q = out / "market" / "quality" / "daily" / f"{gen[:10]}.parquet"
    issues = pd.read_parquet(q) if q.exists() else pd.DataFrame(columns=["severity", "symbol", "detail", "check"])
    counts = issues["severity"].value_counts().to_dict()
    checks = [{"name": "Price sanity and big moves", "outcome": "quarantine" if counts.get("quarantine") else "pass",
               "detail": f"{counts.get('quarantine', 0)} quarantined, {counts.get('log', 0)} logged"},
              {"name": "Completeness", "outcome": "block" if counts.get("block") else "pass",
               "detail": blocked or ""}, broken]
    restore = out / "logs" / "restore" / "latest.json"
    last = lambda sql: (con.execute(sql).fetchone() or [None])[0]
    watch = last("SELECT MAX(created_at) FROM alerts")
    weekly_run = last("SELECT MAX(created_at) FROM panel_opinions")
    fix = lambda t: t.replace("+00:00", "Z") if t else None
    backup = out / "backups" / "daily" / f"{gen[:10]}.sqlite"
    return {"generated_at": gen, "as_of": day, "run_id": f"nightly-{os.environ.get('GITHUB_RUN_ID', 'local')}",
            "status": "blocked" if blocked else ("warn" if counts.get("quarantine") else "ok"),
            "summary": blocked or f"Nightly done. {counts.get('quarantine', 0)} price issues quarantined.",
            "jobs": [{"name": "Nightly", "last_run": gen, "ok": not blocked, "note": blocked},
                     {"name": "Holdings watch", "last_run": fix(watch), "ok": True,
                      "note": "Time of the last alert it recorded."},
                     {"name": "Weekly panel", "last_run": fix(weekly_run), "ok": True,
                      "note": None if weekly_run else "Not run yet."}],
            "checks": checks,
            "quarantined": [{"symbol": r.symbol, "cik": sym_cik[r.symbol], "reason": str(r.detail)}
                            for r in issues[issues["severity"] == "quarantine"].itertuples()
                            if r.symbol in sym_cik and symbol_or_none(r.symbol)],
            "llm_month_usd": llm_month(con, gen[:7]),
            "last_backup": gen if backup.exists() else None,
            "last_restore_test": json.loads(restore.read_text()) if restore.exists() else None}


def weekly(gen, day, verdicts, panels, latest_v):
    d = date.fromisoformat(day)
    week_of = (d - timedelta(days=d.weekday())).isoformat()
    acts = [{"symbol": v["symbol"], "cik": v["cik"], "verdict": v["verdict"], "summary": action_text(v)}
            for v in verdicts if v["date"] >= week_of and v["verdict"] in ("Buy", "Sell", "Trim")
            and symbol_or_none(v["symbol"])]
    dis = []
    for c, p in panels.items():
        if not p or p["verdict"] == "Not enough information" or p.get("agrees") is not False:
            continue
        mv = latest_v.get(c, {}).get("verdict", "Wait")
        sym = latest_v.get(c, {}).get("symbol")
        if symbol_or_none(sym):
            dis.append({"symbol": sym, "cik": c, "math_verdict": mv, "panel_verdict": p["verdict"],
                        "note": "Panel view is low confidence after a flip on rerun." if p["low_confidence"]
                        else p["summary"]})
    n = {k: sum(a["verdict"] == k for a in acts) for k in ("Buy", "Sell", "Trim")}
    return {"generated_at": gen, "as_of": day, "week_of": week_of,
            "summary": f"{n['Buy']} paper buys, {n['Sell']} sells, {n['Trim']} trims this week."
            if acts else "No paper actions this week. Decisions come at the month-end close.",
            "actions": acts, "changes": [], "disagreements": dis}


def stats(series):
    s = pd.Series({t: v for t, v in series})
    if len(s) < 2:
        return 0.0, None, 0.0
    r = s.pct_change().dropna()
    sharpe = round(float(r.mean() / r.std() * math.sqrt(252)), 2) if len(r) >= 60 and r.std() > 0 else None
    return round(100 * (s.iloc[-1] / s.iloc[0] - 1), 2), sharpe, round(float(100 * -(s / s.cummax() - 1).min()), 2)


def luck(nav, spy, ret, sret):
    """How much of the gap to SPY is still within luck: the t-statistic of daily returns over SPY's."""
    days = len(nav)
    ex = (pd.Series(dict(nav)).pct_change() - pd.Series(dict(spy)).pct_change()).dropna()
    head = f"{days} trading day{'s' if days != 1 else ''} in"
    tail = " The plan reviews at 12, 24 and 36 months."
    if len(ex) < 20 or ex.std() == 0:
        return f"{head}: far too early to tell skill from luck.{tail}"
    t, gap = float(ex.mean() / ex.std() * math.sqrt(len(ex))), ret - sret
    side = "ahead of" if gap >= 0 else "behind"
    verdict = ("Within luck: it takes a t of about 2 either way to say more." if abs(t) < 2 else
               "Bigger than luck usually explains, though a few months still say little.")
    return f"{head}, {abs(gap):.1f} points {side} the S&P 500 (t = {t:.2f}). {verdict}{tail}"


def panel_note(pr):
    settled = pr.get("panel_right", 0) + pr.get("math_right", 0)
    open_ = pr.get("disagreements", 0) - settled - pr.get("unpriced", 0)
    head = (f"Panel record: {pr.get('disagreements', 0)} disagreements with the math, {settled} settled "
            f"(the panel right in {pr.get('panel_right', 0)}), {open_} still inside their 3 months")
    unpriced = f", {pr['unpriced']} with no prices to settle" if pr.get("unpriced") else ""
    rule = (" It earns a vote with 30 settled and a binomial p under 0.05" +
            (f" (p = {pr['p_value']:.3f} now)." if settled else "."))
    return head + unpriced + "." + rule


def monthly(con, gen, day, state, settings, broken):
    month = day[:7]
    nav, spy, rsp = state.get("nav", []), state.get("spy", []), state.get("rsp", [])
    ret, sharpe, mdd = stats(nav)
    sret, ssharpe, smdd = stats(spy)
    rret = stats(rsp)[0]
    sb = state.get("scoreboard", {})
    sl = sb.get("slices", {})
    pr = state.get("panel_record", {})
    # the plan's definition of winning, judged from its first review (12 months); before that it's too early
    winning = None if len(nav) < 252 else bool(ret > sret and (sharpe or 0) >= (ssharpe or 0) and mdd <= 1.5 * smdd)
    sent = {"urgent": 0, "important": 0, "info": 0}
    for created, tier, body in con.execute("SELECT created_at, tier, body FROM alerts WHERE sent_at IS NOT NULL"):
        if created[:7] == month and json.loads(body).get("status") == "sent":
            sent[TIER[tier]] += 1
    slice_ = lambda name, k: {"name": name, "positions": sl.get(k, {}).get("positions", 0),
                              "return_pct": sl.get(k, {}).get("return_pct"),
                              "sp500_return_pct": sret if sl.get(k, {}).get("return_pct") is not None else None}
    return {"generated_at": gen, "as_of": day, "month": month, "mode": settings["mode"],
            "since": state.get("since", day), "return_pct": ret, "sp500_return_pct": sret,
            "sp500_equal_return_pct": rret, "sharpe": sharpe, "sp500_sharpe": ssharpe,
            "max_drawdown_pct": mdd, "sp500_max_drawdown_pct": smdd, "winning": winning,
            "luck_note": luck(nav, spy, ret, sret),
            "emergency_brake": bool(mdd > 1.5 * smdd and ret < sret - 10),
            "turnover_pct": sb.get("turnover_pct", 0.0), "costs_pct": sb.get("costs_pct", 0.0),
            "slices": [slice_("S&P 500 members", "sp500"), slice_("Outside the S&P 500 (untested)", "other"),
                       {"name": "Own picks", "positions": 0, "return_pct": None, "sp500_return_pct": None}],
            "panel_record": {k: pr.get(k, 0) for k in ("disagreements", "panel_right", "math_right")}
            | {"vote_earned": bool(pr.get("vote_earned"))},
            "alerts_sent": sent, "llm_usd": llm_month(con, month), "llm_cap_usd": float(settings["llm_cap_usd"]),
            "notes": ["Paper mode: the frozen design, filled at the next open after each month-end close.",
                      "Winning is judged from 12 months in, the plan's first review. Before that it shows as too early.",
                      panel_note(pr),
                      f"Broken-app failures: {broken['detail']}"]}


def main(out_dir, blocked=None):
    build(Path(out_dir), blocked or None)


if __name__ == "__main__":
    main(*sys.argv[1:3])
