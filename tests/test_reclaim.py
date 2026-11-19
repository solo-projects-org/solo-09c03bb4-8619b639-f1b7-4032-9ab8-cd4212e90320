"""Tests for per-symbol journal reclamation and cursor expiry.

Run: python -m pytest tests/test_reclaim.py -v
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import logic
from app.main import create_app

# 2023-11-14T22:13:20Z in epoch ms; its minute starts at T0.
T0 = 1_699_999_980_000
T_IN_0 = T0 + 20_000
T_IN_1 = T0 + 60_000 + 5_000
M0, M1 = T0, T0 + 60_000


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def msg(symbol="600519", trade_no="1", revision=1, op="trade",
        t=T_IN_0, price=10000, qty=100):
    m = {"symbol": symbol, "trade_no": trade_no, "revision": revision, "op": op}
    if op != "cancel":
        m.update(trade_time_utc_ms=t, price_cents=price, qty=qty)
    return m


def post(client, m):
    return client.post("/v1/trades", json=m)


def bars(client, symbol="600519"):
    return client.get(f"/v1/symbols/{symbol}/bars").json()


def changes_raw(client, cursor, symbol="600519", limit=500):
    return client.get(
        f"/v1/symbols/{symbol}/changes", params={"cursor": cursor, "limit": limit}
    )


def changes(client, cursor, symbol="600519", limit=500):
    r = changes_raw(client, cursor, symbol, limit)
    assert r.status_code == 200, r.text
    return r.json()


def reclaim(client, keep, symbol="600519"):
    return client.post(
        f"/v1/symbols/{symbol}/journal/reclaim", json={"keep": keep}
    )


def feed_n(client, n, symbol="600519", start=1):
    """Submit n distinct trades (each yields one upsert journal row)."""
    for i in range(start, start + n):
        # Different minutes so every trade creates its own bar/journal row.
        t = T0 + (i - start) * 60_000 + 1_000
        r = post(client, msg(symbol=symbol, trade_no=str(i), t=t))
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# basic retention semantics
# ---------------------------------------------------------------------------

def test_reclaim_keeps_newest_rows_and_sets_watermark(client):
    feed_n(client, 5)
    r = reclaim(client, keep=2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_count"] == 3
    assert body["reclaim_watermark"] == 3          # largest deleted seq
    assert body["global_high_water"] == 5

    page = changes(client, 3)                       # cursor == watermark is fine
    assert [c["seq"] for c in page["changes"]] == [4, 5]
    assert page["next_cursor"] == 5


def test_reclaim_keep_zero_clears_all_rows(client):
    feed_n(client, 3)
    r = reclaim(client, keep=0)
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_count"] == 3
    assert body["reclaim_watermark"] == 3
    assert body["global_high_water"] == 3
    # The journal itself is empty now.
    assert changes(client, 3)["changes"] == []


def test_reclaim_expired_cursor_gets_410_and_snapshot_recovers(client):
    feed_n(client, 4)
    old_cursor = 2
    reclaim(client, keep=1)                         # deletes seqs 1..3, watermark=3

    r = changes_raw(client, old_cursor)
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert "重新" in detail and "快照" in detail

    # A still-valid cursor keeps receiving corrections/deletes normally.
    page = changes(client, 3)
    assert [c["seq"] for c in page["changes"]] == [4]

    # Fresh snapshot resets the client to the global consistent point.
    snap = bars(client)
    assert snap["cursor"] == 4
    assert changes(client, snap["cursor"])["changes"] == []

    # New writes after recovery are delivered normally.
    post(client, msg(trade_no="9", t=T0 + 9 * 60_000, price=12000))
    page = changes(client, snap["cursor"])
    assert len(page["changes"]) == 1 and page["changes"][0]["seq"] == 5


def test_reclaim_other_symbol_not_falsely_expired(client):
    feed_n(client, 3, symbol="600519")
    feed_n(client, 2, symbol="000001", start=10)
    # Reclaim only 600519.
    r = reclaim(client, keep=0, symbol="600519")
    assert r.status_code == 200
    assert r.json()["reclaim_watermark"] == 3

    # 000001 cursor 0 must not be reported expired: its watermark is still 0.
    page = changes(client, 0, symbol="000001")
    assert [c["seq"] for c in page["changes"]] == [4, 5]
    # And its own later reclaim is independent.
    r2 = reclaim(client, keep=1, symbol="000001")
    assert r2.json()["deleted_count"] == 1 and r2.json()["reclaim_watermark"] == 4
    # 600519 watermark untouched by the 000001 reclaim.
    r3 = reclaim(client, keep=0, symbol="600519")
    assert r3.json()["deleted_count"] == 0 and r3.json()["reclaim_watermark"] == 3


def test_snapshot_after_full_clear_keeps_global_watermark_and_seq_advances(client):
    feed_n(client, 2)
    reclaim(client, keep=0)

    snap = bars(client)
    assert snap["cursor"] == 2                      # committed global watermark
    assert len(snap["bars"]) == 2                   # bars themselves are not reclaimed

    # New trades keep using ever-increasing seq (no reuse of 1/2).
    post(client, msg(trade_no="7", t=T0 + 7 * 60_000))
    page = changes(client, 2)
    assert [c["seq"] for c in page["changes"]] == [3]
    assert page["next_cursor"] == 3
    assert bars(client)["cursor"] == 3


def test_reclaim_is_idempotent_and_watermark_monotonic(client):
    feed_n(client, 4)
    first = reclaim(client, keep=2).json()
    assert first["deleted_count"] == 2 and first["reclaim_watermark"] == 2

    again = reclaim(client, keep=2).json()
    assert again["deleted_count"] == 0
    assert again["reclaim_watermark"] == 2          # never moves backwards

    # Shrinking the window further advances the mark monotonically.
    more = reclaim(client, keep=1).json()
    assert more["deleted_count"] == 1 and more["reclaim_watermark"] == 3
    # Reclaiming with a larger window must not resurrect anything.
    wider = reclaim(client, keep=10).json()
    assert wider["deleted_count"] == 0 and wider["reclaim_watermark"] == 3
    assert [c["seq"] for c in changes(client, 3)["changes"]] == [4]


def test_reclaim_watermark_survives_restart(tmp_path):
    db_file = str(tmp_path / "feed.db")
    with TestClient(create_app(db_file)) as c1:
        feed_n(c1, 4)
        reclaim(c1, keep=1)                          # watermark 3 for 600519

    # Restart on the same database file: expiry must still be recognised.
    with TestClient(create_app(db_file)) as c2:
        assert changes_raw(c2, 1).status_code == 410
        assert changes_raw(c2, 3).status_code == 200
        snap = bars(c2)
        assert snap["cursor"] == 4                  # global watermark also durable
        # And the feed continues with fresh, increasing seq after restart.
        post(c2, msg(trade_no="8", t=T0 + 8 * 60_000))
        assert [c["seq"] for c in changes(c2, 4)["changes"]] == [5]


def test_failed_reclaim_rolls_back_log_and_watermark(client, monkeypatch):
    feed_n(client, 3)
    snap = bars(client)

    # Inject a connection whose execute fails on the watermark upsert only,
    # simulating a fault inside the reclaim transaction.
    from app import db as db_mod

    real_connect = db_mod.connect
    made = {"n": 0}

    class FailingConn(sqlite3.Connection):
        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("INSERT INTO RECLAIM_WATERMARKS"):
                made["n"] += 1
                raise RuntimeError("simulated reclaim failure")
            return super().execute(sql, params)

    def failing_connect(path):
        # FailingConn needs the same factory setup as the real connect().
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None,
                               factory=FailingConn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    monkeypatch.setattr(db_mod, "connect", failing_connect)
    with TestClient(client.app, raise_server_exceptions=False) as c:
        r = c.post("/v1/symbols/600519/journal/reclaim", json={"keep": 0})
    assert r.status_code == 500
    monkeypatch.undo()

    # Original journal and both watermarks are untouched.
    assert made["n"] == 1
    assert len(changes(client, 0)["changes"]) == 3
    assert bars(client)["cursor"] == snap["cursor"]
    # No watermark row was persisted; normal pulls and a retry both work.
    assert changes_raw(client, 0).status_code == 200
    retry = reclaim(client, keep=0)
    assert retry.status_code == 200
    assert retry.json()["deleted_count"] == 3


def test_pull_inside_open_transaction_sees_consistent_log_and_watermark(tmp_path):
    """Interleaving: a read transaction open across a reclaim keeps its view.

    SQLite WAL snapshot isolation: the pull's BEGIN pins one consistent view
    of journal + watermarks. A reclaim that commits while the pull is open
    must neither hide rows from that read nor make its checks inconsistent.
    """
    from app import db as db_mod

    db_file = str(tmp_path / "feed.db")
    with TestClient(create_app(db_file)) as client:
        feed_n(client, 3)

    # Two separate connections, like two concurrent HTTP requests.
    reader = db_mod.connect(db_file)
    writer = db_mod.connect(db_file)
    try:
        reader.execute("BEGIN")
        # Reader pins its snapshot before the reclaim happens.
        max_before = reader.execute("SELECT COALESCE(MAX(seq),0) FROM journal").fetchone()[0]
        wm_before = logic._symbol_watermark(reader, "600519")
        assert max_before == 3 and wm_before == 0

        # Meanwhile a reclaim fully clears and moves the watermark to 3.
        result = logic.reclaim_journal(writer, "600519", 0)
        assert result["deleted_count"] == 3

        # The reader's pinned view is unchanged: same log, same watermark.
        assert reader.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 3
        assert logic._symbol_watermark(reader, "600519") == 0
        reader.execute("COMMIT")

        # A new read after commit sees the reclaimed state and enforces expiry.
        fresh = db_mod.connect(db_file)
        try:
            assert fresh.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0
            assert logic._symbol_watermark(fresh, "600519") == 3
            with pytest.raises(logic.CursorExpiredError):
                logic.pull_changes(fresh, "600519", 1, 500)
        finally:
            fresh.close()
    finally:
        reader.close()
        writer.close()


def test_reclaim_validation_errors(client):
    r = client.post("/v1/symbols/600519/journal/reclaim", json={"keep": -1})
    assert r.status_code == 422
    r = client.post("/v1/symbols/600519/journal/reclaim", json={})
    assert r.status_code == 422


def test_reclaim_unknown_symbol_is_harmless(client):
    feed_n(client, 1)
    r = reclaim(client, keep=0, symbol="999999")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_count"] == 0 and body["reclaim_watermark"] == 0
    # Existing symbol is unaffected.
    assert len(changes(client, 0)["changes"]) == 1


def test_valid_cursor_still_gets_corrections_and_deletes_after_reclaim(client):
    # Two trades in minute 0, then reclaim everything, keep a valid cursor.
    post(client, msg(trade_no="1", revision=1, t=T_IN_0, price=10000))
    post(client, msg(trade_no="2", revision=1, t=T0 + 30_000, price=10200))
    body = reclaim(client, keep=0).json()
    cursor = body["reclaim_watermark"]

    # Cross-minute correction on trade 1: old minute update + new minute upsert,
    # both delivered to the still-valid cursor.
    post(client, msg(trade_no="1", revision=2, op="correction",
                     t=T_IN_1, price=10000, qty=100))
    page = changes(client, cursor)
    assert [c["type"] for c in page["changes"]] == ["upsert", "upsert"]

    # Cancelling the only remaining trade of minute 1 delivers a delete marker.
    post(client, msg(trade_no="1", revision=3, op="cancel"))
    page = changes(client, page["next_cursor"])
    assert any(c["type"] == "delete" for c in page["changes"])


def test_reclaim_endpoint_present_in_openapi(client):
    schema = client.get("/openapi.json").json()
    assert "/v1/symbols/{symbol}/journal/reclaim" in schema["paths"]
    changes_op = schema["paths"]["/v1/symbols/{symbol}/changes"]["get"]
    assert "410" in changes_op["responses"]
