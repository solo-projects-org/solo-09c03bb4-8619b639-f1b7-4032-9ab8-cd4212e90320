"""Core feed logic: revision-checked ingest, minute-bar recompute, cursor pulls.

Consistency rules:
  * Every ingest runs in ONE immediate transaction: trade-state upsert +
    minute-bar recompute + journal insert commit or roll back together.
    A failed write or a crash therefore never leaves a half-applied update,
    and a committed journal seq (cursor) can never be skipped afterwards.
  * Reads (snapshot / pull) run in a single read transaction, so clients only
    ever see fully committed changes.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

MINUTE_MS = 60_000
INT64_MAX = 9_223_372_036_854_775_807

BAR_KEYS = (
    "minute_start_utc_ms",
    "open_cents",
    "high_cents",
    "low_cents",
    "close_cents",
    "volume",
    "trade_count",
)


class ConflictError(Exception):
    """Same revision already stored with different content -> HTTP 409."""


class CursorAheadError(Exception):
    """Client cursor is ahead of the committed journal -> HTTP 409."""

    def __init__(self, cursor: int, max_seq: int):
        self.cursor = cursor
        self.max_seq = max_seq
        super().__init__(
            f"cursor {cursor} 超过当前已提交的最大序号 {max_seq}，请重新快照"
        )


class CursorExpiredError(Exception):
    """Client cursor is at/below this symbol's reclaim watermark -> HTTP 410.

    The journal rows the cursor would continue from have been reclaimed, so
    the client must take a fresh snapshot before pulling changes again.
    """

    def __init__(self, cursor: int, watermark: int, symbol: str):
        self.cursor = cursor
        self.watermark = watermark
        self.symbol = symbol
        super().__init__(
            f"股票 {symbol} 的游标 {cursor} 已低于日志回收水位 {watermark}，"
            "对应变更日志已被回收，请重新获取快照"
        )


HIGH_WATER_KEY = "journal_high_water"


def minute_floor(ts_ms: int) -> int:
    return ts_ms - (ts_ms % MINUTE_MS)


def _trade_no_num(trade_no: str) -> Optional[int]:
    """Numeric sort key for all-digit trade numbers (must fit SQLite int64)."""
    if trade_no.isdigit():
        value = int(trade_no)
        if value <= INT64_MAX:
            return value
    return None


def _incoming_content(msg) -> tuple:
    if msg.op == "cancel":
        return ("cancel", None, None, None)
    return (msg.op, msg.trade_time_utc_ms, msg.price_cents, msg.qty)


def _stored_content(row: sqlite3.Row) -> tuple:
    return (row["op"], row["trade_time_utc_ms"], row["price_cents"], row["qty"])


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def apply_message(conn: sqlite3.Connection, msg) -> dict[str, Any]:
    """Apply one message. Returns {status, current_revision, affected_minutes}.

    Revision rules per (symbol, trade_no):
      revision <  stored : stale_ignored -- late message, never rolls back state
      revision == stored : identical content -> duplicate (idempotent);
                           different content  -> ConflictError (409)
      revision >  stored : applied (gaps allowed)
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE symbol = ? AND trade_no = ?",
            (msg.symbol, msg.trade_no),
        ).fetchone()

        if row is not None and msg.revision < row["revision"]:
            conn.execute("COMMIT")
            return {
                "status": "stale_ignored",
                "current_revision": row["revision"],
                "affected_minutes": [],
            }

        if row is not None and msg.revision == row["revision"]:
            if _stored_content(row) == _incoming_content(msg):
                conn.execute("COMMIT")
                return {
                    "status": "duplicate",
                    "current_revision": row["revision"],
                    "affected_minutes": [],
                }
            raise ConflictError(
                f"{msg.symbol}/{msg.trade_no} 修订号 {msg.revision} "
                "已存在且内容不同，拒绝写入"
            )

        old_minute: Optional[int] = None
        if row is not None and not row["cancelled"]:
            old_minute = minute_floor(row["trade_time_utc_ms"])

        cancelled = msg.op == "cancel"
        conn.execute(
            """
            INSERT INTO trades (symbol, trade_no, revision, op,
                                trade_time_utc_ms, price_cents, qty,
                                cancelled, trade_no_num)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, trade_no) DO UPDATE SET
                revision          = excluded.revision,
                op                = excluded.op,
                trade_time_utc_ms = excluded.trade_time_utc_ms,
                price_cents       = excluded.price_cents,
                qty               = excluded.qty,
                cancelled         = excluded.cancelled,
                trade_no_num      = excluded.trade_no_num
            """,
            (
                msg.symbol,
                msg.trade_no,
                msg.revision,
                msg.op,
                None if cancelled else msg.trade_time_utc_ms,
                None if cancelled else msg.price_cents,
                None if cancelled else msg.qty,
                1 if cancelled else 0,
                _trade_no_num(msg.trade_no),
            ),
        )

        new_minute = None if cancelled else minute_floor(msg.trade_time_utc_ms)

        # Cross-minute corrections touch both the old and the new minute;
        # both recomputes (and their journal rows) commit in this transaction.
        affected = []
        for minute in sorted({m for m in (old_minute, new_minute) if m is not None}):
            if recompute_minute(conn, msg.symbol, minute):
                affected.append(minute)

        conn.execute("COMMIT")
        return {
            "status": "applied",
            "current_revision": msg.revision,
            "affected_minutes": affected,
        }
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# minute bars
# ---------------------------------------------------------------------------

