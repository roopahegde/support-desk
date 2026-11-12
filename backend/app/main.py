"""REST API for the Support Desk backend.

Run from the repo root:  python -m uvicorn backend.app.main:app --reload
Swagger UI (interactive docs):  http://127.0.0.1:8000/docs
"""

import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.security import APIKeyHeader

from backend.app.audit import record_event
from backend.app.db import connect
from backend.app.errors import ERROR_RESPONSES, ApiError, register_error_handlers
from backend.app.logging_config import configure_logging, request_id_var
from backend.app.models import (
    Order,
    OrderEvent,
    ProductStock,
    Refund,
    RefundPolicy,
    RefundRequest,
)
from backend.app.policy import REFUND_POLICY
from backend.app.rules import PolicyViolation, check_cancellable, evaluate_refund

logger = logging.getLogger(__name__)

# backend/.env, found from this file's location. A bare load_dotenv() searches from a
# directory that depends on how the server was started, and misses this file under --reload.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_FILE)  # existing environment variables win over the file, as in CI
configure_logging()  # console or file, chosen by LOG_TARGET in backend/.env

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def parse_api_keys(value: str) -> dict[str, str]:
    """Turn "actor=key,actor=key" into {key: actor}. Raises ValueError if it is malformed.

    Error messages name the entry's position, never its content: they are logged, and
    the content contains a key.
    """
    keys: dict[str, str] = {}
    entries = [entry.strip() for entry in value.split(",")]
    for position, entry in enumerate(entries, start=1):
        if not entry:
            continue  # tolerate a trailing comma or blank entry
        actor, separator, key = entry.partition("=")  # split at the FIRST "=" only
        if not separator or not actor.strip() or not key.strip():
            raise ValueError(f"entry {position} is not in the form actor=key")
        if key.strip() in keys:
            raise ValueError(f"entry {position} reuses a key that another client already has")
        keys[key.strip()] = actor.strip()
    return keys


def server_misconfigured() -> ApiError:
    """The 500 a client gets when the server's own key setup is broken."""
    # The client only learns it is not at fault. The log, written by the caller, says why.
    return ApiError(
        500,
        "server_misconfigured",
        "The server's API-key check is not configured, so no request can be"
        " authorized. This is a server problem, not a problem with your API key.",
    )


def require_api_key(api_key: Annotated[str | None, Depends(api_key_header)]) -> str:
    """Return the client ("actor") whose key is in the X-API-Key header, or reject the request."""
    try:
        keys = parse_api_keys(os.environ.get("BACKEND_API_KEYS", ""))
    except ValueError as problem:
        logger.error("BACKEND_API_KEYS is invalid (%s): fix it in %s", problem, ENV_FILE)
        raise server_misconfigured() from None
    # No keys at all is a server misconfiguration: fail closed, for every request.
    if not keys:
        logger.error("BACKEND_API_KEYS is not set: add it to %s or the environment", ENV_FILE)
        raise server_misconfigured()
    if api_key is not None:
        # Check every key, without stopping at a match, so response time doesn't hint at which
        # client's key was close. Compare bytes: compare_digest rejects non-ASCII str values.
        matches = [
            actor for key, actor in keys.items()
            if secrets.compare_digest(api_key.encode(), key.encode())
        ]
        if matches:
            return matches[0]  # parse_api_keys guarantees there is at most one
    raise ApiError(401, "unauthorized", "Invalid API key")


# An endpoint parameter of this type receives the caller's actor name, e.g. "agent:support-desk".
# The app-wide dependency below already ran require_api_key; FastAPI reuses that result.
Actor = Annotated[str, Depends(require_api_key)]


def get_on_behalf_of(
    # FastAPI maps x_on_behalf_of to the X-On-Behalf-Of header. Optional: None when not sent.
    x_on_behalf_of: Annotated[str | None, Header(max_length=100)] = None,
) -> str | None:
    """The end customer the caller says it is serving, e.g. "customer:1".

    It is recorded in the audit trail but NOT verified: any caller can send any value.
    Never use it to decide what a caller is allowed to do.
    """
    if x_on_behalf_of is None or not x_on_behalf_of.strip():
        return None  # an empty header means the same as no header
    return x_on_behalf_of.strip()


