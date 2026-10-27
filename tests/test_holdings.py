import json

import pytest

from monitor.holdings import clean_ratio, holdings, long_term_on, trades
from pipeline.db import connect, ingest_inbox, now


def db(tmp_path, *rows):
    tmp_path.mkdir(parents=True, exist_ok=True)
    con = connect(tmp_path / "app.sqlite")
    ingest_inbox(con, {f"{i:03}.json": r for i, r in enumerate(rows)})
    return con


def buy(d, sym, n, p, **kw):
    return {"kind": "buy", "date": d, "symbol": sym, "shares": n, "price": p, **kw}


def sell(d, sym, n, p, **kw):
    return {"kind": "sell", "date": d, "symbol": sym, "shares": n, "price": p, **kw}


def test_fifo_named_lots_and_cash(tmp_path):
    con = db(tmp_path, {"kind": "deposit", "date": "2026-01-02", "amount": 10000},
             buy("2026-01-05", "AAPL", 10, 100, fees=1), buy("2026-02-05", "AAPL", 10, 120, lot_id="b"),
             buy("2026-02-06", "MSFT", 5, 400), sell("2026-03-02", "AAPL", 15, 130, fees=1),
             {"kind": "withdrawal", "date": "2026-03-03", "amount": 500})
    h = holdings(trades(con), [], "2026-06-01")
    assert [(lot["lot_id"], lot["symbol"], lot["shares"]) for lot in h["lots"]] == [("b", "AAPL", 5), ("4", "MSFT", 5)]
    assert h["lots"][0]["cost_basis"] == pytest.approx(600)
    s = h["sales"][0]
    assert [(p["lot_id"], p["shares"]) for p in s["lots"]] == [("2", 10), ("b", 5)]
    assert s["basis"] == pytest.approx(1001 + 600) and s["gain"] == pytest.approx(15 * 130 - 1 - 1601)
    assert h["cash"] == pytest.approx(10000 - 1001 - 1200 - 2000 + 1949 - 500)

    con = db(tmp_path / "named", buy("2026-01-05", "AAPL", 10, 100), buy("2026-02-05", "AAPL", 10, 120, lot_id="b"),
             sell("2026-03-02", "AAPL", 4, 130, lot_id="b"))
    assert [(lot["lot_id"], lot["shares"]) for lot in holdings(trades(con), [], "2026-06-01")["lots"]] == [("1", 10), ("b", 6)]


def test_trades_after_as_of_are_ignored_and_oversells_raise(tmp_path):
    con = db(tmp_path, buy("2026-01-05", "AAPL", 10, 100), sell("2026-03-02", "AAPL", 11, 130))
    assert holdings(trades(con), [], "2026-02-01")["lots"][0]["shares"] == 10
    with pytest.raises(ValueError, match="only 10"):
        holdings(trades(con), [], "2026-06-01")
    con = db(tmp_path / "lot", buy("2026-01-05", "AAPL", 10, 100), sell("2026-03-02", "AAPL", 1, 130, lot_id="nope"))
    with pytest.raises(ValueError, match="from lot nope"):
        holdings(trades(con), [], "2026-06-01")


def test_splits_and_dividends(tmp_path):
    con = db(tmp_path, buy("2024-06-03", "NVDA", 10, 1150), buy("2024-06-10", "NVDA", 10, 121),
             sell("2024-06-11", "NVDA", 50, 125))
    actions = [{"symbol": "NVDA", "date": "2024-06-10", "kind": "split", "value": 10},
               {"symbol": "NVDA", "date": "2024-06-11", "kind": "dividend", "value": 0.01},
               {"symbol": "AAPL", "date": "2024-06-10", "kind": "split", "value": 4}]
    h = holdings(trades(con), actions, "2024-12-31")
    # the 06-03 lot becomes 100 shares at 115; the 06-10 buy is already post-split; the 06-11 sale still gets the dividend
    first, second = h["lots"]
    assert (first["shares"], first["price"], first["cost_basis"]) == (pytest.approx(50), pytest.approx(115), pytest.approx(5750))
    assert (second["shares"], second["price"]) == (10, 121)
    assert first["dividends"] == pytest.approx(1.0) and second["dividends"] == pytest.approx(0.1)
    assert h["dividends"] == pytest.approx(1.1)
    assert h["cash"] == pytest.approx(-11500 - 1210 + 6250)  # dividends are recorded, not added to cash


def test_spin_off_ratios_are_refused():
    assert all(clean_ratio(v) for v in [2, 4, 10, 50, 1.5, 1.25, 0.1, 1 / 35])
    assert not any(clean_ratio(v) for v in [1.196, 1.066, 1.04, 1.998, 1.0027])
    with pytest.raises(ValueError, match="spin-off"):
        holdings([{"id": 1, **buy("2024-01-02", "MMM", 1, 100)}],
                 [{"symbol": "MMM", "date": "2024-04-01", "kind": "split", "value": 1.196}], "2024-12-31")


def test_corrections_replace_fields_in_order(tmp_path):
    con = db(tmp_path, buy("2026-01-05", "AAPL", 10, 100))
    for payload in [{"price": 101}, {"price": 102, "shares": 12}]:
        con.execute("INSERT INTO corrections (table_name, row_id, reason, payload, created_at) VALUES (?,?,?,?,?)",
                    ("trades", 1, "typo", json.dumps(payload), now()))
    lot = holdings(trades(con), [], "2026-02-01")["lots"][0]
    assert (lot["shares"], lot["price"]) == (12, 102)
    con.execute("INSERT INTO corrections (table_name, row_id, reason, payload, created_at) VALUES (?,?,?,?,?)",
                ("trades", 1, "bad", json.dumps({"source": "x"}), now()))
    with pytest.raises(ValueError, match="source"):
        trades(con)


def test_long_term_flag_starts_the_day_after_the_anniversary(tmp_path):
    assert long_term_on("2025-01-10").isoformat() == "2026-01-11"
    assert long_term_on("2024-02-29").isoformat() == "2025-03-01"
    con = db(tmp_path, buy("2025-01-10", "AAPL", 1, 100))
    assert not holdings(trades(con), [], "2026-01-10")["lots"][0]["long_term"]
    assert holdings(trades(con), [], "2026-01-11")["lots"][0]["long_term"]


def test_wash_sale_reminder(tmp_path):
    con = db(tmp_path, buy("2026-01-05", "AAPL", 10, 100), sell("2026-03-02", "AAPL", 10, 80),
             buy("2026-04-01", "AAPL", 10, 85), buy("2026-04-02", "AAPL", 10, 86),
             buy("2026-01-30", "MSFT", 10, 400), sell("2026-05-01", "MSFT", 10, 450), buy("2026-05-02", "MSFT", 1, 450))
    h = holdings(trades(con), [], "2026-06-01")
    loss, gain = h["sales"]
    assert loss["gain"] < 0 and loss["wash_sale"] and loss["replacements"] == ["3"]  # 30 days after counts, 31 doesn't
    assert not gain["wash_sale"]
    assert {lot["lot_id"]: lot["wash_sale"] for lot in h["lots"]} == {"3": True, "4": False, "7": False}

    con = db(tmp_path / "before", buy("2026-01-05", "AAPL", 10, 100), buy("2026-02-20", "AAPL", 5, 90),
             sell("2026-03-02", "AAPL", 10, 80))
    h = holdings(trades(con), [], "2026-06-01")  # bought 10 days before the loss sale; the sold lot itself never counts
    assert h["sales"][0]["replacements"] == ["2"] and h["lots"][0]["wash_sale"]