def _trade_order_key(row: sqlite3.Row) -> tuple:
    # Open/close are determined by (trade_time_utc_ms, trade_no); all-digit
    # trade numbers compare numerically, others lexicographically.
    if row["trade_no_num"] is not None:
        no_key = (0, row["trade_no_num"], "")
    else:
        no_key = (1, 0, row["trade_no"])
    return (row["trade_time_utc_ms"], no_key)


def recompute_minute(conn: sqlite3.Connection, symbol: str, minute_ms: int) -> bool:
    """Rebuild one minute bar from active trades and journal the visible change.

    Returns True if a journal row was written (bar created/updated/deleted).
    Must be called inside the ingest transaction.
    """
    rows = conn.execute(
        """
        SELECT trade_time_utc_ms, trade_no, trade_no_num, price_cents, qty
        FROM trades
        WHERE symbol = ? AND cancelled = 0
          AND trade_time_utc_ms >= ? AND trade_time_utc_ms < ?
        """,
        (symbol, minute_ms, minute_ms + MINUTE_MS),
    ).fetchall()

    existing = conn.execute(
        "SELECT * FROM minute_bars WHERE symbol = ? AND minute_start_utc_ms = ?",
        (symbol, minute_ms),
    ).fetchone()

    if not rows:
        # Last trade of the minute disappeared -> delete marker, but only if a
        # bar was ever published for this minute.
        if existing is None:
            return False
        conn.execute(
            "DELETE FROM minute_bars WHERE symbol = ? AND minute_start_utc_ms = ?",
            (symbol, minute_ms),
        )
        _journal(conn, symbol, minute_ms, "delete", None)
        return True

    ordered = sorted(rows, key=_trade_order_key)
    bar = {
        "minute_start_utc_ms": minute_ms,
        "open_cents": ordered[0]["price_cents"],
        "high_cents": max(r["price_cents"] for r in ordered),
        "low_cents": min(r["price_cents"] for r in ordered),
        "close_cents": ordered[-1]["price_cents"],
        "volume": sum(r["qty"] for r in ordered),
        "trade_count": len(ordered),
    }

    if existing is not None and all(existing[k] == v for k, v in bar.items()):
        return False  # visible state unchanged -> no journal noise

    conn.execute(
        """
        INSERT INTO minute_bars (symbol, minute_start_utc_ms, open_cents, high_cents,
                                 low_cents, close_cents, volume, trade_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, minute_start_utc_ms) DO UPDATE SET
            open_cents  = excluded.open_cents,
            high_cents  = excluded.high_cents,
            low_cents   = excluded.low_cents,
            close_cents = excluded.close_cents,
            volume      = excluded.volume,
            trade_count = excluded.trade_count
        """,
        (
            symbol,
            minute_ms,
            bar["open_cents"],
            bar["high_cents"],
            bar["low_cents"],
            bar["close_cents"],
            bar["volume"],
            bar["trade_count"],
        ),
    )
    _journal(conn, symbol, minute_ms, "upsert", bar)
    return True


def _journal(
    conn: sqlite3.Connection,
    symbol: str,
    minute_ms: int,
    change_type: str,
    bar: Optional[dict],
) -> None:
    cur = conn.execute(
        """
        INSERT INTO journal (symbol, minute_start_utc_ms, change_type, bar_json)
        VALUES (?, ?, ?, ?)
        """,
        (symbol, minute_ms, change_type, None if bar is None else json.dumps(bar, sort_keys=True)),
    )
    # Advance the durable global watermark in the SAME transaction. It records
    # the largest seq ever committed, so it survives even a full journal
    # reclaim and lets snapshots keep returning a committed global cursor.
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)
        """,
        (HIGH_WATER_KEY, cur.lastrowid),
    )


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

def _high_water(conn: sqlite3.Connection) -> int:
    """Largest journal seq ever committed (durable even if the journal is empty)."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (HIGH_WATER_KEY,)
    ).fetchone()
    return 0 if row is None else row["value"]


