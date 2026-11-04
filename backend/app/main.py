"""REST API for the Support Desk backend.

Run from the repo root:  python -m uvicorn backend.app.main:app --reload
Swagger UI (interactive docs):  http://127.0.0.1:8000/docs
"""

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException

from backend.app.db import connect
from backend.app.models import Order

app = FastAPI(title="Support Desk API")


def get_db() -> Iterator[sqlite3.Connection]:
    """Give one request its own database connection. Tests override this dependency."""
    conn = connect()
    try:
        yield conn  # the endpoint runs here
    finally:
        conn.close()  # runs after the response is built, on the 404 path too


# An endpoint parameter of this type receives the connection from get_db.
DbConn = Annotated[sqlite3.Connection, Depends(get_db)]


# response_model: FastAPI validates the return value against Order and documents it in Swagger.
@app.get("/orders/{order_id}", response_model=Order)
def get_order(order_id: int, conn: DbConn):  # the int hint makes /orders/abc return a 422
    """Return one order with its line items.

    Responds with 404 if the order does not exist.
    """
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    items = conn.execute(
        "SELECT sku, quantity, unit_price_paise FROM order_items WHERE order_id = ?",
        (order_id,),
    ).fetchall()
    return {**dict(order), "items": [dict(item) for item in items]}


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
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")
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
