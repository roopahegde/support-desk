"""Tests for the SQLite schema and the scenario seed data."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.db import connect, init_db
from backend.app.seed import seed

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
