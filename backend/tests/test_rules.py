"""Unit tests for the refund rules: plain function calls, no API and no database."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models import RefundRequest
from backend.app.rules import PolicyViolation, evaluate_refund

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def order(status="delivered", delivered_ago=timedelta(days=5), total_paise=349_900):
    """A delivered INR 3,499 order by default; override one field per test."""
    return {
        "id": 1042,
        "status": status,
        "delivered_at": (NOW - delivered_ago).isoformat() if delivered_ago is not None else None,
        "total_paise": total_paise,
    }


def refund(reason="damaged", amount_paise=349_900):
    return RefundRequest(reason=reason, amount_paise=amount_paise)


def test_eligible_refund_is_completed():
    assert evaluate_refund(order(), refund(), NOW) == "completed"


@pytest.mark.parametrize("status", ["placed", "shipped", "cancelled", "refunded"])
def test_order_that_is_not_delivered_cannot_be_refunded(status):
    with pytest.raises(PolicyViolation, match=f"has not been delivered \\(its status is '{status}'\\)"):
        evaluate_refund(order(status=status, delivered_ago=None), refund(), NOW)


def test_refund_on_the_last_day_of_the_window_is_allowed():
    assert evaluate_refund(order(delivered_ago=timedelta(days=30)), refund(), NOW) == "completed"


def test_refund_just_after_the_window_is_rejected():
    late = order(delivered_ago=timedelta(days=30, seconds=1))
    with pytest.raises(PolicyViolation, match="only allowed within 30 days of delivery"):
        evaluate_refund(late, refund(), NOW)


def test_order_delivered_45_days_ago_is_rejected_with_its_age():
    with pytest.raises(PolicyViolation, match="was delivered 45 days ago"):
        evaluate_refund(order(delivered_ago=timedelta(days=45)), refund(), NOW)


def test_changed_mind_is_allowed_within_7_days():
    within = order(delivered_ago=timedelta(days=7))
    assert evaluate_refund(within, refund(reason="changed_mind"), NOW) == "completed"


def test_changed_mind_just_after_7_days_is_rejected():
    late = order(delivered_ago=timedelta(days=7, seconds=1))
    with pytest.raises(PolicyViolation, match="'changed_mind' is only accepted within 7 days"):
        evaluate_refund(late, refund(reason="changed_mind"), NOW)


@pytest.mark.parametrize("reason", ["damaged", "wrong_item", "not_as_described"])
def test_other_reasons_keep_the_full_30_day_window(reason):
    assert evaluate_refund(order(delivered_ago=timedelta(days=20)), refund(reason=reason), NOW) == (
        "completed"
    )


def test_refund_of_the_full_order_total_is_allowed():
    assert evaluate_refund(order(), refund(amount_paise=349_900), NOW) == "completed"


def test_refund_above_the_order_total_is_rejected():
    with pytest.raises(PolicyViolation, match="more than the order total"):
        evaluate_refund(order(), refund(amount_paise=349_901), NOW)


def test_refund_of_exactly_10000_rupees_does_not_need_approval():
    big_order = order(total_paise=5_000_000)
    assert evaluate_refund(big_order, refund(amount_paise=1_000_000), NOW) == "completed"


def test_refund_above_10000_rupees_needs_approval():
    big_order = order(total_paise=5_000_000)
    assert evaluate_refund(big_order, refund(amount_paise=1_000_001), NOW) == "pending_approval"


def test_window_is_checked_before_the_approval_threshold():
    # An ineligible refund must be refused, not sent to a human for approval.
    old_big_order = order(delivered_ago=timedelta(days=45), total_paise=5_000_000)
    with pytest.raises(PolicyViolation):
        evaluate_refund(old_big_order, refund(amount_paise=5_000_000), NOW)
