"""SQLite schema and connection helpers for the Support Desk backend.

Conventions:
- Money is stored as INTEGER paise (1 INR = 100 paise), never as floats.
- Timestamps are ISO-8601 text in UTC, e.g. "2026-10-05T09:30:00+00:00".
"""

import sqlite3
from pathlib import Path

# backend/support_desk.db  (__file__ is this file: backend/app/db.py)
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "support_desk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS products (
    sku         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price_paise INTEGER NOT NULL CHECK (price_paise >= 0),
    stock       INTEGER NOT NULL CHECK (stock >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    status       TEXT NOT NULL
                 CHECK (status IN ('placed', 'shipped', 'delivered', 'cancelled', 'refunded')),
    total_paise  INTEGER NOT NULL CHECK (total_paise >= 0),
    placed_at    TEXT NOT NULL,
    shipped_at   TEXT,      -- NULL until the order ships
    delivered_at TEXT       -- NULL until delivered; the refund window counts from here
);

-- Find a customer's orders without reading the whole table.
CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id);

CREATE TABLE IF NOT EXISTS order_items (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES orders(id),
    sku              TEXT NOT NULL REFERENCES products(sku),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_paise INTEGER NOT NULL CHECK (unit_price_paise >= 0)  -- price at time of sale
);

-- Find an order's items without reading the whole table.
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);

CREATE TABLE IF NOT EXISTS refunds (
    id              INTEGER PRIMARY KEY,
    order_id        INTEGER NOT NULL UNIQUE REFERENCES orders(id),  -- UNIQUE = refund once
    amount_paise    INTEGER NOT NULL CHECK (amount_paise > 0),
    reason          TEXT NOT NULL
                    CHECK (reason IN ('damaged', 'wrong_item', 'not_as_described', 'changed_mind')),
    status          TEXT NOT NULL CHECK (status IN ('completed', 'pending_approval')),
    idempotency_key TEXT NOT NULL UNIQUE,  -- a retried request can never create a 2nd refund
    created_at      TEXT NOT NULL
);

-- The audit trail: one row per change to an order, written in the same transaction as the
-- change itself. Rows are only ever added, never edited or removed (see the triggers below).
CREATE TABLE IF NOT EXISTS order_events (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL REFERENCES orders(id),
    event_type   TEXT NOT NULL CHECK (event_type IN ('placed', 'shipped', 'delivered',
                     'cancelled', 'refund_completed', 'refund_pending_approval')),
    from_status  TEXT,           -- the order's status before; NULL for 'placed'
    to_status    TEXT NOT NULL,  -- after; the same as before for a pending refund
    actor        TEXT NOT NULL,  -- who made the change, from their API key: 'agent:support-desk'
    on_behalf_of TEXT,           -- the end customer they said they served; NOT verified
    request_id   TEXT,           -- matches req=... in the log; NULL for seeded history
    details      TEXT,           -- JSON with event-specific facts, e.g. refund id and amount
    occurred_at  TEXT NOT NULL
);

-- Show an order's history without reading the whole table.
CREATE INDEX IF NOT EXISTS idx_order_events_order_id ON order_events(order_id);

-- Append-only, enforced by the database: no code path, bug or SQL console can rewrite history.
CREATE TRIGGER IF NOT EXISTS order_events_no_update BEFORE UPDATE ON order_events
BEGIN SELECT RAISE(ABORT, 'order_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS order_events_no_delete BEFORE DELETE ON order_events
BEGIN SELECT RAISE(ABORT, 'order_events is append-only'); END;
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with the settings every caller should get."""
    # FastAPI may run a request's dependency and endpoint on different threads. That is safe
    # here because each request has its own connection, used by one thread at a time.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # rows act like dicts: row["status"]
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite leaves FK checks OFF by default
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables. Safe to run repeatedly thanks to IF NOT EXISTS."""
    conn.executescript(SCHEMA)
