"""Order business rules, as plain functions: no web or database code, so each rule is easy to test."""

from collections.abc import Mapping
from datetime import datetime, timedelta

from backend.app.models import RefundRequest, RefundStatus
from backend.app.policy import REFUND_POLICY


class PolicyViolation(Exception):
    """A request breaks a business rule. The message is written for the agent to relay."""


def evaluate_refund(order: Mapping, refund: RefundRequest, now: datetime) -> RefundStatus:
    """Check a refund request against the policy.

    Returns "completed" if the refund can go ahead, or "pending_approval" if a human
    must approve it first. Raises PolicyViolation if the policy does not allow it.
    """
    order_id = order["id"]
    if order["status"] != "delivered":
        raise PolicyViolation(
            f"Order {order_id} cannot be refunded because it has not been delivered"
            f" (its status is '{order['status']}')."
        )

    age = now - datetime.fromisoformat(order["delivered_at"])
    window_days = REFUND_POLICY.refund_window_days
    if age > timedelta(days=window_days):
        raise PolicyViolation(
            f"Order {order_id} was delivered {age.days} days ago. Refunds are only allowed"
            f" within {window_days} days of delivery."
        )

    changed_mind_days = REFUND_POLICY.changed_mind_window_days
    if refund.reason == "changed_mind" and age > timedelta(days=changed_mind_days):
        raise PolicyViolation(
            f"Order {order_id} was delivered {age.days} days ago. The reason 'changed_mind' is"
            f" only accepted within {changed_mind_days} days of delivery."
        )

    if refund.amount_paise > order["total_paise"]:
        raise PolicyViolation(
            f"The refund amount ({refund.amount_paise} paise) is more than the order total"
            f" ({order['total_paise']} paise)."
        )

    if refund.amount_paise > REFUND_POLICY.approval_threshold_paise:
        return "pending_approval"
    return "completed"


# Why each status can't be cancelled, and what the customer can do instead.
_NOT_CANCELLABLE = {
    "shipped": "has already shipped, so it can't be cancelled. A refund can be requested after"
    " it is delivered.",
    "delivered": "has already been delivered, so it can't be cancelled. A refund can be"
    " requested instead.",
    "refunded": "has already been refunded, so it can't be cancelled.",
}


def check_cancellable(order: Mapping) -> None:
    """Raise PolicyViolation unless the order can be cancelled: only orders not yet shipped can.

    An order that is already cancelled passes, so a repeated cancel request is harmless.
    """
    status = order["status"]
    if status in ("placed", "cancelled"):
        return
    fallback = f"can't be cancelled (its status is '{status}')."  # for any future status
    raise PolicyViolation(f"Order {order['id']} {_NOT_CANCELLABLE.get(status, fallback)}")
