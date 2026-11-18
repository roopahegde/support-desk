"""Tests for the SQLite schema and the scenario seed data."""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from typing import get_args

import pytest

from backend.app.db import connect, init_db
from backend.app.models import EventType
from backend.app.seed import ORDERS, seed

# A frozen "now": every date in the seed is relative to it, so assertions are exact.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    """A fresh, seeded database per test (tmp_path is a pytest-provided temp folder)."""
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    seed(conn, now=NOW)
    yield conn  # the test runs here
    conn.close()


def test_order_1042_is_the_happy_path(conn):
    order = conn.execute("SELECT * FROM orders WHERE id = 1042").fetchone()
    assert order["status"] == "delivered"
    assert order["total_paise"] == 349_900  # INR 3,499
    delivered = datetime.fromisoformat(order["delivered_at"])
    assert NOW - delivered == timedelta(days=5)


def test_order_1043_is_outside_the_30_day_window(conn):
    order = conn.execute("SELECT * FROM orders WHERE id = 1043").fetchone()
    assert NOW - datetime.fromisoformat(order["delivered_at"]) == timedelta(days=45)


def test_order_1044_is_above_the_approval_threshold(conn):
    order = conn.execute("SELECT * FROM orders WHERE id = 1044").fetchone()
    assert order["total_paise"] > 1_000_000  # more than INR 10,000


def test_order_1045_is_already_refunded(conn):
    order = conn.execute("SELECT * FROM orders WHERE id = 1045").fetchone()
    refund = conn.execute("SELECT * FROM refunds WHERE order_id = 1045").fetchone()
    assert order["status"] == "refunded"
    assert refund["idempotency_key"] == "seed-refund-1045"


def test_kettle_is_out_of_stock(conn):
    product = conn.execute("SELECT * FROM products WHERE sku = 'KTL-500'").fetchone()
    assert product["stock"] == 0


def test_database_rejects_a_second_refund_for_the_same_order(conn):
    # The UNIQUE constraint on refunds.order_id is the last line of defence for "refund once".
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO refunds (order_id, amount_paise, reason, status,"
            " idempotency_key, created_at) VALUES (1045, 100, 'damaged', 'completed',"
            " 'another-key', '2026-10-01T00:00:00+00:00')"
        )


def add_event(conn, event_type="cancelled"):
    """Insert one audit event for order 1048 and return its id."""
    cursor = conn.execute(
        "INSERT INTO order_events (order_id, event_type, from_status, to_status, actor,"
        " occurred_at) VALUES (1048, ?, 'placed', 'cancelled', 'staff:test', ?)",
        (event_type, NOW.isoformat()),
    )
    return cursor.lastrowid


def test_an_order_event_can_be_recorded(conn):
    event_id = add_event(conn)
    event = conn.execute("SELECT * FROM order_events WHERE id = ?", (event_id,)).fetchone()
    assert event["actor"] == "staff:test"
    assert event["on_behalf_of"] is None  # optional columns default to NULL


def test_database_rejects_an_unknown_event_type(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_event(conn, event_type="deleted")


def test_order_events_cannot_be_edited(conn):
    event_id = add_event(conn)
    # The trigger's RAISE(ABORT, ...) reaches Python as an IntegrityError carrying our message.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE order_events SET actor = 'someone-else' WHERE id = ?", (event_id,))


def test_order_events_cannot_be_deleted(conn):
    event_id = add_event(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM order_events WHERE id = ?", (event_id,))


# --- Seeded order history ---


def history(conn, order_id):
    """An order's events in the order they happened."""
    return conn.execute(
        "SELECT * FROM order_events WHERE order_id = ? ORDER BY occurred_at, id", (order_id,)
    ).fetchall()


@pytest.mark.parametrize("order_id", [o["id"] for o in ORDERS])
def test_every_order_history_leads_to_its_current_status(conn, order_id):
    events = history(conn, order_id)
    status = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()[0]
    assert events[0]["event_type"] == "placed" and events[0]["from_status"] is None
    for before, after in pairwise(events):  # each step starts where the last one ended
        assert after["from_status"] == before["to_status"]
    assert events[-1]["to_status"] == status


def test_seeded_history_is_marked_as_coming_from_the_seed(conn):
    rows = conn.execute("SELECT DISTINCT actor, request_id FROM order_events").fetchall()
    assert [tuple(row) for row in rows] == [("system:seed", None)]  # no request caused it


def test_seeded_history_uses_the_seed_dates(conn):
    delivered = history(conn, 1042)[-1]
    assert delivered["event_type"] == "delivered"
    assert datetime.fromisoformat(delivered["occurred_at"]) == NOW - timedelta(days=5)


def test_refunded_order_history_ends_with_its_refund(conn):
    last = history(conn, 1045)[-1]
    refund = conn.execute("SELECT id FROM refunds WHERE order_id = 1045").fetchone()
    assert last["event_type"] == "refund_completed"
    assert json.loads(last["details"]) == {
        "refund_id": refund["id"], "amount_paise": 129_900, "reason": "damaged"
    }


def test_seeding_again_resets_the_history_and_keeps_it_protected(conn):
    before = conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]
    seed(conn, now=NOW)  # a reset must work even though the table rejects DELETE
    assert conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == before
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):  # triggers recreated
        conn.execute("DELETE FROM order_events")


def test_event_types_in_the_code_match_the_database(conn):
    # models.EventType and the CHECK in db.py list the same values. If one changes alone, the
    # API could accept an event the database rejects, or document one that can never occur.
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'order_events'"
    ).fetchone()[0]
    check_list = re.search(r"event_type IN \(([^)]*)\)", table_sql).group(1)
    assert set(re.findall(r"'(\w+)'", check_list)) == set(get_args(EventType))
