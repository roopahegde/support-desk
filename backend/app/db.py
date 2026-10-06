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

CREATE TABLE IF NOT EXISTS order_items (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES orders(id),
    sku              TEXT NOT NULL REFERENCES products(sku),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_paise INTEGER NOT NULL CHECK (unit_price_paise >= 0)  -- price at time of sale
);

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
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with the settings every caller should get."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # rows act like dicts: row["status"]
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite leaves FK checks OFF by default
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables. Safe to run repeatedly thanks to IF NOT EXISTS."""
    conn.executescript(SCHEMA)
