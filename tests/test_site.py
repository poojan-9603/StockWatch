"""The site's data files from a synthetic market run through live scoring: every file passes the contract, the paper
book shows as holdings, the month-end buys show as actions, today's buys (not held) as picks, and app alerts show on Today."""
import json
from datetime import datetime, timezone

import pandas as pd

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
    picks = files["picks"]["picks"]
    held = {x["cik"] for x in h["holdings"]}
    assert all(p["verdict"] == "Buy" and p["size_pct"] > 0 for p in picks)  # today's buys, sized
    assert not {p["cik"] for p in picks} & held  # owned stocks are on Holdings, not Picks
    assert files["picks"]["decided"] == "2026-10-30"  # the month-end the Buys come from, shown on the page
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
    assert all(p["verdict"] == "Buy" for p in files["picks"]["picks"])  # daily picks, before any month-end too
    health = json.loads((out / "site" / "data" / "health.json").read_text())
    assert health["status"] == "blocked" and "Not measured tonight" in health["checks"][-1]["detail"]


def test_a_blocked_day_right_after_month_end_shows_no_actions(tmp_path):
    out, bench = market(tmp_path)
    (out / "db").mkdir()
    con = connect(out / "db" / "app.sqlite")
    live.run(out, con, utc(2026, 10, 31, 1, 17), bench=bench)  # the month-end Buys, then a blocked night (2026-11-03)
    files = site.build(out, blocked="only 0.0% of index stocks priced")
    assert files["today"]["status"] == "blocked" and files["today"]["actions"] == []


def test_picks_new_since_the_last_scored_day(tmp_path):
    out, bench = market(tmp_path)
    (out / "db").mkdir()
    con = connect(out / "db" / "app.sqlite")
    live.run(out, con, utc(2026, 10, 31, 1, 17), bench=bench)
    sc = out / "market" / "scores" / "latest.parquet"
    pd.read_parquet(sc).assign(sector=float("nan")).to_parquet(sc)  # no sector known: NaN, as on 2026-10-31
    first = site.build(out)["picks"]["picks"]
    assert {p["sector"] for p in first} == {"Unknown"}
    assert len(first) > 1 and not any(p["new_today"] for p in first)  # bought at the next open, so not held yet
    seen = out / "market" / "paper" / "picks_seen.json"
    s = json.loads(seen.read_text())
    seen.write_text(json.dumps(s | {"day": "2026-10-29", "ciks": s["ciks"][1:]}))  # yesterday lacked the top pick
    files = site.build(out)
    assert [p["new_today"] for p in files["picks"]["picks"]] == [True] + [False] * (len(first) - 1)
    assert f"New on Picks today: {first[0]['symbol']}." in [q["text"] for q in files["today"]["quiet"]]
    again = site.build(out)  # a rerun the same night keeps the same comparison
    assert [p["new_today"] for p in again["picks"]["picks"]] == [True] + [False] * (len(first) - 1)


def test_leader_dip_watch_themes_strong_industries_and_odds(tmp_path):
    out, bench = market(tmp_path, end="2026-10-29")
    (out / "db").mkdir()
    con = connect(out / "db" / "app.sqlite")
    log = live.run(out, con, utc(2026, 10, 30, 1, 17), bench=bench)
    f = out / "market" / "scores" / "leaders.parquet"
    lead = pd.read_parquet(f)
    assert len(lead) == 20 and lead["drawdown"].notna().all() and "leaders_down_20" in log  # 20 members, all leaders
    assert set(lead.loc[lead["tech"], "sic"]) == {"3570"}
    pd.DataFrame({"cik": [1000, 1001, 1003, 1005], "strong": [False, True, False, False],
                  "tech": [True, False, False, True], "drawdown": [-0.25, -0.21, -0.40, -0.15],
                  "drawdown_prev": [-0.10, -0.22, -0.40, -0.10], "sic": ["3570", "3580", "3600", "3570"]}).to_parquet(f)
    files = site.build(out)
    d = files["picks"]["dips"]
    # 1003 is down 40% but in no theme and no strong industry; 1005 is in a theme but only 15% down
    assert [x["symbol"] for x in d] == ["S00", "S01"] and [x["below_high_pct"] for x in d] == [25.0, 21.0]
    assert d[0]["new_today"] and not d[1]["new_today"]  # S01 was already 22% down the day before
    assert d[0]["why"].startswith("Theme: computers") and "big tech leaders" in d[0]["history"]
    assert "strongest third" in d[1]["why"] and "coin flip" in d[1]["history"]
    assert any("S00 (25%)" in q["text"] and "S01" not in q["text"] for q in files["today"]["quiet"])
