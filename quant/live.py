"""Live scoring and paper verdicts with the frozen design (paper mode, docs/M3.md and docs/LIVE.md).

Every night, for the latest trading day only (inside quant.lock.live, hard rule 1):
  scores    the seven signals and the re-ranked score for every stock in the live universe (PLAN Step 1), ranked
            across all of it -> market/scores/latest.parquet, for the site and the panel. Not a record.
On the month's last trading day, if the data is current (FROZEN rebalances monthly):
  snapshot  that day's scores -> market/scores/month_end/<day>.parquet, written once and never changed
  paper     quant.backtest.simulate replays FROZEN from the first snapshot over every snapshot, so the paper
            portfolio is a pure function of the snapshots and prices. Fills are at the next open, as in the backtest.
  verdicts  that day's decisions appended to the verdict log (hash chain): Buy, Sell (with the reason), Trim, Hold,
            book "paper". Earlier months' recorded Buys and Sells must match the replay, or nothing is written.
A month-end whose data arrives late gets no decisions, said loudly: deciding late but filling at the earlier open
would be a look-ahead. The news check is recorded as not checked (no news source yet; FROZEN had none).

Usage: python -m quant.live OUT_DIR
"""
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds
from dateutil.easter import easter

from agents.schema import direction
from monitor import daily
from pipeline.db import add_verdict, connect
from quant import backtest as bt, lock, signals as sg
from quant.measure import members
from quant.sectors import gics11

HISTORY_DAYS = 400  # calendar days of prices: 252 trading days for momentum and volatility, with room to spare
SETTLE_MONTHS, VOTE_MIN, VOTE_P = 3, 30, 0.05  # the panel record rule (docs/M7.md), set before any results


def _observed(d):
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def _nth(year, month, weekday, n):
    """The nth given weekday of a month (n = -1: the last)."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def holidays(year):
    """NYSE full-day closures by its published rules. A New Year's Day on a Saturday isn't made up on the Friday.
    Unscheduled closures (a national day of mourning) aren't known in advance."""
    ny = date(year, 1, 1)
    h = {_observed(ny) if ny.weekday() == 6 else ny, _nth(year, 1, 0, 3), _nth(year, 2, 0, 3),
         easter(year) - timedelta(days=2), _nth(year, 5, 0, -1), _observed(date(year, 7, 4)), _nth(year, 9, 0, 1),
         _nth(year, 11, 3, 4), _observed(date(year, 12, 25))}
    if year >= 2022:
        h.add(_observed(date(year, 6, 19)))
    return h


def trading_day(d):
    return d.weekday() < 5 and d not in holidays(d.year)


def next_trading_day(d):
    d += timedelta(days=1)
    while not trading_day(d):
        d += timedelta(days=1)
    return d


def month_end(day):
    d = date.fromisoformat(day)
    return trading_day(d) and next_trading_day(d).month != d.month


def current(day, now):
    """True if a decision at `day`'s close is still in time: before the next trading day's 9:30 am New York open,
    where it fills (13:30 UTC, the earlier of the two daylight-saving cases). A late GitHub start is fine; data that
    only arrives after that open is not."""
    nxt = next_trading_day(date.fromisoformat(day))
    return now < datetime(nxt.year, nxt.month, nxt.day, 13, 30, tzinfo=timezone.utc)


def latest_universe(m, day):
    files = sorted(p for p in (m / "universe").glob("*.parquet") if p.stem <= day[:7])
    if not files:
        raise SystemExit(f"no live universe file for {day[:7]} or before in market/universe")
    u = pd.read_parquet(files[-1]).sort_values("dollar_volume", ascending=False).drop_duplicates("cik")
    return u[["cik", "symbol", "name"]].assign(cik=lambda x: x["cik"].astype(int))


