"""Seed the Support Desk database with the tutorial's scenario data.

Run from the repo root:  python -m backend.app.seed
Re-running wipes and re-creates the data, so it doubles as a "reset" command.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from backend.app.audit import record_event
from backend.app.db import DEFAULT_DB_PATH, connect, init_db

SEED_ACTOR = "system:seed"  # who the audit trail says made the seeded changes

# (id, name, email)
CUSTOMERS = [
    (1, "Priya Sharma", "priya@example.com"),
    (2, "Arjun Mehta", "arjun@example.com"),
    (3, "Kavya Iyer", "kavya@example.com"),
]

# (sku, name, price_paise, stock)  -- 349_900 paise = INR 3,499
PRODUCTS = [
    ("SPK-200", "Bluetooth Speaker", 349_900, 25),
    ("HPH-310", "Wireless Headphones", 219_900, 12),
    ("LTP-900", "Ultrabook Laptop", 5_499_900, 4),
    ("TSH-101", "Cotton T-Shirt", 129_900, 100),
    ("KTL-500", "Electric Kettle", 189_900, 0),  # out of stock: replacement scenario
    ("BAG-220", "Travel Backpack", 249_900, 30),
]

# Dates are "days ago" relative to the moment you seed, so the scenarios never go stale.
# None = hasn't happened yet. Totals are computed from the items, not typed in by hand.
ORDERS = [
    # Happy path: damaged-item refund (also within the 7-day changed_mind window)
    {"id": 1042, "customer_id": 1, "status": "delivered", "items": [("SPK-200", 1)],
     "placed": 9, "shipped": 7, "delivered": 5},
    # Outside the 30-day refund window
    {"id": 1043, "customer_id": 1, "status": "delivered", "items": [("HPH-310", 1)],
     "placed": 50, "shipped": 48, "delivered": 45},
    # Above INR 10,000: needs human approval
    {"id": 1044, "customer_id": 2, "status": "delivered", "items": [("LTP-900", 1)],
     "placed": 14, "shipped": 12, "delivered": 10},
    # Already refunded (its refund row is created separately below)
    {"id": 1045, "customer_id": 2, "status": "refunded", "items": [("TSH-101", 1)],
     "placed": 18, "shipped": 16, "delivered": 14},
    # Replacement wanted, but the kettle is out of stock
    {"id": 1046, "customer_id": 3, "status": "delivered", "items": [("KTL-500", 1)],
     "placed": 7, "shipped": 5, "delivered": 3},
    # Shipped, not delivered: cannot be refunded yet
    {"id": 1047, "customer_id": 3, "status": "shipped", "items": [("BAG-220", 1)],
     "placed": 3, "shipped": 1, "delivered": None},
    # Placed, not shipped: can be cancelled, not refunded
    {"id": 1048, "customer_id": 1, "status": "placed", "items": [("TSH-101", 2)],
     "placed": 1, "shipped": None, "delivered": None},
    # Delivered 20 days ago: damaged is OK, changed_mind is not (past 7 days)
    {"id": 1049, "customer_id": 2, "status": "delivered", "items": [("BAG-220", 1)],
     "placed": 24, "shipped": 22, "delivered": 20},
]

# Order 1045's existing refund. The fixed idempotency key lets later steps replay
# the exact same request and prove the API returns this refund instead of paying twice.
REFUNDS = [
    {"order_id": 1045, "amount_paise": 129_900, "reason": "damaged", "status": "completed",
     "idempotency_key": "seed-refund-1045", "created": 12},
]


def _days_ago(now: datetime, days: int | None) -> str | None:
    """Turn 'N days ago' into an ISO-8601 UTC string (None stays None -> SQL NULL)."""
    if days is None:
        return None
    return (now - timedelta(days=days)).isoformat(timespec="seconds")


def _record_history(conn: sqlite3.Connection, order: dict, now: datetime) -> None:
    """Write the audit events that led to the order's seeded status, in the order they happened."""
    steps = [
        ("placed", None, "placed", order["placed"]),
        ("shipped", "placed", "shipped", order["shipped"]),
        ("delivered", "shipped", "delivered", order["delivered"]),
    ]
    for event_type, from_status, to_status, days in steps:
        if days is None:
            break  # it hasn't happened yet, so neither has anything after it
        record_event(
            conn, order_id=order["id"], event_type=event_type, from_status=from_status,
            to_status=to_status, actor=SEED_ACTOR, occurred_at=now - timedelta(days=days),
        )


def seed(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    """Wipe all tables and insert the scenario data. Tests pass a fixed `now`."""
    now = now or datetime.now(timezone.utc)
    price = {sku: price_paise for sku, _name, price_paise, _stock in PRODUCTS}

    # The audit table is append-only: its triggers reject DELETE, by design. A reset of test
    # data is the one place where wiping it is right, so drop the table and let init_db
    # recreate it, with its triggers. (Application code never does this.)
    conn.execute("DROP TABLE IF EXISTS order_events")
    init_db(conn)

    with conn:  # one transaction: either everything is inserted, or nothing is
        # Delete children before parents, or the foreign keys would block us.
        for table in ("refunds", "order_items", "orders", "products", "customers"):
            conn.execute(f"DELETE FROM {table}")

        conn.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
        conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", PRODUCTS)

        for o in ORDERS:
            total = sum(price[sku] * qty for sku, qty in o["items"])
            conn.execute(
                "INSERT INTO orders (id, customer_id, status, total_paise,"
                " placed_at, shipped_at, delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (o["id"], o["customer_id"], o["status"], total,
                 _days_ago(now, o["placed"]), _days_ago(now, o["shipped"]),
                 _days_ago(now, o["delivered"])),
            )
            for sku, qty in o["items"]:
                conn.execute(
                    "INSERT INTO order_items (order_id, sku, quantity, unit_price_paise)"
                    " VALUES (?, ?, ?, ?)",
                    (o["id"], sku, qty, price[sku]),
                )
            _record_history(conn, o, now)

        for r in REFUNDS:
            cursor = conn.execute(
                "INSERT INTO refunds (order_id, amount_paise, reason, status,"
                " idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (r["order_id"], r["amount_paise"], r["reason"], r["status"],
                 r["idempotency_key"], _days_ago(now, r["created"])),
            )
            completed = r["status"] == "completed"
            record_event(
                conn, order_id=r["order_id"],
                event_type="refund_completed" if completed else "refund_pending_approval",
                from_status="delivered", to_status="refunded" if completed else "delivered",
                actor=SEED_ACTOR, occurred_at=now - timedelta(days=r["created"]),
                details={"refund_id": cursor.lastrowid, "amount_paise": r["amount_paise"],
                         "reason": r["reason"]},
            )


def main() -> None:
    conn = connect(DEFAULT_DB_PATH)
    init_db(conn)
    seed(conn)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("customers", "products", "orders", "refunds", "order_events")}
    conn.close()
    print(f"Seeded {DEFAULT_DB_PATH}: {counts}")


# Runs only when executed as a script (python -m backend.app.seed), not when imported.
if __name__ == "__main__":
    main()
