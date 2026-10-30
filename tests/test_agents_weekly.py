"""The panel's weekly pass: paper holdings first (as owned), then the shortlist that passes the buy rule; numbers from
our fundamentals as of the day; views recorded to panel_opinions and the site file."""
import json

import pandas as pd

from agents import docs, news, panel, weekly
from pipeline.db import add_verdict, connect
from test_agents_panel import client


def setup(tmp_path, monkeypatch):
    out, m = tmp_path / "out", tmp_path / "out" / "market"
    (m / "scores").mkdir(parents=True)
    (m / "fundamentals").mkdir()
    pd.DataFrame({"cik": [1, 2, 3, 4], "symbol": ["A", "B", "C", "D"], "date": "2026-10-30",
                  "score": [95.0, 90.0, 85.0, 50.0], "n60": [6, 4, 5, 6]}).to_parquet(m / "scores" / "latest.parquet")
    pd.DataFrame([{"cik": c, "tag": "Revenues", "start": "2025-01-01", "end": "2025-12-31", "value": 1e9 * c,
                   "form": "10-K", "filed": "2026-02-10", "accn": f"a{c}"} for c in (1, 3, 9)]
                 ).to_parquet(m / "fundamentals" / "facts.parquet")
    pd.DataFrame({"cik": [1, 9, 9], "accession": ["x1", "x9", "old"], "form": "8-K",
                  "filed": ["2026-10-01", "2026-10-15", "2025-01-01"], "items": "2.02"}).to_parquet(
        m / "filings_8k.parquet")
    seen = []

    def fake_docs(rows):
        seen.append([r["accession"] for r in rows])
        return [docs.document("Sales grew 12 percent this year.", f"d-{r['accession']}", str(r["filed"]), "SEC 8-K",
                              "filing", html=False) for r in rows]

    monkeypatch.setattr(panel, "filing_docs", fake_docs)
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    monkeypatch.setattr(news, "news_docs", lambda sym, day, key: seen.append(("news", sym, day, key)) or [])
    (out / "db").mkdir()
    con = connect(out / "db" / "app.sqlite")
    add_verdict(con, date="2026-09-30", cik=9, symbol="Z", verdict="Buy", gates=json.dumps({"book": "paper"}))
    add_verdict(con, date="2026-10-30", cik=9, symbol="Z", verdict="Hold", gates=json.dumps({"book": "paper"}))
    add_verdict(con, date="2026-10-30", cik=7, symbol="Q", verdict="Sell", gates=json.dumps({"book": "paper"}))
    return out, con, seen


def test_holdings_then_shortlist_numbers_and_records(tmp_path, monkeypatch):
    out, con, seen = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(weekly.lock, "live", lambda day: __import__("contextlib").nullcontext())
    monkeypatch.setattr(weekly.lock, "check", lambda dates: None)
    weekly.main(out, client=client())
    site = json.loads((out / "market" / "panel" / "latest.json").read_text())
    assert [(s["symbol"], s["owned"]) for s in site["stocks"]] == [("Z", True), ("A", False), ("C", False)]
    assert [x for x in seen if isinstance(x, list)] == [["x9"], ["x1"], []]  # only 8-Ks from the last 120 days
    assert [x for x in seen if isinstance(x, tuple)] == [("news", s, "2026-10-30", "k") for s in ("Z", "A", "C")]
    assert site["stocks"][1]["panel"]["agrees"] is True  # the fake judge says Buy; the math said Buy
    rows = con.execute("SELECT symbol, verdict, model FROM panel_opinions").fetchall()
    assert [r[0] for r in rows] == ["Z", "A", "C"] and "claude-opus-5-5 judge" in rows[0][2]
    assert rows[2][1] == "Not enough information"  # no documents, no view
    maths = [json.loads(c)["math"] for (c,) in con.execute("SELECT claims FROM panel_opinions")]
    assert maths[1:] == ["Buy", "Buy"] and maths[0] in ("Buy", "Hold", "Trim")  # what each view was compared with


def test_numbers_are_as_of_the_day_and_never_the_score(tmp_path):
    f = pd.DataFrame([{"cik": 1, "tag": "Revenues", "start": "2025-01-01", "end": "2025-12-31", "value": 5e9,
                       "form": "10-K", "filed": "2026-02-10", "accn": "a"},
                      {"cik": 1, "tag": "NetIncomeLoss", "start": "2025-01-01", "end": "2025-12-31", "value": 4e8,
                       "form": "10-K", "filed": "2026-11-20", "accn": "b"}])
    with weekly.lock.live("2026-10-30", today="2026-10-31"):  # live data only inside the live lock
        n = weekly.numbers(f, 1, "2026-10-30")
    assert n == {"revenue (latest reported, USD)": 5e9}  # net income wasn't filed yet
