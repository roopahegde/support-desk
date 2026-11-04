"""Pydantic models: the public contract of the REST API, separate from the database tables."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

OrderStatus = Literal["placed", "shipped", "delivered", "cancelled", "refunded"]
RefundReason = Literal["damaged", "wrong_item", "not_as_described", "changed_mind"]


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


class ProductStock(BaseModel):
    sku: str
    name: str
    stock: int  # 0 is a valid answer: the product exists but is out of stock


class RefundPolicy(BaseModel):
    refund_window_days: int  # counted from delivery
    changed_mind_window_days: int  # the shorter window for the changed_mind reason
    accepted_reasons: list[RefundReason]
    approval_threshold_paise: int  # refunds above this need human approval
    max_refunds_per_order: int