OnBehalfOf = Annotated[str | None, Depends(get_on_behalf_of)]


app = FastAPI(
    title="Support Desk API",
    dependencies=[Depends(require_api_key)],
    responses=ERROR_RESPONSES,  # document the standard error body on every endpoint
)
register_error_handlers(app)  # every error, including FastAPI's own, uses the same format


@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Give every request an ID and log one line for it: method, path, status, duration.

    The ID is returned in the X-Request-ID header, so a client can quote it in a
    complaint and it can be matched to the log. Headers and bodies are never logged:
    they can hold the API key and customer data.
    """
    request_id_var.set(uuid.uuid4().hex[:12])  # every log line in this request carries it
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # A crash: the error handler logs the stack trace and builds the 500 response.
        logger.error(
            "request method=%s path=%s status=500 duration_ms=%.1f",
            request.method, request.url.path, (time.perf_counter() - start) * 1000,
        )
        raise
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.1f",
        request.method, request.url.path, response.status_code,
        (time.perf_counter() - start) * 1000,
    )
    response.headers["X-Request-ID"] = request_id_var.get()
    return response


def get_db() -> Iterator[sqlite3.Connection]:
    """Give one request its own database connection. Tests override this dependency."""
    conn = connect()
    try:
        yield conn  # the endpoint runs here
    finally:
        conn.close()  # runs after the response is built, on the 404 path too


# An endpoint parameter of this type receives the connection from get_db.
DbConn = Annotated[sqlite3.Connection, Depends(get_db)]


def get_now() -> datetime:
    """The current time in UTC. Tests override this to freeze the clock."""
    return datetime.now(timezone.utc)


Now = Annotated[datetime, Depends(get_now)]


# response_model: FastAPI validates the return value against Order and documents it in Swagger.
@app.get("/orders/{order_id}", response_model=Order)
def get_order(order_id: int, conn: DbConn):  # the int hint makes /orders/abc return a 422
    """Return one order with its line items.

    Responds with 404 if the order does not exist.
    """
    return load_order(conn, order_id)


def load_order(conn: sqlite3.Connection, order_id: int) -> dict:
    """Fetch an order with its line items, or raise a 404."""
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        raise ApiError(404, "not_found", f"Order {order_id} not found")
    items = conn.execute(
        "SELECT sku, quantity, unit_price_paise FROM order_items WHERE order_id = ?",
        (order_id,),
    ).fetchall()
    return {**dict(order), "items": [dict(item) for item in items]}


@app.get("/orders/{order_id}/events", response_model=list[OrderEvent])
def get_order_events(order_id: int, conn: DbConn):
    """Return the order's history, oldest first: every change, who made it, and when.

    Responds with 404 if the order does not exist. on_behalf_of is shown exactly as the
    caller stated it; it is not verified.
    """
    if conn.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone() is None:
        raise ApiError(404, "not_found", f"Order {order_id} not found")
    rows = conn.execute(
        "SELECT * FROM order_events WHERE order_id = ? ORDER BY occurred_at, id",  # id breaks ties
        (order_id,),
    ).fetchall()
    # details is stored as JSON text; return it as a real JSON object, not a string inside a string.
    return [
        {**dict(row), "details": json.loads(row["details"]) if row["details"] else None}
        for row in rows
    ]


# response_model: FastAPI validates the return value against list[Order] and documents it in Swagger.
@app.get("/customers/{customer_id}/orders", response_model=list[Order])
def get_customer_orders(customer_id: int, conn: DbConn):
    """Return all of a customer's orders with their line items, newest first.

    Responds with 404 if the customer does not exist, and with an empty list
    if the customer exists but has no orders.
    """
    customer = conn.execute(
        "SELECT id FROM customers WHERE id = ?",
        (customer_id,),
    ).fetchone()
    if customer is None:
        raise ApiError(404, "not_found", f"Customer {customer_id} not found")
    # One query, no N+1. LEFT JOIN keeps an order that has no items (its item columns are NULL).
    rows = conn.execute(
        """
        SELECT orders.id AS order_id, orders.customer_id, orders.status, orders.total_paise,
               orders.placed_at, orders.shipped_at, orders.delivered_at,
               order_items.sku, order_items.quantity, order_items.unit_price_paise
        FROM orders
        LEFT JOIN order_items ON order_items.order_id = orders.id
        WHERE orders.customer_id = ?
        ORDER BY orders.placed_at DESC, orders.id DESC, order_items.id
        """,
        (customer_id,),
    ).fetchall()

    # The JOIN returns one row per item, so group the rows back into one entry per order.
    orders_by_id = {}
    for row in rows:
        if row["order_id"] not in orders_by_id:
            orders_by_id[row["order_id"]] = {
                "id": row["order_id"],
                "customer_id": row["customer_id"],
                "status": row["status"],
                "total_paise": row["total_paise"],
                "placed_at": row["placed_at"],
                "shipped_at": row["shipped_at"],
                "delivered_at": row["delivered_at"],
                "items": [],
            }
        if row["sku"] is not None:  # None means this order has no items
            orders_by_id[row["order_id"]]["items"].append(
                {
                    "sku": row["sku"],
                    "quantity": row["quantity"],
                    "unit_price_paise": row["unit_price_paise"],
                }
            )
    return list(orders_by_id.values())  # dicts keep insertion order, so newest first is preserved


@app.get("/products/{sku}/stock", response_model=ProductStock)
def get_product_stock(sku: str, conn: DbConn):
    """Return how many units of a product are in stock.

    Responds with 404 if the SKU does not exist. A product that is out of
    stock is not an error: it returns 200 with a stock of 0.
    """
    product = conn.execute(
        "SELECT sku, name, stock FROM products WHERE sku = ?",
        (sku,),
    ).fetchone()
    if product is None:
        raise ApiError(404, "not_found", f"Product {sku} not found")
    return dict(product)


@app.get("/policies/refund", response_model=RefundPolicy)
def get_refund_policy():
    """Return the refund policy that the API applies to refund requests."""
    return REFUND_POLICY


def find_refund_by_key(conn: sqlite3.Connection, idempotency_key: str) -> sqlite3.Row | None:
    """Return the refund created with this idempotency key, if there is one."""
    return conn.execute(
        "SELECT * FROM refunds WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()


def replay_refund(existing: sqlite3.Row, order_id: int, refund: RefundRequest) -> dict:
    """Return the original refund for a retry, or raise a 409 if the key was reused."""
    is_same_request = (
        existing["order_id"] == order_id
        and existing["reason"] == refund.reason
        and existing["amount_paise"] == refund.amount_paise
    )
    if not is_same_request:
        raise ApiError(
            409,
            "idempotency_key_reused",
            "Idempotency key was already used for a different request",
        )
    logger.info("refund_replayed order_id=%s refund_id=%s", order_id, existing["id"])
    return dict(existing)


def already_refunded_error(conn: sqlite3.Connection, order_id: int) -> ApiError:
    """A 409 that says whether the order's existing refund is done or waiting for approval."""
    existing = conn.execute(
        "SELECT status FROM refunds WHERE order_id = ?", (order_id,)
    ).fetchone()
    if existing is not None and existing["status"] == "pending_approval":
        return ApiError(
            409,
            "refund_pending_approval",
            f"Order {order_id} already has a refund waiting for human approval",
        )
    return ApiError(409, "already_refunded", f"Order {order_id} has already been refunded")


