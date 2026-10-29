"""Live scoring and paper verdicts: the NYSE calendar, month-ends, the live lock, scores every night, decisions only
at a current month-end, write-once snapshots, the hash-chained log, reruns, and a replay that must match the log."""
import json
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from pipeline.db import connect, verify_chain
from quant import live


def test_nyse_holidays_and_month_ends():
    h26 = live.holidays(2026)
    assert {date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 11, 26),
            date(2026, 12, 25), date(2026, 1, 19), date(2026, 2, 16), date(2026, 9, 7)} <= h26
    assert date(2027, 12, 31) not in live.holidays(2028) and live.trading_day(date(2027, 12, 31))
    assert live.month_end("2026-10-30") and not live.month_end("2026-10-29")
    assert live.month_end("2027-05-28")  # May 31, 2027 is Memorial Day
    assert live.month_end("2027-03-31") and not live.month_end("2027-03-26")  # Good Friday 2027


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_a_month_end_decision_must_come_before_the_next_open():
    assert live.current("2026-10-30", utc(2026, 10, 31, 1, 17))
    assert live.current("2026-10-30", utc(2026, 11, 2, 13, 29))  # GitHub hours late is still fine
    assert not live.current("2026-10-30", utc(2026, 11, 2, 13, 30))


def market(tmp_path, end="2026-10-30", n=40, seed=1):
    """A small live market: n stocks with prices, a year of filings, a universe file and an S&P membership."""
    rng = np.random.default_rng(seed)
    out, m = tmp_path / "out", tmp_path / "out" / "market"
    days = pd.bdate_range("2025-07-01", end).strftime("%Y-%m-%d")
    rows, facts = [], []
    for i in range(n):
        cik, sym = 1000 + i, f"S{i:02d}"
        px = 50 * np.exp(np.cumsum(rng.normal(0.0005 * (i % 7), 0.02, len(days))))
        rows += [{"source": "yfinance", "symbol": sym, "date": d, "open": p * 0.999, "close": p, "dividend": 0.0,
                  "split": np.nan, "volume": 1e6} for d, p in zip(days, px)]
        for fy, filed in [(2024, "2025-02-14"), (2025, "2026-02-13")]:
            g = 1 + 0.02 * (i % 5) * (fy - 2024)
            base = {"cik": cik, "form": "10-K", "filed": filed, "accn": f"{cik}-{fy}"}
            facts += [base | {"tag": "NetIncomeLoss", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                              "value": 1e8 * (1 + i % 9) * g},
                      base | {"tag": "Revenues", "start": f"{fy}-01-01", "end": f"{fy}-12-31", "value": 1e9 * g},
                      base | {"tag": "Assets", "start": None, "end": f"{fy}-12-31", "value": 2e9 * g * (1 + i % 4)},
                      base | {"tag": "StockholdersEquity", "start": None, "end": f"{fy}-12-31",
                              "value": 1e9 * (1 + i % 6)},
                      base | {"tag": "EntityCommonStockSharesOutstanding", "start": None, "end": f"{fy}-12-31",
                              "value": 1e8 * (1 + 0.01 * (i % 3) * (fy - 2024))}]
    p = pd.DataFrame(rows)
    for y, g in p.groupby(p["date"].str[:4]):
        (m / "prices" / f"year={y}").mkdir(parents=True, exist_ok=True)
        g.to_parquet(m / "prices" / f"year={y}" / "part.parquet", index=False)
    (m / "fundamentals").mkdir(parents=True)
    pd.DataFrame(facts).to_parquet(m / "fundamentals" / "facts.parquet", index=False)
    (m / "universe").mkdir()
    pd.DataFrame({"cik": [1000 + i for i in range(n)], "symbol": [f"S{i:02d}" for i in range(n)],
                  "name": [f"Co {i}" for i in range(n)], "dollar_volume": 1e7}).to_parquet(
        m / "universe" / "2026-10.parquet")
    pd.DataFrame({"cik": [1000 + i for i in range(n)], "sic": [3570 + 10 * (i % 5) for i in range(n)]}).to_parquet(
        m / "companies.parquet")
    pd.DataFrame({"cik": [1000], "filed": ["2026-01-05"], "items": ["2.02"]}).to_parquet(m / "filings_8k.parquet")
    pd.DataFrame({"ticker": [f"S{i:02d}" for i in range(20)], "cik": [1000 + i for i in range(20)],
                  "start": "2000-01-01", "end": None}).to_parquet(m / "ticker_map.parquet")
    bdays = pd.bdate_range("2011-01-03", end).strftime("%Y-%m-%d")
    bench = {k: pd.DataFrame({"date": bdays, "close": np.linspace(100, 200, len(bdays)), "tr": 0.0003})
             for k in ["SPY", "RSP"]}
    bench["vix"] = pd.DataFrame({"date": bdays, "vix": 15.0})
    return out, bench


