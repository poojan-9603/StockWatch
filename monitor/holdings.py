"""Current holdings per lot, derived (never stored) from the trades and corrections tables.

A correction (table_name 'trades') is a row whose payload JSON replaces fields of the trade it points at, applied in
id order. Sells close lots FIFO unless they name a lot_id. Splits and dividends come in as rows (symbol, date, kind
split|dividend, value) and apply on their date before that day's trades: a split multiplies shares and divides price,
a dividend pays value per share to the lots held going into that date (recorded per lot, not added to cash).
The long-term and wash-sale flags are reminders, not tax advice.
"""
import json
from datetime import date, timedelta

from pipeline.db import TRADE_FIELDS

EPS = 1e-6
WASH_DAYS = 30


def trades(con):
    """Every trade with its corrections applied, as dicts in id order."""
    cur = con.execute("SELECT * FROM trades ORDER BY id")
    names = [d[0] for d in cur.description]
    rows = {r[0]: dict(zip(names, r)) for r in cur}
    for cid, rid, payload in con.execute(
            "SELECT id, row_id, payload FROM corrections WHERE table_name = 'trades' ORDER BY id"):
        fix = json.loads(payload)
        if rid not in rows or set(fix) - TRADE_FIELDS:
            raise ValueError(f"correction {cid} to trade {rid}: {sorted(set(fix) - TRADE_FIELDS) or 'no such trade'}")
        rows[rid].update(fix)
    return list(rows.values())


def clean_ratio(v):
    """A real split is a small whole-number ratio (2, 3/2, 1/10). Yahoo's spin-off 'splits' (3M 1.196) are not."""
    return v > 0 and any(abs(x * q - round(x * q)) < 1e-4 for x in (v, 1 / v) for q in range(1, 11))


def long_term_on(buy_date):
    """First day a lot counts as held over a year: the day after its anniversary (Feb 29 buys use Feb 28)."""
    d = date.fromisoformat(buy_date)
    return (date(d.year + 1, 2, 28) if (d.month, d.day) == (2, 29) else d.replace(year=d.year + 1)) + timedelta(1)


def check(t):
    k = t["kind"]
    ok = (t.get("symbol") and (t.get("shares") or 0) > 0 and (t.get("price") or 0) > 0 if k in ("buy", "sell")
          else (t.get("amount") or 0) > 0 if k in ("deposit", "withdrawal") else False)
    if not ok or (t.get("fees") or 0) < 0:
        raise ValueError(f"trade {t['id']} can't be used: {t}")


def sell(lots, t):
    sym, want, lot_id = t["symbol"], t["shares"], t.get("lot_id")
    pool = [lot for lot in lots if lot["symbol"] == sym and (not lot_id or lot["lot_id"] == lot_id)]
    have = sum(lot["shares"] for lot in pool)
    if have < want - EPS:
        raise ValueError(f"trade {t['id']} sells {want} {sym}{f' from lot {lot_id}' if lot_id else ''}, only {have} held")
    left, parts = want, []
    for lot in pool:  # lots are in buy order: FIFO
        if left <= EPS:
            break
        n = min(lot["shares"], left)
        basis = lot["cost_basis"] * n / lot["shares"]
        lot["shares"] -= n
        lot["cost_basis"] -= basis
        left -= n
        parts.append({"lot_id": lot["lot_id"], "shares": n, "basis": basis,
                      "long_term": date.fromisoformat(t["date"]) >= long_term_on(lot["buy_date"])})
    lots[:] = [lot for lot in lots if lot["shares"] > EPS]
    proceeds = want * t["price"] - (t.get("fees") or 0)
    basis = sum(p["basis"] for p in parts)
    return {"trade_id": t["id"], "symbol": sym, "date": t["date"], "shares": want, "proceeds": proceeds,
            "basis": basis, "gain": proceeds - basis, "lots": parts}


def holdings(trades, actions, as_of):
    """trades: from trades(con). actions: dicts with symbol, date, kind (split|dividend), value. Uses everything dated
    up to as_of (YYYY-MM-DD). Returns {lots: open lots, sales, cash, dividends}."""
    events = [(a["date"], 0, i, a) for i, a in enumerate(actions) if a["date"] <= as_of]
    events += [(t["date"], 1, t["id"], t) for t in trades if t["date"] <= as_of]
    lots, buys, sales, cash, dividends = [], [], [], 0.0, 0.0
    for d, is_trade, _, e in sorted(events, key=lambda x: x[:3]):
        if not is_trade:
            held = [lot for lot in lots if lot["symbol"] == e["symbol"]]
            if e["kind"] == "split":
                if not clean_ratio(e["value"]):
                    raise ValueError(f"{e['symbol']} split {e['value']} on {d} looks like a spin-off: confirm it first")
                for lot in held:
                    lot["shares"] *= e["value"]
                    lot["price"] /= e["value"]
            elif e["kind"] == "dividend":
                for lot in held:
                    lot["dividends"] += lot["shares"] * e["value"]
                    dividends += lot["shares"] * e["value"]
            else:
                raise ValueError(f"unknown corporate action {e}")
            continue
        check(e)
        fees = e.get("fees") or 0
        if e["kind"] == "deposit":
            cash += e["amount"] - fees
        elif e["kind"] == "withdrawal":
            cash -= e["amount"] + fees
        elif e["kind"] == "buy":
            lot_id = e.get("lot_id") or str(e["id"])
            if any(lot["lot_id"] == lot_id for lot in lots):
                raise ValueError(f"trade {e['id']} reuses open lot id {lot_id}")
            cash -= e["shares"] * e["price"] + fees
            lots.append({"lot_id": lot_id, "trade_id": e["id"], "symbol": e["symbol"], "cik": e.get("cik"),
                         "buy_date": d, "shares": e["shares"], "price": e["price"],
                         "cost_basis": e["shares"] * e["price"] + fees, "dividends": 0.0})
            buys.append((lot_id, e["symbol"], date.fromisoformat(d)))
        else:
            cash += e["shares"] * e["price"] - fees
            sales.append(sell(lots, e))
    for s in sales:
        sold, on = {p["lot_id"] for p in s["lots"]}, date.fromisoformat(s["date"])
        s["replacements"] = [b[0] for b in buys if s["gain"] < 0 and b[1] == s["symbol"] and b[0] not in sold
                             and abs((b[2] - on).days) <= WASH_DAYS]
        s["wash_sale"] = bool(s["replacements"])
    flagged = {lot_id for s in sales for lot_id in s["replacements"]}
    for lot in lots:
        lot["long_term_on"] = long_term_on(lot["buy_date"]).isoformat()
        lot["long_term"] = date.fromisoformat(as_of) >= long_term_on(lot["buy_date"])
        lot["wash_sale"] = lot["lot_id"] in flagged
    return {"lots": lots, "sales": sales, "cash": cash, "dividends": dividends}