def replay_or_conflict(
    conn: sqlite3.Connection, order_id: int, idempotency_key: str, refund: RefundRequest
) -> dict:
    """The order already has a refund. If a racing request with this key created it, this
    is a retry: return that refund. Otherwise raise the 409."""
    existing = find_refund_by_key(conn, idempotency_key)
    if existing is not None:
        return replay_refund(existing, order_id, refund)
    raise already_refunded_error(conn, order_id)


@app.post("/orders/{order_id}/refunds", response_model=Refund, status_code=201)
def create_refund(
    order_id: int,
    refund: RefundRequest,
    # FastAPI maps idempotency_key to the Idempotency-Key header. It is required: missing gets a 422.
    idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
    conn: DbConn,
    now: Now,
    actor: Actor,
    on_behalf_of: OnBehalfOf,
):
    """Refund an order, if the refund policy allows it. Retries with the same key are safe.

    Refunds above the approval threshold are saved as pending_approval and the
    order is not marked refunded until a human approves. A retry with the same
    Idempotency-Key and details returns the original refund and changes nothing.

    Responds with 404 if the order does not exist, 409 if it already has a refund
    or the key was used for a different request, and 422 if the request is invalid
    or the policy does not allow the refund (the message says which rule).
    """
    existing = find_refund_by_key(conn, idempotency_key)
    if existing is not None:
        return replay_refund(existing, order_id, refund)

    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        raise ApiError(404, "not_found", f"Order {order_id} not found")
    # Checked before the rules, so a refunded order gets "already refunded", not "not delivered".
    if conn.execute("SELECT 1 FROM refunds WHERE order_id = ?", (order_id,)).fetchone():
        return replay_or_conflict(conn, order_id, idempotency_key, refund)

    try:
        status = evaluate_refund(dict(order), refund, now)
    except PolicyViolation as error:
        raise ApiError(422, "policy_violation", str(error)) from None

    created_at = now.isoformat(timespec="seconds")
    try:
        with conn:  # one transaction: both writes succeed, or neither does
            cursor = conn.execute(
                "INSERT INTO refunds (order_id, amount_paise, reason, status,"
                " idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, refund.amount_paise, refund.reason, status, idempotency_key, created_at),
            )
            refund_id = cursor.lastrowid
            if status == "completed":  # a pending refund leaves the order as it is
                conn.execute("UPDATE orders SET status = 'refunded' WHERE id = ?", (order_id,))
            # Same transaction: if this insert fails, the refund and status change are undone too.
            # Replays never get here, so a retried request adds no second event.
            record_event(
                conn,
                order_id=order_id,
                event_type="refund_completed" if status == "completed" else "refund_pending_approval",
                from_status=order["status"],
                to_status="refunded" if status == "completed" else order["status"],
                actor=actor,
                on_behalf_of=on_behalf_of,
                occurred_at=now,
                details={
                    "refund_id": refund_id,
                    "amount_paise": refund.amount_paise,
                    "reason": refund.reason,
                },
            )
    except sqlite3.IntegrityError as error:
        if "refunds.order_id" not in str(error) and "refunds.idempotency_key" not in str(error):
            raise  # some other constraint failed: a server bug, not a duplicate refund
        # A UNIQUE rule rejected it. Relying on this, not only the checks above, stops two
        # simultaneous requests from both creating a refund. A racing retry breaks both rules
        # and SQLite names just one, so look the key up instead of trusting that name.
        return replay_or_conflict(conn, order_id, idempotency_key, refund)

    logger.info(
        "refund_created order_id=%s refund_id=%s amount_paise=%s reason=%s status=%s actor=%s",
        order_id, refund_id, refund.amount_paise, refund.reason, status, actor,
    )
    return dict(conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone())