def test_month_end_scores_snapshots_and_writes_hash_chained_paper_verdicts(tmp_path):
    out, bench = market(tmp_path)
    con = connect(tmp_path / "app.sqlite")
    log = live.run(out, con, utc(2026, 10, 31, 1, 17), bench=bench)
    assert log["day"] == "2026-10-30" and log["month_end"] and log["scored"] == 40
    assert log["decisions"]["Buy"] > 0 and log["decisions"]["Sell"] == 0
    assert (out / "market" / "scores" / "month_end" / "2026-10-30.parquet").exists()
    rows = con.execute("SELECT verdict, symbol, gates, signal_ranks FROM verdicts").fetchall()
    assert all(v == "Buy" for v, *_ in rows) and verify_chain(con) is None
    g = json.loads(rows[0][2])
    assert g["book"] == "paper" and g["news"].startswith("not checked") and g["size_pct"] >= 2
    assert set(json.loads(rows[0][3])) == {"momentum", "quality", "value", "low_volatility", "earnings_surprise",
                                           "issuance", "asset_growth"}
    again = live.run(out, con, utc(2026, 10, 31, 3, 0), bench=bench)  # a rerun records nothing new
    assert "already recorded" in again["notes"][0] and con.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] \
        == len(rows)


def test_ordinary_days_only_score(tmp_path):
    out, bench = market(tmp_path, end="2026-10-29")
    con = connect(tmp_path / "app.sqlite")
    log = live.run(out, con, utc(2026, 10, 30, 1, 17), bench=bench)
    assert not log["month_end"] and log["scored"] == 40 and log["verdicts"] == 0
    assert (out / "market" / "scores" / "latest.parquet").exists()
    assert not (out / "market" / "scores" / "month_end").exists()


def test_a_late_month_end_gets_no_decisions_and_old_data_is_locked(tmp_path):
    out, bench = market(tmp_path)
    con = connect(tmp_path / "app.sqlite")
    with pytest.raises(SystemExit, match="arrived late"):
        live.run(out, con, utc(2026, 11, 2, 15, 0), bench=bench)
    assert con.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0
    with pytest.raises(PermissionError, match="latest day only"):
        live.run(out, con, utc(2026, 12, 1, 1, 17), bench=bench)


def test_the_replay_must_match_the_log(tmp_path):
    out, bench = market(tmp_path)
    con = connect(tmp_path / "app.sqlite")
    live.run(out, con, utc(2026, 10, 31, 1, 17), bench=bench)
    trace = {"2026-10-30": {"buys": {}, "sells": {}}}  # a replay that bought nothing
    with pytest.raises(SystemExit, match="doesn't match"):
        live.check_replay(con, trace)


def test_paper_book_every_night_and_the_nightly_alerts(tmp_path):
    from monitor import alerts, daily
    out, bench = market(tmp_path)
    con = connect(tmp_path / "app.sqlite")
    live.run(out, con, utc(2026, 10, 31, 1, 17), bench=bench)  # the first month-end
    out2, bench2 = market(tmp_path / "later", end="2026-11-04")
    snap = out / "market" / "scores" / "month_end" / "2026-10-30.parquet"
    (out2 / "market" / "scores" / "month_end").mkdir(parents=True)
    (out2 / "market" / "scores" / "month_end" / snap.name).write_bytes(snap.read_bytes())
    log = live.run(out2, con, utc(2026, 11, 5, 1, 17), bench=bench2)
    p = log["paper"]
    assert p["since"] == "2026-10-30" and p["positions"] > 0 and log["verdicts"] == 0
    state = json.loads((out2 / "market" / "paper" / "state.json").read_text())
    assert state["started"] and state["since"] == "2026-10-30" and state["positions"]
    pos = state["positions"][0]
    assert pos["bought"] == "2026-10-30" and pos["target_pct"] >= 2 and len(pos["prices"]) > 50
    assert "S&P 500" in state["market"]["note"] and state["nav"][0] == ["2026-10-30", 1.0]
    found = daily.alerts_for(log)
    assert found[0]["key"] == "summary:2026-11-04" and "Paper book since 2026-10-30" in found[0]["text"]
    assert daily.run(con, log, utc(2026, 11, 5, 1, 17)) == {"alerts": 1, "sent": {"app": 1}}
    crash = log | {"paper": p | {"drawdown_pct": -22.5}}
    a = daily.alerts_for(crash)
    assert a[1]["tier"] == "urgent" and a[1]["key"].startswith("drawdown:paper:") and "Nothing is sold" in a[1]["text"]
    assert daily.alerts_for({"day": "2026-10-29", "scored": 5})[0]["text"].endswith("starts at the first month-end.")
    assert {h["status"] for h in alerts.history(con)} == {"sent"}
