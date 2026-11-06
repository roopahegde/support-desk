"""Refund business rules, as plain functions: no web or database code, so each rule is easy to test."""

from collections.abc import Mapping
from datetime import datetime, timedelta

from backend.app.models import RefundRequest, RefundStatus
from backend.app.policy import REFUND_POLICY


class RefundRuleError(Exception):
    """A refund request breaks the policy. The message is written for the agent to relay."""


def evaluate_refund(order: Mapping, refund: RefundRequest, now: datetime) -> RefundStatus:
    """Check a refund request against the policy.

    Returns "completed" if the refund can go ahead, or "pending_approval" if a human
    must approve it first. Raises RefundRuleError if the policy does not allow it.
    """
    order_id = order["id"]
    if order["status"] != "delivered":
        raise RefundRuleError(
            f"Order {order_id} cannot be refunded because it has not been delivered"
            f" (its status is '{order['status']}')."
        )

    age = now - datetime.fromisoformat(order["delivered_at"])
    window_days = REFUND_POLICY.refund_window_days
    if age > timedelta(days=window_days):
        raise RefundRuleError(
            f"Order {order_id} was delivered {age.days} days ago. Refunds are only allowed"
            f" within {window_days} days of delivery."
        )

    changed_mind_days = REFUND_POLICY.changed_mind_window_days
    if refund.reason == "changed_mind" and age > timedelta(days=changed_mind_days):
        raise RefundRuleError(
            f"Order {order_id} was delivered {age.days} days ago. The reason 'changed_mind' is"
            f" only accepted within {changed_mind_days} days of delivery."
        )

    if refund.amount_paise > order["total_paise"]:
        raise RefundRuleError(
            f"The refund amount ({refund.amount_paise} paise) is more than the order total"
            f" ({order['total_paise']} paise)."
        )

    if refund.amount_paise > REFUND_POLICY.approval_threshold_paise:
        return "pending_approval"
    return "completed"
