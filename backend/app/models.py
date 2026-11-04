"""Pydantic models: the public contract of the REST API, separate from the database tables."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

OrderStatus = Literal["placed", "shipped", "delivered", "cancelled", "refunded"]


class OrderItem(BaseModel):
    sku: str
    quantity: int
    unit_price_paise: int  # price at time of sale; money stays in integer paise, as in the database


class Order(BaseModel):
    id: int
    customer_id: int
    status: OrderStatus  # Literal: any other value fails validation
    total_paise: int
    placed_at: datetime
    shipped_at: datetime | None  # None until the order ships
    delivered_at: datetime | None  # None until delivered
    items: list[OrderItem]  # models can nest: an order contains its line items