def load(out, day, symbols, since):
    """Prices for `symbols` ({symbol: cik}) from `since` to `day`, with everything scoring and the replay read.
    Call inside lock.live(day)."""
    m = out / "market"
    t = ds.dataset(m / "prices", partitioning="hive").to_table(
        columns=["symbol", "date", "open", "close", "dividend", "split"],
        filter=(pc.field("source") == "yfinance") & (pc.field("date") >= since) & (pc.field("date") <= day)
        & pc.field("symbol").isin(sorted(symbols))).to_pandas()
    lock.check(t["date"])
    t["cik"] = t["symbol"].map(symbols)
    p, wide = bt.returns(t.sort_values(["cik", "date"]).drop_duplicates(["cik", "date"]).reset_index(drop=True))
    blocked = set()
    for f in sorted((m / "quality" / "daily").glob("*.parquet")):
        q = pd.read_parquet(f)
        for r in q[(q["severity"] == "quarantine") & q["symbol"].isin(symbols)].itertuples():
            blocked |= {(symbols[r.symbol], x) for x in pd.date_range(r.start, r.end).strftime("%Y-%m-%d")}
    last = p.groupby("cik")["date"].max()
    ended = last[last < sg.shift(day, -7)]
    k8 = pd.read_parquet(m / "filings_8k.parquet", columns=["cik", "filed", "items"])
    k8 = k8[k8["items"].fillna("").str.contains("1.03", regex=False) & k8["cik"].isin(ended.index)]
    failed = {c for c, L in ended.items() if ((k8["cik"] == c) & (k8["filed"] >= sg.shift(L, -365))
                                             & (k8["filed"] <= sg.shift(L, 30))).any()}
    co = pd.read_parquet(m / "companies.parquet").set_index("cik")
    facts = ds.dataset(m / "fundamentals" / "facts.parquet").to_table(
        columns=["cik", "tag", "start", "end", "value", "form", "filed", "accn"],
        filter=pc.field("tag").isin(sg.TAGS) & (pc.field("filed") <= day)
        & pc.field("cik").isin(sorted(set(symbols.values())))).to_pandas().sort_values(["end", "filed"])
    lock.check(facts["filed"])
    return dict(p=p, close=wide["close"], tr=wide["tr"], tro=wide["tro"], blocked=blocked, ended=ended,
                failed=failed, sector=co["sic"].map(gics11), facts=facts)


def score(d, day, ciks, sp500):
    """One day's scores for `ciks`: the seven ranks, n, n60, score (FROZEN's choices, re-ranked), sector, vol."""
    ciks = [c for c in ciks if c in d["close"].columns and pd.notna(d["close"].at[day, c])]
    eq, splits, multi = sg.eps_quarters(d["facts"]), sg.splits_table(d["p"]), sg.multi_class(d["facts"])
    raw = sg.raw(day, ciks, d["close"], d["tr"], d["facts"], eq, splits, multi)
    raw = raw.assign(date=day, sector=d["sector"].reindex(raw.index).values).rename_axis("cik").reset_index()
    s = bt.scored(raw, bt.FROZEN["choice"], bt.FROZEN["rule"])
    return s.assign(sp500=s["cik"].isin(sp500))


def verdict_rows(t, tr, sc, symbols):
    """The verdict log rows for one month-end's decisions (trace entry tr) with that day's scores sc."""
    s = sc.set_index("cik")
    ranks = lambda c: json.dumps({k: (None if pd.isna(s.at[c, k]) else round(float(s.at[c, k]), 1))
                                  for k in sg.SIGNALS}) if c in s.index else None
    sym = {c: n for n, c in symbols.items()}
    base = {"book": "paper", "design": "FROZEN (docs/M3.md)", "market_mood": "bad" if tr["mood_bad"] else "ok",
            "news": "not checked (no news source yet; the frozen design had none)", "timing": "not used (frozen)"}
    rows = []
    for c, size in tr["buys"].items():
        rows.append(("Buy", c, base | {"score_bar": tr["bar"], "signals_above_60": int(s.at[c, "n60"]),
                                       "room": "yes", "size_pct": round(100 * size, 2),
                                       "slice": "S&P 500" if s.at[c, "sp500"] else "outside the S&P 500"}))
    for c, why in tr["sells"].items():
        rows.append(("Sell", c, base | {"reason": why, "weight_pct": round(100 * tr["weights"].get(c, 0), 2)}))
    for c, cap in tr["trims"].items():
        rows.append(("Trim", c, base | {"to_pct": round(100 * cap, 2),
                                        "weight_pct": round(100 * tr["weights"].get(c, 0), 2)}))
    for c in tr["keep"]:
        if c not in tr["trims"]:
            rows.append(("Hold", c, base | {"weight_pct": round(100 * tr["weights"].get(c, 0), 2)}))
    return [dict(date=t, cik=int(c), symbol=sym.get(c), verdict=v,
                 score=None if c not in s.index or pd.isna(s.at[c, "score"]) else round(float(s.at[c, "score"]), 2),
                 signal_ranks=ranks(c), gates=json.dumps(g, sort_keys=True)) for v, c, g in rows]


