"""The panel's weekly pass (ARCHITECTURE "Jobs": weekend): the agent panel on the paper holdings and the shortlist.

  holdings   the paper book's positions from its latest month-end verdicts (Buy, Hold, Trim), as owned stocks
  shortlist  the SHORTLIST best-scored stocks that pass FROZEN's buy rule today, not already held
  documents  each stock's 8-Ks from the last DOC_DAYS (up to MAX_8KS), with their EX-99 press releases. News waits on
             a Finnhub key, so the news reader has nothing to read for now.
  numbers    the latest annual figures from our fundamentals (pit.as_of on today), never the math score
Views go to the panel_opinions table (the pipeline is the one writer) and market/panel/latest.json for the site.
Refuses to start if MAX_STOCKS * the worst cost per stock could pass WEEK_BUDGET.

Usage: python -m agents.weekly OUT_DIR
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds

from agents import panel
from pipeline.db import connect, now as stamp
from quant import backtest as bt, lock
from quant.measure import COST, REVENUE
from quant.pit import as_of, first_available

SHORTLIST, MAX_STOCKS = 10, 30
DOC_DAYS, MAX_8KS = 120, 3
WEEK_BUDGET, WORST_PER_STOCK = 3.00, 0.09  # dollars; the smoke runs cost about $0.045 a stock
FIGURES = {"revenue": REVENUE, "cost of revenue": COST, "net income": ["NetIncomeLoss"],
           "operating income": ["OperatingIncomeLoss"], "total assets": ["Assets"],
           "stockholders' equity": ["StockholdersEquity"]}


def paper_holdings(con):
    """{cik: (symbol, paper verdict)} at the latest paper month-end; empty before the first."""
    rows = [(d, c, s, v) for d, c, s, v, g in con.execute("SELECT date, cik, symbol, verdict, gates FROM verdicts")
            if json.loads(g or "{}").get("book") == "paper"]
    if not rows:
        return {}
    last = max(r[0] for r in rows)
    return {c: (s, v) for d, c, s, v in rows if d == last and v in ("Buy", "Hold", "Trim")}


def shortlist(scores, held, n=SHORTLIST):
    ok = scores[(scores["score"] >= bt.FROZEN["buy"]) & (scores["n60"] >= bt.FROZEN["n60"])
                & ~scores["cik"].isin(list(held))]
    return ok.nlargest(n, "score")


def numbers(facts, cik, day):
    """The latest annual figures as known on `day`, in US dollars, labeled for the readers."""
    f = facts[facts["cik"] == cik]
    found = as_of(f, day, sum(FIGURES.values(), []), annual=None)
    out = {}
    for label, tags in FIGURES.items():
        v = first_available(found, tags).get(cik)
        if v is not None and pd.notna(v):
            out[f"{label} (latest reported, USD)"] = float(v)
    return out


def stocks(out, con, day, k8):
    """The panel's input for this week: owned paper holdings first, then the shortlist."""
    m = out / "market"
    scores = pd.read_parquet(m / "scores" / "latest.parquet")
    held = paper_holdings(con)
    picks = shortlist(scores, held)
    want = [(c, s, True, v) for c, (s, v) in held.items()]
    want += [(int(r.cik), r.symbol, False, "Buy") for r in picks.itertuples()]
    want = want[:MAX_STOCKS]
    ciks = sorted({c for c, *_ in want})
    facts = ds.dataset(m / "fundamentals" / "facts.parquet").to_table(
        columns=["cik", "tag", "start", "end", "value", "form", "filed", "accn"],
        filter=pc.field("cik").isin(ciks) & (pc.field("filed") <= day)).to_pandas().sort_values(["end", "filed"])
    lock.check(facts["filed"])
    since = (date.fromisoformat(day) - timedelta(days=DOC_DAYS)).isoformat()
    out_stocks = []
    for c, sym, owned, math_verdict in want:
        rows = k8[(k8["cik"] == c) & (k8["form"] == "8-K") & (k8["filed"] >= since)].sort_values("filed").tail(MAX_8KS)
        out_stocks.append({"cik": c, "symbol": sym, "date": day, "owned": owned, "math": math_verdict,
                           "docs": panel.filing_docs(rows.to_dict("records")), "numbers": numbers(facts, c, day)})
    return out_stocks


def record(con, views, created_at, todo):
    for v, s in zip(views, todo):
        r = panel.row(v, created_at, s["math"])
        con.execute(f"INSERT INTO panel_opinions ({','.join(r)}) VALUES ({','.join('?' * len(r))})", list(r.values()))
    con.commit()


def main(out_dir, client=None):
    if MAX_STOCKS * WORST_PER_STOCK > WEEK_BUDGET:
        raise SystemExit(f"up to ${MAX_STOCKS * WORST_PER_STOCK:.2f} would pass the ${WEEK_BUDGET} weekly budget")
    out = Path(out_dir)
    scores = pd.read_parquet(out / "market" / "scores" / "latest.parquet", columns=["date"])
    day = scores["date"].max()
    con = connect(out / "db" / "app.sqlite")
    with lock.live(day):
        k8 = pd.read_parquet(out / "market" / "filings_8k.parquet")
        todo = stocks(out, con, day, k8)
    if client is None:
        import anthropic  # only here, so tests and the pipeline don't need the SDK
        client = anthropic.Anthropic(max_retries=6)
    views = panel.run(client, todo)
    record(con, views, stamp(), todo)
    site = [{"cik": s["cik"], "symbol": s["symbol"], "owned": s["owned"],
             "panel": panel.to_contract(v, s["math"], s["docs"])} for s, v in zip(todo, views)]
    (out / "market" / "panel").mkdir(parents=True, exist_ok=True)
    (out / "market" / "panel" / "latest.json").write_text(json.dumps({"date": day, "stocks": site}, indent=1))
    failed = [v["symbol"] for v in views if v["verdict"] is None]
    print(json.dumps({"day": day, "stocks": len(todo), "owned": sum(s["owned"] for s in todo),
                      "cost": round(sum(v["cost"] for v in views), 4), "failed": failed}, indent=1))
    if failed:
        raise SystemExit(f"panel judge failed for {failed}: recorded, shown as no panel")


if __name__ == "__main__":
    main(sys.argv[1])
