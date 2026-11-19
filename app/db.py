"""SQLite storage layer.

Tables:
  trades              -- current state per (symbol, trade_no), keyed by revision
  minute_bars         -- current materialized minute bar per (symbol, minute)
  journal             -- append-only change log; its seq is the cursor risk
                         control uses. Rows may be reclaimed per symbol, but
                         AUTOINCREMENT guarantees seq values are never reused.
  meta                -- global durable watermark (largest journal seq ever
                         committed). Survives even when the whole journal is
                         reclaimed, so snapshots still return a committed
                         global cursor and new trades keep increasing seq.
  reclaim_watermarks  -- per-symbol reclaim watermark. A client cursor below
                         its symbol's watermark references journal rows that
                         have been deleted and must take a fresh snapshot.
                         Durable across restarts.

WAL + synchronous=FULL: a committed transaction survives a process restart or
power loss, so a cursor a client has already seen can never be "uncommitted".
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    symbol            TEXT    NOT NULL,
    trade_no          TEXT    NOT NULL,
    revision          INTEGER NOT NULL,
    op                TEXT    NOT NULL CHECK (op IN ('trade', 'correction', 'cancel')),
    trade_time_utc_ms INTEGER,
    price_cents       INTEGER,
    qty               INTEGER,
    cancelled         INTEGER NOT NULL DEFAULT 0,
    trade_no_num      INTEGER,
    PRIMARY KEY (symbol, trade_no)
);

CREATE TABLE IF NOT EXISTS minute_bars (
    symbol              TEXT    NOT NULL,
    minute_start_utc_ms INTEGER NOT NULL,
    open_cents          INTEGER NOT NULL,
    high_cents          INTEGER NOT NULL,
    low_cents           INTEGER NOT NULL,
    close_cents         INTEGER NOT NULL,
    volume              INTEGER NOT NULL,
    trade_count         INTEGER NOT NULL,
    PRIMARY KEY (symbol, minute_start_utc_ms)
);

CREATE TABLE IF NOT EXISTS journal (
    seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT    NOT NULL,
    minute_start_utc_ms INTEGER NOT NULL,
    change_type         TEXT    NOT NULL CHECK (change_type IN ('upsert', 'delete')),
    bar_json            TEXT
);

CREATE INDEX IF NOT EXISTS idx_journal_symbol_seq ON journal (symbol, seq);
CREATE INDEX IF NOT EXISTS idx_trades_minute ON trades (symbol, cancelled, trade_time_utc_ms);

-- Global durable state (currently only the journal high-water mark).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

-- Per-symbol reclaim watermark: every journal row with seq <= watermark_seq
-- for that symbol has been deleted. Monotonic, never moves backwards.
CREATE TABLE IF NOT EXISTS reclaim_watermarks (
    symbol        TEXT PRIMARY KEY,
    watermark_seq INTEGER NOT NULL,
    reclaimed_at  TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None -> autocommit; transactions are opened explicitly
    # with BEGIN / BEGIN IMMEDIATE in logic.py so commit boundaries are exact.
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.executescript(SCHEMA)
        # Upgrade path for databases created before the meta table existed:
        # seed the global watermark from whatever journal rows are present.
        # Only fills when the key is absent, so it never moves the mark back.
        # (Check-then-insert on a fresh connection: no other writer exists yet.)
        exists = conn.execute(
            "SELECT 1 FROM meta WHERE key = 'journal_high_water'"
        ).fetchone()
        if exists is None:
            seed = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM journal").fetchone()[0]
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('journal_high_water', ?)",
                (seed,),
            )
    finally:
        conn.close()
