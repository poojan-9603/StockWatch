"""The site's data files from a synthetic market run through live scoring: every file passes the contract, the paper
book shows as holdings, the month-end buys show as picks and actions, and app alerts show on Today."""
import json
from datetime import datetime, timezone

from monitor import watch
from pipeline import site
from pipeline.db import connect
from quant import live
from test_live import market, utc


def run_book(tmp_path):
    out, bench = market(tmp_path)
    con = connect(tmp_path / "app.sqlite")
    live.run(out, con, utc(2026, 10, 31, 1, 17), bench=bench)
    out2, bench2 = market(tmp_path / "later", end="2026-11-04")
    snap = out / "market" / "scores" / "month_end" / "2026-10-30.parquet"
    (out2 / "market" / "scores" / "month_end").mkdir(parents=True)
    (out2 / "market" / "scores" / "month_end" / snap.name).write_bytes(snap.read_bytes())
    (out2 / "db").mkdir()
    (out2 / "db" / "app.sqlite").write_bytes((tmp_path / "app.sqlite").read_bytes())
    con2 = connect(out2 / "db" / "app.sqlite")
    live.run(out2, con2, utc(2026, 11, 5, 1, 17), bench=bench2)
    watch.send(con2, [watch.test_alert(datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc))],
               datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc), [])
    return out2


def test_every_file_passes_the_contract_and_shows_the_paper_book(tmp_path):
    out = run_book(tmp_path)
    (out / "logs" / "restore").mkdir(parents=True)
    (out / "logs" / "restore" / "latest.json").write_text('{"date": "2026-11-07", "ok": true}')
    (out / "logs" / "actions.json").write_text(json.dumps([
        {"workflowName": "nightly", "event": "schedule", "status": "completed", "conclusion": "failure",
         "createdAt": f"{site.now_ts()[:10]}T00:00:00Z", "url": "u"}]))
    files = site.build(out)
    data = out / "site" / "data"
    assert {p.relative_to(data).as_posix() for p in data.rglob("*.json")} >= {
        "today.json", "picks.json", "holdings.json", "settings.json", "health.json", "reports/weekly.json",
        "reports/monthly.json"}
    h = files["holdings"]
    assert h["since"] == "2026-10-30" and h["holdings"] and h["managed_usd"] == 10000
    assert abs(sum(x["weight_pct"] for x in h["holdings"]) + 100 * h["cash_usd"] / h["value_usd"] - 100) < 0.5
    first = h["holdings"][0]
    assert (data / "holding" / f"{first['cik']}.json").exists()
    assert files[f"holding/{first['cik']}"]["prices"] and files[f"holding/{first['cik']}"]["why_bought"]["score"]
    assert any(p["verdict"] == "Buy" for p in files["picks"]["picks"])
    assert files["today"]["alerts"][0]["title"] == "Test alert"
    assert files["today"]["status"] == "quiet"  # the Oct 30 decisions are more than 3 days old by Nov 4
    assert files["settings"]["mode"] == "paper" and files["settings"]["test_years_locked"]
    m = files["reports/monthly"]
    assert m["since"] == "2026-10-30" and m["winning"] is None and m["turnover_pct"] > 0
    assert sum(s["positions"] for s in m["slices"]) == len(h["holdings"])
    assert files["health"]["last_restore_test"] == {"date": "2026-11-07", "ok": True}
    broken = files["health"]["checks"][-1]
    assert broken["outcome"] == "log" and "scheduled run ended in failure" in broken["detail"]
    assert any("ended in failure" in n for n in m["notes"])


def test_before_the_first_month_end_and_on_a_blocked_day(tmp_path):
    out, bench = market(tmp_path, end="2026-10-29")
    (out / "db").mkdir()
    con = connect(out / "db" / "app.sqlite")
    live.run(out, con, utc(2026, 10, 30, 1, 17), bench=bench)
    files = site.build(out, blocked="only 80% of index stocks priced")
    assert files["holdings"]["holdings"] == [] and files["today"]["status"] == "blocked"
    assert any("starts at the first month-end" in q["text"] for q in files["today"]["quiet"])
    assert all(p["verdict"] == "Wait" for p in files["picks"]["picks"])
    health = json.loads((out / "site" / "data" / "health.json").read_text())
    assert health["status"] == "blocked" and "Not measured tonight" in health["checks"][-1]["detail"]
