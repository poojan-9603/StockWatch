import sqlite3

from pipeline.db import add_verdict, connect, export_text, ingest_inbox, verify_chain


def test_tables_are_append_only(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    ingest_inbox(con, {"t1.json": {"kind": "deposit", "date": "2026-10-01", "amount": 1000}})
    for sql in ["UPDATE trades SET amount = 1", "DELETE FROM trades"]:
        try:
            con.execute(sql)
        except sqlite3.IntegrityError as e:
            assert "append-only" in str(e)
        else:
            raise AssertionError(f"{sql} went through")


def test_verdict_hash_chain_detects_tampering(tmp_path):
    p = tmp_path / "app.sqlite"
    con = connect(p)
    for v in ["Buy", "Wait", "Hold"]:
        add_verdict(con, date="2026-10-26", symbol="AAPL", verdict=v, score=80.0)
    assert verify_chain(con) is None
    raw = sqlite3.connect(p)  # someone edits a past verdict behind the triggers' back
    raw.execute("DROP TRIGGER verdicts_no_update")
    raw.execute("UPDATE verdicts SET verdict = 'Sell' WHERE id = 2")
    raw.commit()
    assert verify_chain(connect(p)) == 2


def test_inbox_is_idempotent_and_rejects_bad_trades(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    files = {"a.json": {"kind": "buy", "date": "2026-10-20", "symbol": "AAPL", "shares": 10, "price": 250.0},
             "b.json": {"kind": "buy", "date": "2026-10-20", "symbol": "AAPL", "shares": 0, "price": 250.0},
             "c.json": {"kind": "buy", "date": "2026-10-20", "symbol": "AAPL", "shares": 1, "price": 1, "hack": 1}}
    assert ingest_inbox(con, files)[:2] == (1, 0)
    added, seen, bad = ingest_inbox(con, files)
    assert (added, seen) == (0, 1) and set(bad) == {"b.json", "c.json"}
    counts = export_text(con, tmp_path / "text")
    assert counts["trades"] == 1 and (tmp_path / "text" / "verdicts.csv").exists()


def test_nightly_backup_then_restore_passes_and_refuses_to_start_empty(tmp_path):
    import json

    from pipeline.backup import nightly, restore
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "t1.json").write_text(json.dumps({"kind": "deposit", "date": "2026-10-01", "amount": 5000}))
    try:
        nightly(tmp_path, first_run=False)  # no database and not told it's the first run: stop, don't create one
    except SystemExit as e:
        assert "refusing" in str(e)
    nightly(tmp_path, first_run=True)
    add_verdict(connect(tmp_path / "db" / "app.sqlite"), date="2026-10-26", verdict="Wait")
    nightly(tmp_path, first_run=False)
    assert len(list((tmp_path / "backups" / "monthly").glob("*.sqlite"))) == 1
    restore(tmp_path)  # passes: integrity, chain, counts match the export


def test_inbox_rejects_wrong_types_instead_of_crashing(tmp_path):
    con = connect(tmp_path / "app.sqlite")
    added, _, bad = ingest_inbox(con, {"a.json": {"kind": "buy", "date": "2026-10-01", "symbol": "AAA",
                                                  "shares": "10", "price": 5.0}})
    assert added == 0 and "a.json" in bad


def test_experiments_are_added_once(tmp_path):
    from pipeline.db import ingest_experiments
    con = connect(tmp_path / "app.sqlite")
    e = {"x.json": {"created_at": "2026-10-27T00:00:00+00:00", "name": "M3 tuning: base", "params": "{}",
                    "result": "{}", "note": "n"}}
    assert ingest_experiments(con, e)[:2] == (1, 0)
    assert ingest_experiments(con, e)[:2] == (0, 1)