@app.post("/orders/{order_id}/cancel", response_model=Order)
def cancel_order(order_id: int, conn: DbConn, now: Now, actor: Actor, on_behalf_of: OnBehalfOf):
    """Cancel an order that has not shipped yet, and return it.

    Cancelling an order that is already cancelled changes nothing and returns it,
    so retries are safe. Responds with 404 if the order does not exist, and with
    422 if it has shipped, been delivered or been refunded.
    """
    order = load_order(conn, order_id)
    try:
        check_cancellable(order)
    except PolicyViolation as error:
        raise ApiError(422, "policy_violation", str(error)) from None

    with conn:
        # The WHERE clause re-checks the status inside the write itself, so an order that
        # ships between our read and this update is never cancelled.
        cursor = conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'placed'",
            (order_id,),
        )
        # rowcount 0 means it was already cancelled (or just shipped): nothing changed, no event.
        if cursor.rowcount == 1:
            record_event(
                conn,
                order_id=order_id,
                event_type="cancelled",
                from_status="placed",
                to_status="cancelled",
                actor=actor,
                on_behalf_of=on_behalf_of,
                occurred_at=now,
            )
    if cursor.rowcount == 1:  # only log when this request actually changed the order
        logger.info("order_cancelled order_id=%s actor=%s", order_id, actor)

    order = load_order(conn, order_id)
    if order["status"] != "cancelled":  # it changed after we read it, e.g. it just shipped
        try:
            check_cancellable(order)
        except PolicyViolation as error:
            raise ApiError(422, "policy_violation", str(error)) from None
    return order