def check_replay(con, trace):
    """Earlier months' recorded paper Buys and Sells must be exactly what the replay decides."""
    logged = {}
    for t, c, v, g in con.execute("SELECT date, cik, verdict, gates FROM verdicts WHERE verdict IN ('Buy', 'Sell')"):
        if json.loads(g or "{}").get("book") == "paper":
            logged.setdefault(t, {"Buy": set(), "Sell": set()})[v].add(c)
    for t, got in logged.items():
        want = {"Buy": set(trace.get(t, {}).get("buys", {})), "Sell": set(trace.get(t, {}).get("sells", {}))}
        if got != want:
            raise SystemExit(f"paper replay doesn't match the verdict log for {t}: logged {got}, replay {want}; "
                             "no verdicts written (data revised under the snapshots?)")


def paper_stats(nav, bench, trace):
    """The paper book against SPY since its start (nav starts at 1.0 on the first month-end's close)."""
    spy = bt.bench_nav(bench, "SPY", list(nav.index))
    last = trace[max(trace)] if trace else {"keep": [], "buys": {}}
    return {"since": nav.index[0], "value": round(float(nav.iloc[-1]), 6),
            "return_pct": round(100 * (nav.iloc[-1] / nav.iloc[0] - 1), 2),
            "spy_return_pct": round(100 * (spy.iloc[-1] / spy.iloc[0] - 1), 2),
            "drawdown_pct": round(100 * (nav.iloc[-1] / nav.max() - 1), 2), "peak_day": nav.idxmax(),
            "day_change_pct": round(100 * (nav.iloc[-1] / nav.iloc[-2] - 1), 2) if len(nav) > 1 else 0.0,
            "positions": len(last["keep"]) + len(last["buys"])}


def scoreboard(d, final, sp500):
    """Since the start: one-way turnover and trading costs as % of the book, and each slice's return (S&P 500
    members, everything else), compounded daily from the positions held at the previous close."""
    vals = final.get("vals", {})
    days = sorted(vals)
    out = {"turnover_pct": round(100 * final.get("traded", 0.0) / 2, 2),
           "costs_pct": round(100 * final.get("traded", 0.0) * bt.FROZEN["cost"], 2), "slices": {}}
    for name, inside in (("sp500", True), ("other", False)):
        grow, held_ever = 1.0, False
        # ponytail: a stock bought or sold at an open counts close to close that day; exact would need intraday fills
        for a, b in zip(days, days[1:]):
            held = {c: v for c, v in vals[a].items() if (c in sp500) == inside}
            tot = sum(held.values())
            if tot > 0:
                held_ever = True
                r = d["tr"].loc[b, list(held)].fillna(0.0)
                grow *= 1 + float(sum(held[c] * r[c] for c in held)) / tot
        now = sum(1 for c in final.get("weights", {}) if (c in sp500) == inside)
        out["slices"][name] = {"positions": now, "return_pct": round(100 * (grow - 1), 2) if held_ever else None}
    return out


