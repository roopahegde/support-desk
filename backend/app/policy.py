"""The refund policy: defined once, published by the API, and enforced by its refund rules."""

from backend.app.models import RefundPolicy

REFUND_POLICY = RefundPolicy(
    refund_window_days=30,
    changed_mind_window_days=7,
    accepted_reasons=["damaged", "wrong_item", "not_as_described", "changed_mind"],
    approval_threshold_paise=1_000_000,  # INR 10,000
    max_refunds_per_order=1,
)
