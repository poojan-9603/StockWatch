"""Records upkeep: ingest the inbox, snapshot the database, export it as text, and test a restore.

Usage: python -m pipeline.backup OUT_DIR nightly|restore
  nightly  out/db/app.sqlite (created on the very first run only) gets the inbox trades in out/inbox/*.json; then
           out/backups/daily/<date>.sqlite, out/backups/monthly/<yyyy-mm>.sqlite on the month's first run, and the text
           export in out/backup_text/ (pushed to the backup repo by the workflow)
  restore  opens the newest snapshot in out/backups/daily/: integrity check, verdict hash chain, and row counts
           against the text export in out/backup_text/. Exits non-zero on any mismatch.
"""
import csv
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from pipeline.db import TABLES, connect, export_text, ingest_inbox, verify_chain
from pipeline.prices import today


def nightly(out, first_run):
    db = out / "db" / "app.sqlite"
    if not db.exists() and not first_run:
        sys.exit("db/app.sqlite is missing but R2 says one exists: refusing to start a new, empty database")
    db.parent.mkdir(parents=True, exist_ok=True)
    con = connect(db)
    files = {p.name: json.loads(p.read_text()) for p in sorted((out / "inbox").glob("*.json"))}
    added, seen, bad = ingest_inbox(con, files)
    counts = export_text(con, out / "backup_text")
    con.close()
    daily = out / "backups" / "daily" / f"{today()}.sqlite"
    daily.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db, daily)
    monthly = out / "backups" / "monthly" / f"{today()[:7]}.sqlite"
    if not monthly.exists():
        monthly.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db, monthly)
    print(json.dumps({"inbox_added": added, "inbox_seen": seen, "inbox_rejected": bad, "rows": counts}, indent=1))
    if bad:
        sys.exit(f"inbox files rejected, left in place for review: {sorted(bad)}")


def restore(out):
    snaps = sorted((out / "backups" / "daily").glob("*.sqlite"))
    if not snaps:
        sys.exit("no daily snapshot to restore")
    errors = []
    con = sqlite3.connect(snaps[-1])
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        errors.append("integrity_check failed")
    con.close()
    con = connect(snaps[-1])
    broken = verify_chain(con)
    if broken is not None:
        errors.append(f"verdict hash chain broken at row {broken}")
    for name in TABLES:
        n = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        p = out / "backup_text" / f"{name}.csv"
        t = sum(1 for _ in csv.reader(open(p, encoding="utf-8", newline=""))) - 1 if p.exists() else None
        if t is not None and t != n:  # the text export can lag a day behind; equal is the normal case
            errors.append(f"{name}: snapshot has {n} rows, text export {t}")
    print(f"restore test on {snaps[-1].name}: {'PASS' if not errors else 'FAIL: ' + '; '.join(errors)}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    out, step = Path(sys.argv[1]), sys.argv[2]
    nightly(out, first_run="--first-run" in sys.argv) if step == "nightly" else restore(out)