def panel_record(con, d, bench, day):
    """The panel's record against the math, by the rule set on 2026-10-29 before any results (docs/M7.md).

    A disagreement is a panel view and the math verdict it was shown against making opposite calls on the stock
    against the S&P 500 (agents.schema.direction). It settles 3 months after the view: the side whose call matched
    the stock's total return against SPY's was right. One per stock at a time: views of a stock while its
    disagreement is still open don't count again, so weekly repeats can't inflate the count. The panel earns a vote
    with at least 30 settled and a one-sided binomial p under 0.05."""
    tri = (1 + d["tr"].fillna(0)).cumprod()  # a stock whose prices stop stays at its last close (a cash deal)
    spy = bt.bench_nav(bench, "SPY", list(tri.index))
    open_until, n, right, wrong, unpriced = {}, 0, 0, 0, 0
    for t, c, v, extra in con.execute("SELECT date, cik, verdict, claims FROM panel_opinions ORDER BY date, id"):
        p, m = direction(v), direction(json.loads(extra or "{}").get("math"))
        if p is None or m is None or p == m or t < open_until.get(c, ""):
            continue
        end = (pd.Timestamp(t) + pd.DateOffset(months=SETTLE_MONTHS)).strftime("%Y-%m-%d")
        open_until[c] = end
        n += 1
        if end > day:
            continue
        if c not in tri.columns:
            unpriced += 1
            continue
        stock = tri[c].loc[:end].iloc[-1] / tri[c].loc[:t].iloc[-1]
        beat = stock > spy.loc[:end].iloc[-1] / spy.loc[:t].iloc[-1]
        right, wrong = (right + 1, wrong) if (1 if beat else -1) == p else (right, wrong + 1)
    settled = right + wrong
    pval = sum(math.comb(settled, k) for k in range(right, settled + 1)) / 2 ** settled if settled else 1.0
    return {"disagreements": n, "panel_right": right, "math_right": wrong, "unpriced": unpriced,
            "p_value": round(pval, 4), "vote_earned": settled >= VOTE_MIN and pval < VOTE_P}


def mood(bench, day):
    """The market filter's view on `day`: SPY against its 200-day average, and VIX against FROZEN's level."""
    spy = bench["SPY"].set_index("date")["close"].loc[:day]
    ma = spy.rolling(200).mean()
    vix = bench["vix"].set_index("date")["vix"].loc[:day]
    below, high = bool(spy.iloc[-1] < ma.iloc[-1]), bool(vix.iloc[-1] > bt.FROZEN["vix"])
    note = (f"S&P 500 {'below' if below else 'above'} its 200-day average, VIX {vix.iloc[-1]:.1f} "
            f"({'high' if high else 'normal'}).")
    return {"bad_mood": below and high, "note": note + (" Buy bar raised, new positions half size." if below and high
                                                        else "")}


def paper_book(d, nav, final, bench, con, day, symbols):
    """The paper book for the site: value history against SPY, and each position's weight, return since its fill
    at the open after its Buy, today's move and recent closes."""
    tri = (1 + d["tr"].fillna(0)).cumprod()
    tri_open = tri.shift() * (1 + d["tro"])
    buys = {}
    for t, c, g, s in con.execute("SELECT date, cik, gates, score FROM verdicts WHERE verdict = 'Buy' ORDER BY id"):
        g = json.loads(g or "{}")
        if g.get("book") == "paper":
            buys[c] = {"date": t, "size_pct": g.get("size_pct"), "score": s}
    sym = {c: n for n, c in symbols.items()}
    spy, rsp = (bt.bench_nav(bench, k, list(nav.index)) for k in ("SPY", "RSP"))
    positions = []
    for c, w in sorted(final.get("weights", {}).items(), key=lambda x: -x[1]):
        b = buys.get(c, {})
        after = [x for x in tri.index if b and x > b["date"]]
        entry = tri_open.at[after[0], c] if after and pd.notna(tri_open.at[after[0], c]) else None
        closes = d["close"][c].loc[:day].dropna().tail(130)
        positions.append({"cik": int(c), "symbol": sym.get(c), "weight_pct": round(100 * w, 2),
                          "bought": b.get("date"), "target_pct": b.get("size_pct"), "buy_score": b.get("score"),
                          "return_pct": round(100 * (tri.at[day, c] / entry - 1), 2) if entry else 0.0,
                          "day_change_pct": round(100 * float(d["tr"].at[day, c]), 2)
                          if pd.notna(d["tr"].at[day, c]) else 0.0,
                          "close": float(closes.iloc[-1]) if len(closes) else None,
                          "prices": [[t, round(float(v), 4)] for t, v in closes.items()]})
    return {"since": nav.index[0], "cash_pct": round(100 * final.get("cash", 1.0), 2),
            "nav": [[t, round(float(v), 6)] for t, v in nav.items()],
            "spy": [[t, round(float(v), 6)] for t, v in spy.items()],
            "rsp": [[t, round(float(v), 6)] for t, v in rsp.items()], "positions": positions}