def _symbol_watermark(conn: sqlite3.Connection, symbol: str) -> int:
    """Reclaim watermark of one symbol: rows with seq <= it no longer exist."""
    row = conn.execute(
        "SELECT watermark_seq FROM reclaim_watermarks WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return 0 if row is None else row["watermark_seq"]


def reclaim_journal(conn: sqlite3.Connection, symbol: str, keep: int) -> dict[str, Any]:
    """Reclaim one symbol's change log, keeping only its newest `keep` rows.

    Deletes journal rows strictly older than the keep boundary and advances
    the symbol's durable reclaim watermark to the largest deleted seq
    (0 when there was nothing to delete). Everything runs in ONE immediate
    transaction together with the watermark update: any failure rolls back,
    leaving both the original log and the watermark untouched.

    Other symbols' rows and watermarks are never touched, so their cursors
    never get false "expired" errors. The global high-water mark is also
    untouched; AUTOINCREMENT keeps handing out fresh, ever-increasing seq
    values after the journal is emptied.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = _symbol_watermark(conn, symbol)

        # Largest seq that must be deleted: with keep=0 it is simply the
        # symbol's maximum seq; otherwise it is the seq keep positions back.
        # Returns no row when the symbol does not even have `keep` rows.
        boundary_row = conn.execute(
            """
            SELECT seq FROM journal
            WHERE symbol = ?
            ORDER BY seq DESC
            LIMIT 1 OFFSET ?
            """,
            (symbol, keep),
        ).fetchone()

        if boundary_row is None or boundary_row["seq"] <= previous:
            # Nothing beyond the retention window. The watermark may still be
            # positive from an earlier reclaim: report it without changing it.
            conn.execute("COMMIT")
            return {
                "symbol": symbol,
                "keep": keep,
                "deleted_count": 0,
                "reclaim_watermark": previous,
                "global_high_water": _high_water(conn),
            }

        boundary = boundary_row["seq"]
        deleted = conn.execute(
            "DELETE FROM journal WHERE symbol = ? AND seq <= ?",
            (symbol, boundary),
        ).rowcount

        conn.execute(
            """
            INSERT INTO reclaim_watermarks (symbol, watermark_seq, reclaimed_at)
            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ON CONFLICT(symbol) DO UPDATE SET
                watermark_seq = MAX(reclaim_watermarks.watermark_seq, excluded.watermark_seq),
                reclaimed_at  = excluded.reclaimed_at
            """,
            (symbol, boundary),
        )

        result = {
            "symbol": symbol,
            "keep": keep,
            "deleted_count": deleted,
            "reclaim_watermark": boundary,
            "global_high_water": _high_water(conn),
        }
        conn.execute("COMMIT")
        return result
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def snapshot_bars(conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    """First query: all current minute bars plus a cursor consistent with them.

    The cursor is the durable GLOBAL high-water mark (largest seq ever
    committed), so it stays valid and non-zero even after the whole journal
    has been reclaimed. Bars and cursor are read in one read transaction.
    """
    conn.execute("BEGIN")
    try:
        cursor = _high_water(conn)
        rows = conn.execute(
            "SELECT * FROM minute_bars WHERE symbol = ? ORDER BY minute_start_utc_ms",
            (symbol,),
        ).fetchall()
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "symbol": symbol,
        "cursor": cursor,
        "bars": [{k: row[k] for k in BAR_KEYS} for row in rows],
    }


def pull_changes(
    conn: sqlite3.Connection, symbol: str, cursor: int, limit: int
) -> dict[str, Any]:
    """Incremental pull: committed changes for one symbol with seq > cursor.

    All checks and reads happen in one read transaction, so the returned
    changes always correspond to one consistent view of the journal and both
    watermarks:
      * cursor <= this symbol's reclaim watermark -> 410 CursorExpiredError,
        the rows it builds on have been reclaimed; take a fresh snapshot.
      * cursor > global high-water mark -> 409 CursorAheadError as before.
      * otherwise only surviving rows of THIS symbol are delivered, so
        reclaiming another symbol can never produce a false expiry here.
    """
    conn.execute("BEGIN")
    try:
        max_seq = _high_water(conn)
        watermark = _symbol_watermark(conn, symbol)
        # Reclaim deletes rows with seq <= watermark, so a cursor strictly
        # below the watermark can no longer be continued. A cursor equal to
        # the watermark simply starts after the last deleted row and stays
        # valid (also keeps cursor=0 working before any reclaim).
        if cursor < watermark:
            raise CursorExpiredError(cursor, watermark, symbol)
        if cursor > max_seq:
            raise CursorAheadError(cursor, max_seq)
        rows = conn.execute(
            "SELECT * FROM journal WHERE symbol = ? AND seq > ? ORDER BY seq LIMIT ?",
            (symbol, cursor, limit),
        ).fetchall()
        has_more = len(rows) == limit
        # Fewer rows than the limit => we saw everything committed up to max_seq.
        next_cursor = rows[-1]["seq"] if has_more else max_seq
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    changes = [
        {
            "seq": r["seq"],
            "minute_start_utc_ms": r["minute_start_utc_ms"],
            "type": r["change_type"],
            "bar": None if r["bar_json"] is None else json.loads(r["bar_json"]),
        }
        for r in rows
    ]
    return {
        "symbol": symbol,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "changes": changes,
    }


def get_trade(conn: sqlite3.Connection, symbol: str, trade_no: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM trades WHERE symbol = ? AND trade_no = ?", (symbol, trade_no)
    ).fetchone()
    if row is None:
        return None
    return {
        "symbol": row["symbol"],
        "trade_no": row["trade_no"],
        "revision": row["revision"],
        "op": row["op"],
        "trade_time_utc_ms": row["trade_time_utc_ms"],
        "price_cents": row["price_cents"],
        "qty": row["qty"],
        "cancelled": bool(row["cancelled"]),
    }
