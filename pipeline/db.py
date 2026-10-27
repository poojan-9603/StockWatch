"""The irreplaceable records: SQLite, append-only (triggers reject UPDATE and DELETE), with a hash chain on verdicts.

A correction is a new row in `corrections` pointing at the row it fixes. Current holdings are derived from trades and
corrections, never stored. Only the pipeline writes this database; the Worker drops trades into R2 inbox/ as JSON
files and `ingest_inbox` adds them (one writer).
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

TABLES = {
    "trades": """id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('buy','sell','deposit','withdrawal')),
                 date TEXT NOT NULL, cik INTEGER, symbol TEXT, lot_id TEXT, shares REAL, price REAL, amount REAL,
                 fees REAL DEFAULT 0, note TEXT, source TEXT NOT NULL, inbox_file TEXT UNIQUE, created_at TEXT NOT NULL""",
    "corrections": """id INTEGER PRIMARY KEY, table_name TEXT NOT NULL, row_id INTEGER NOT NULL, reason TEXT NOT NULL,
                      payload TEXT NOT NULL, created_at TEXT NOT NULL""",
    "verdicts": """id INTEGER PRIMARY KEY, date TEXT NOT NULL, cik INTEGER, symbol TEXT, verdict TEXT NOT NULL, score REAL,
                   signal_ranks TEXT, gates TEXT, created_at TEXT NOT NULL, prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL""",
    "buy_snapshots": """id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL REFERENCES trades(id), lot_id TEXT,
                        snapshot TEXT NOT NULL, created_at TEXT NOT NULL""",
    "panel_opinions": """id INTEGER PRIMARY KEY, date TEXT NOT NULL, cik INTEGER, symbol TEXT, verdict TEXT,
                         confidence REAL, claims TEXT, model TEXT, created_at TEXT NOT NULL""",
    "alerts": """id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, tier TEXT NOT NULL, channel TEXT NOT NULL,
                 subject TEXT, body TEXT, sent_at TEXT""",
    "experiments": """id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL, params TEXT, result TEXT,
                      note TEXT""",
}
GENESIS = "0" * 64


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path):
    """Open (creating if needed) the database with every table and its append-only triggers."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    for name, cols in TABLES.items():
        con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({cols})")
        for op in ("UPDATE", "DELETE"):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {name}_no_{op.lower()} BEFORE {op} ON {name} "
                        f"BEGIN SELECT RAISE(ABORT, '{name} is append-only: add a correction instead'); END")
    con.commit()
    return con


def verdict_hash(prev, row):
    """sha256 of the previous hash and the row's fields as sorted JSON."""
    body = json.dumps({k: row[k] for k in sorted(row)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


def add_verdict(con, **row):
    row = {"cik": None, "symbol": None, "score": None, "signal_ranks": None, "gates": None, **row,
           "created_at": now()}
    last = con.execute("SELECT row_hash FROM verdicts ORDER BY id DESC LIMIT 1").fetchone()
    prev = last[0] if last else GENESIS
    h = verdict_hash(prev, row)
    cols = list(row) + ["prev_hash", "row_hash"]
    con.execute(f"INSERT INTO verdicts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row[c] for c in row] + [prev, h])
    con.commit()
    return h


def verify_chain(con):
    """Recompute every verdict's hash. Returns the id of the first broken row, or None if the chain holds."""
    prev = GENESIS
    cur = con.execute("SELECT * FROM verdicts ORDER BY id")
    names = [d[0] for d in cur.description]
    for r in cur:
        row = dict(zip(names, r))
        rid, stored_prev, stored = row.pop("id"), row.pop("prev_hash"), row.pop("row_hash")
        if stored_prev != prev or verdict_hash(prev, row) != stored:
            return rid
        prev = stored
    return None


TRADE_FIELDS = {"kind", "date", "cik", "symbol", "lot_id", "shares", "price", "amount", "fees", "note"}


def ingest_inbox(con, files):
    """files: {inbox file name: parsed JSON}. Adds each valid trade once (inbox_file is unique, so rerunning is
    safe). Returns (added, already there, rejected {name: reason})."""
    added, seen, bad = 0, 0, {}
    for name, t in sorted(files.items()):
        if con.execute("SELECT 1 FROM trades WHERE inbox_file = ?", (name,)).fetchone():
            seen += 1
            continue
        extra = set(t) - TRADE_FIELDS
        if extra or t.get("kind") not in ("buy", "sell", "deposit", "withdrawal") or not t.get("date"):
            bad[name] = f"bad fields or kind: {sorted(extra) or t.get('kind')}"
            continue
        num = lambda k: isinstance(t.get(k), (int, float)) and not isinstance(t.get(k), bool)
        if any(t.get(k) is not None and not num(k) for k in ("cik", "shares", "price", "amount", "fees")):
            bad[name] = "cik, shares, price, amount and fees must be numbers"
            continue
        if t["kind"] in ("buy", "sell") and not (t.get("symbol") and num("shares") and t["shares"] > 0
                                                  and num("price") and t["price"] > 0):
            bad[name] = "buy and sell need symbol, shares > 0 and price > 0"
            continue
        row = {**{k: t.get(k) for k in TRADE_FIELDS}, "fees": t.get("fees") or 0, "source": "inbox",
               "inbox_file": name, "created_at": now()}
        con.execute(f"INSERT INTO trades ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        added += 1
    con.commit()
    return added, seen, bad


EXPERIMENT_FIELDS = {"created_at", "name", "params", "result", "note"}


def ingest_experiments(con, files):
    """files: {name: parsed JSON} from inbox/experiments/, written by quant.backtest. Each is added once (an
    identical created_at, name and params already there counts as seen). Returns (added, seen, rejected)."""
    added, seen, bad = 0, 0, {}
    for name, e in sorted(files.items()):
        if set(e) - EXPERIMENT_FIELDS or not (e.get("created_at") and e.get("name")):
            bad[name] = "bad fields"
            continue
        if con.execute("SELECT 1 FROM experiments WHERE created_at = ? AND name = ? AND params IS ?",
                       (e["created_at"], e["name"], e.get("params"))).fetchone():
            seen += 1
            continue
        con.execute("INSERT INTO experiments (created_at, name, params, result, note) VALUES (?, ?, ?, ?, ?)",
                    [e.get(k) for k in ("created_at", "name", "params", "result", "note")])
        added += 1
    con.commit()
    return added, seen, bad


def export_text(con, folder):
    """One CSV per table, ordered by id: the nightly text backup."""
    import csv
    folder.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name in TABLES:
        cur = con.execute(f"SELECT * FROM {name} ORDER BY id")
        with open(folder / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            n = 0
            for r in cur:
                w.writerow(r)
                n += 1
        counts[name] = n
    return counts
