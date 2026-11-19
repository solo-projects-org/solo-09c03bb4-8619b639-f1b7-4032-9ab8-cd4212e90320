"""Key-scenario tests for the trade feed API.

Run: python -m pytest tests/ -v
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import logic
from app.main import create_app

# 2023-11-14T22:13:20Z in epoch ms; its minute starts at T0.
T0 = 1_699_999_980_000          # minute 0 start
T_IN_0 = T0 + 20_000            # inside minute 0
T_IN_1 = T0 + 60_000 + 5_000    # inside minute 1
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


def changes(client, cursor, symbol="600519", limit=500):
    r = client.get(f"/v1/symbols/{symbol}/changes", params={"cursor": cursor, "limit": limit})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# bars & ordering
# ---------------------------------------------------------------------------

def test_basic_bars_ohlcv(client):
    post(client, msg(trade_no="1", t=T0 + 1_000, price=10000, qty=100))
    post(client, msg(trade_no="2", t=T0 + 2_000, price=10100, qty=200))
    post(client, msg(trade_no="3", t=T0 + 3_000, price=9900, qty=300))

    snap = bars(client)
    assert snap["cursor"] >= 1
    assert snap["bars"] == [{
        "minute_start_utc_ms": M0,
        "open_cents": 10000, "high_cents": 10100,
        "low_cents": 9900, "close_cents": 9900,
        "volume": 600, "trade_count": 3,
    }]


def test_open_close_tie_break_by_trade_no(client):
    # Same millisecond: trade number decides open/close; numeric compare ("2" < "10").
    post(client, msg(trade_no="10", t=T0 + 5_000, price=10100, qty=10))
    post(client, msg(trade_no="2", t=T0 + 5_000, price=10000, qty=10))
    bar = bars(client)["bars"][0]
    assert bar["open_cents"] == 10000
    assert bar["close_cents"] == 10100


# ---------------------------------------------------------------------------
# revision semantics
# ---------------------------------------------------------------------------

def test_idempotent_redelivery(client):
    assert post(client, msg()).json()["status"] == "applied"
    r = post(client, msg())
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    # No extra journal rows, bar unchanged.
    assert len(changes(client, 0)["changes"]) == 1
    assert bars(client)["bars"][0]["volume"] == 100


def test_same_revision_different_content_rejected(client):
    post(client, msg(price=10000))
    r = post(client, msg(price=10001))
    assert r.status_code == 409
    assert bars(client)["bars"][0]["open_cents"] == 10000
    assert len(changes(client, 0)["changes"]) == 1


def test_stale_revision_does_not_roll_back(client):
    post(client, msg(revision=1, price=10000))
    post(client, msg(revision=2, op="correction", price=10200))
    r = post(client, msg(revision=1, price=10000))
    assert r.status_code == 200
    assert r.json()["status"] == "stale_ignored"
    assert r.json()["current_revision"] == 2
    assert bars(client)["bars"][0]["open_cents"] == 10200


def test_revision_gap_allowed(client):
    post(client, msg(revision=1))
    r = post(client, msg(revision=5, op="correction", price=10500))
    assert r.json()["status"] == "applied"
    assert bars(client)["bars"][0]["open_cents"] == 10500


def test_cancel_unknown_trade_creates_tombstone(client):
    r = post(client, msg(revision=3, op="cancel"))
    assert r.json()["status"] == "applied"
    assert r.json()["affected_minutes"] == []
    # An older trade message arriving afterwards is stale and must not resurrect.
    r = post(client, msg(revision=2, price=10000))
    assert r.json()["status"] == "stale_ignored"
    assert bars(client)["bars"] == []


# ---------------------------------------------------------------------------
# corrections / cancels and the change feed
# ---------------------------------------------------------------------------

def test_correction_same_minute_updates_bar(client):
    post(client, msg(revision=1, price=10000, qty=100))
    snap = bars(client)
    post(client, msg(revision=2, op="correction", price=11000, qty=150))
    page = changes(client, snap["cursor"])
    assert [c["type"] for c in page["changes"]] == ["upsert"]
    bar = page["changes"][0]["bar"]
    assert bar["open_cents"] == 11000 and bar["volume"] == 150


def test_cross_minute_correction_delivers_both_minutes(client):
    post(client, msg(revision=1, t=T_IN_0, price=10000))
    snap = bars(client)
    r = post(client, msg(revision=2, op="correction", t=T_IN_1, price=10100))
    assert r.json()["status"] == "applied"

    page = changes(client, snap["cursor"])
    assert len(page["changes"]) == 2
    by_minute = {c["minute_start_utc_ms"]: c for c in page["changes"]}
    # Old minute lost its only trade -> delete marker; new minute -> upsert.
    assert by_minute[M0]["type"] == "delete" and by_minute[M0]["bar"] is None
    assert by_minute[M1]["type"] == "upsert" and by_minute[M1]["bar"]["open_cents"] == 10100
    assert bars(client)["bars"][0]["minute_start_utc_ms"] == M1


def test_cross_minute_correction_with_remaining_trades(client):
    post(client, msg(trade_no="1", revision=1, t=T_IN_0, price=10000, qty=100))
    post(client, msg(trade_no="2", revision=1, t=T0 + 30_000, price=10200, qty=200))
    snap = bars(client)
    # Move trade 1 into minute 1.
    post(client, msg(trade_no="1", revision=2, op="correction", t=T_IN_1, price=10000, qty=100))

    page = changes(client, snap["cursor"])
    by_minute = {c["minute_start_utc_ms"]: c for c in page["changes"]}
    # Old minute still has trade 2 -> updated upsert, not a delete.
    assert by_minute[M0]["type"] == "upsert"
    assert by_minute[M0]["bar"]["volume"] == 200
    assert by_minute[M0]["bar"]["open_cents"] == 10200
    assert by_minute[M1]["type"] == "upsert"
    assert by_minute[M1]["bar"]["volume"] == 100


def test_cancel_last_trade_returns_delete_marker(client):
    post(client, msg(revision=1))
    snap = bars(client)
    assert len(snap["bars"]) == 1
    post(client, msg(revision=2, op="cancel"))

    page = changes(client, snap["cursor"])
    assert page["changes"] == [{
        "seq": page["changes"][0]["seq"],
        "minute_start_utc_ms": M0,
        "type": "delete",
        "bar": None,
    }]
    assert bars(client)["bars"] == []


def test_cancel_one_of_many_updates_bar(client):
    post(client, msg(trade_no="1", revision=1, price=10000, qty=100))
    post(client, msg(trade_no="2", revision=1, t=T0 + 2_000, price=10100, qty=200))
    snap = bars(client)
    post(client, msg(trade_no="1", revision=2, op="cancel"))

    page = changes(client, snap["cursor"])
    assert [c["type"] for c in page["changes"]] == ["upsert"]
    bar = page["changes"][0]["bar"]
    assert bar["volume"] == 200 and bar["trade_count"] == 1
    assert bar["open_cents"] == 10100  # trade 2 is now first


def test_cancel_redelivery_idempotent(client):
    post(client, msg(revision=1))
    post(client, msg(revision=2, op="cancel"))
    r = post(client, msg(revision=2, op="cancel"))
    assert r.json()["status"] == "duplicate"
    assert len(changes(client, 0)["changes"]) == 2  # upsert + delete, nothing more


# ---------------------------------------------------------------------------
# cursor flow & consistency
# ---------------------------------------------------------------------------

def test_cursor_flow(client):
    post(client, msg(trade_no="1", revision=1))
    snap = bars(client)
    cursor = snap["cursor"]

    page = changes(client, cursor)
    assert page["changes"] == [] and page["next_cursor"] == cursor

    post(client, msg(trade_no="2", revision=1, t=T_IN_1, price=10100, qty=50))
    page = changes(client, cursor)
    assert len(page["changes"]) == 1
    assert page["changes"][0]["bar"]["volume"] == 50
    assert page["next_cursor"] > cursor
    assert changes(client, page["next_cursor"])["changes"] == []


def test_snapshot_cursor_consistent_with_changes(client):
    post(client, msg(trade_no="1", revision=1))
    snap = bars(client)
    # Everything visible in the snapshot is <= cursor: nothing is re-delivered.
    assert changes(client, snap["cursor"])["changes"] == []


def test_cursor_ahead_rejected(client):
    post(client, msg())
    r = client.get("/v1/symbols/600519/changes", params={"cursor": 10_000})
    assert r.status_code == 409


def test_symbols_are_isolated(client):
    post(client, msg(symbol="600519", trade_no="1"))
    post(client, msg(symbol="000001", trade_no="1", price=2000))
    assert len(bars(client, "600519")["bars"]) == 1
    assert bars(client, "000001")["bars"][0]["open_cents"] == 2000
    assert changes(client, 0, "600519")["changes"][0]["bar"]["open_cents"] == 10000


# ---------------------------------------------------------------------------
# durability: restart & failed writes
# ---------------------------------------------------------------------------

def test_restart_keeps_state_and_cursor(tmp_path):
    db_file = str(tmp_path / "feed.db")
    with TestClient(create_app(db_file)) as c1:
        post(c1, msg(trade_no="1", revision=1))
        post(c1, msg(trade_no="2", revision=1, t=T_IN_1, price=10100, qty=50))
        cursor = bars(c1)["cursor"]

    # "Restart": brand-new app object on the same database file.
    with TestClient(create_app(db_file)) as c2:
        snap = bars(c2)
        assert snap["cursor"] == cursor
        assert len(snap["bars"]) == 2
        page = changes(c2, cursor)
        assert page["changes"] == []
        # Feed continues after restart without skipping the cursor.
        post(c2, msg(trade_no="1", revision=2, op="cancel"))
        page = changes(c2, cursor)
        assert [c["type"] for c in page["changes"]] == ["delete"]


def test_failed_write_rolls_back_everything(client, monkeypatch, tmp_path):
    post(client, msg(trade_no="1", revision=1, t=T_IN_0))
    cursor = bars(client)["cursor"]

    def boom(conn, symbol, minute_ms):
        raise RuntimeError("simulated failure mid-transaction")

    monkeypatch.setattr(logic, "recompute_minute", boom)
    with TestClient(client.app, raise_server_exceptions=False) as c:
        r = c.post("/v1/trades", json=msg(trade_no="1", revision=2,
                                          op="correction", t=T_IN_1))
        assert r.status_code == 500

    monkeypatch.undo()
    # Trade state, bars and journal are all untouched: no half update.
    state = client.get("/v1/trades/600519/1").json()
    assert state["revision"] == 1 and state["op"] == "trade"
    assert bars(client)["cursor"] == cursor
    assert changes(client, cursor)["changes"] == []
    # And the same message can be retried successfully afterwards.
    assert post(client, msg(trade_no="1", revision=2, op="correction", t=T_IN_1)).json()["status"] == "applied"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_validation_errors(client):
    assert client.post("/v1/trades", json={"symbol": "600519", "trade_no": "1",
                                           "revision": 1, "op": "trade"}).status_code == 422
    assert client.post("/v1/trades", json=msg(revision=0)).status_code == 422
    assert client.post("/v1/trades", json=msg(price=-5)).status_code == 422
    # cancel needs no payload
    assert post(client, msg(trade_no="9", revision=1, op="cancel")).status_code == 200