def run(out, con, now, bench=None):
    """The nightly step. Returns a log dict."""
    m = out / "market"
    day = ds.dataset(m / "prices", partitioning="hive").to_table(
        columns=["date"], filter=pc.field("source") == "yfinance").column("date").to_pandas().max()
    log = {"day": day, "scored": 0, "month_end": month_end(day), "verdicts": 0, "notes": []}
    with lock.live(day, today=now.strftime("%Y-%m-%d")):
        u = latest_universe(m, day)
        snaps = {p.stem: pd.read_parquet(p) for p in sorted((m / "scores" / "month_end").glob("*.parquet"))}
        symbols = dict(zip(u["symbol"], u["cik"]))
        for s in snaps.values():  # stocks scored at earlier month-ends stay priced for the replay
            symbols |= {k: c for k, c in zip(s["symbol"], s["cik"]) if k not in symbols}
        for k, c in con.execute("SELECT DISTINCT symbol, cik FROM panel_opinions"):  # and the panel's, to settle
            if k and c is not None and k not in symbols:
                symbols[k] = c
        start = min([sg.shift(day, -HISTORY_DAYS)] + [sg.shift(t, -HISTORY_DAYS) for t in snaps])
        d = load(out, day, symbols, start)
        segs = pd.read_parquet(m / "ticker_map.parquet")
        sp500 = set(members(segs, day)["cik"])
        sc = score(d, day, list(u["cik"]), sp500)
        sc = sc.assign(symbol=sc["cik"].map(dict(zip(u["cik"], u["symbol"]))),
                       name=sc["cik"].map(dict(zip(u["cik"], u["name"]))))
        (m / "scores").mkdir(parents=True, exist_ok=True)
        sc.to_parquet(m / "scores" / "latest.parquet", index=False)
        log["scored"] = int(sc["score"].notna().sum())
        decide = month_end(day)
        if decide and not current(day, now):
            log["notes"].append(f"{day} is a month-end but its data arrived late: no decisions this month")
            raise SystemExit(log["notes"][-1])
        if decide and day not in snaps:
            (m / "scores" / "month_end").mkdir(parents=True, exist_ok=True)
            sc.to_parquet(m / "scores" / "month_end" / f"{day}.parquet", index=False)
            snaps[day] = sc
        if bench is None:
            bt.bench(out)
            bench = {k: pd.read_parquet(out / "bench" / f"{k}.parquet") for k in ["SPY", "RSP"]}
            bench["vix"] = pd.read_parquet(out / "bench" / "vix.parquet")
        state = {"day": day, "market": mood(bench, day), "started": bool(snaps)}
        (m / "paper").mkdir(parents=True, exist_ok=True)
        if not snaps:
            (m / "paper" / "state.json").write_text(json.dumps(state, indent=1))
            return log  # the paper book starts at the first month-end
        allsc = pd.concat(snaps.values(), ignore_index=True)
        trace, final = {}, {}
        nav, _ = bt.simulate(d, allsc, {k: bench[k] for k in ["SPY", "RSP"]}, bench["vix"],
                             bt.FROZEN | {"start": min(snaps)}, trace, final)
        check_replay(con, trace)  # every night, so revised data shows up the day it happens
        log["paper"] = paper_stats(nav, bench, trace)
        state |= paper_book(d, nav, final, bench, con, day, symbols)
        state["scoreboard"] = scoreboard(d, final, sp500)
        state["panel_record"] = panel_record(con, d, bench, day)
        (m / "paper" / "state.json").write_text(json.dumps(state, indent=1, default=float))
        if not decide:
            return log
        if con.execute("SELECT 1 FROM verdicts WHERE date = ? LIMIT 1", (day,)).fetchone():
            log["notes"].append(f"verdicts for {day} already recorded (a rerun)")
            return log
        if day not in trace:
            raise SystemExit(f"the replay made no decision on {day}; no verdicts written")
        rows = verdict_rows(day, trace[day], sc, symbols)
        for r in rows:
            add_verdict(con, **r)
        log["verdicts"] = len(rows)
        log["decisions"] = {v: sum(r["verdict"] == v for r in rows) for v in ("Buy", "Sell", "Trim", "Hold")}
    return log


def main(out_dir):
    out = Path(out_dir)
    con = connect(out / "db" / "app.sqlite")
    now = datetime.now(timezone.utc)
    log = run(out, con, now)
    print(json.dumps(log, indent=1), flush=True)
    print(json.dumps(daily.run(con, log, now, out / "market" / "prices")), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
