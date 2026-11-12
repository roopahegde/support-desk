"""The audit trail: each change to an order is recorded as one row in order_events."""

import json
import sqlite3
from datetime import datetime

from backend.app.logging_config import request_id_var
from backend.app.models import EventType, OrderStatus


def record_event(
    conn: sqlite3.Connection,
    *,  # everything after this must be passed by name, so from/to can never be swapped by accident
    order_id: int,
    event_type: EventType,
    from_status: OrderStatus | None,
    to_status: OrderStatus,
    actor: str,
    occurred_at: datetime,
    on_behalf_of: str | None = None,
    details: dict | None = None,
) -> None:
    """Add one event to the order's history.

    Call it INSIDE the same `with conn:` block as the change it records: then the change
    and its event are committed together, or rolled back together, never one without the other.
    """
    request_id = request_id_var.get()
    conn.execute(
        "INSERT INTO order_events (order_id, event_type, from_status, to_status, actor,"
        " on_behalf_of, request_id, details, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            order_id, event_type, from_status, to_status, actor, on_behalf_of,
            None if request_id == "-" else request_id,  # "-" means outside a request (the seed)
            json.dumps(details) if details else None,
            occurred_at.isoformat(timespec="seconds"),
        ),
    )
