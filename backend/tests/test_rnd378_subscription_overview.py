from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.services.entitlements import SubscriptionSummary
from app.services.subscription_overview import classify_subscription_display

NOW = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)


def _summary(**changes) -> SubscriptionSummary:
    base = SubscriptionSummary(
        subscription_id="rnd378-sub",
        tenant_id="tenant-a",
        plan_code="annual_base_cny_99",
        plan_name="年度基础套餐",
        stored_status="active",
        effective_status="active",
        is_entitled=True,
        starts_at=NOW - timedelta(days=10),
        ends_at=NOW + timedelta(days=365),
        amount_cents=9900,
        currency="CNY",
        billing_period_months=12,
        storage_quota_bytes=5 * 1024**3,
        entitlements=("archive_access", "unlimited_seats"),
        renewal_count=0,
        revision=1,
        source="wechat_pay",
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("summary", "state", "reason"),
    [
        (None, "unavailable", "no_subscription"),
        (_summary(stored_status="trial"), "trial", None),
        (_summary(), "paid_active", None),
        (_summary(ends_at=NOW + timedelta(days=30)), "expiring_soon", None),
        (
            _summary(
                effective_status="expired",
                is_entitled=False,
                ends_at=NOW - timedelta(seconds=1),
            ),
            "expired",
            "subscription_expired",
        ),
        (
            _summary(
                stored_status="canceled",
                effective_status="canceled",
                is_entitled=False,
            ),
            "canceled",
            "subscription_canceled",
        ),
        (
            _summary(
                stored_status="past_due",
                effective_status="past_due",
                is_entitled=False,
            ),
            "unavailable",
            "payment_overdue",
        ),
        (
            _summary(
                is_entitled=False,
                starts_at=NOW + timedelta(days=1),
            ),
            "unavailable",
            "subscription_not_started",
        ),
    ],
)
def test_customer_subscription_states_are_explicit(summary, state, reason) -> None:
    assert classify_subscription_display(summary, at=NOW) == (state, reason)


def test_expiring_soon_boundary_does_not_use_browser_time() -> None:
    summary = _summary(ends_at=NOW + timedelta(days=30, seconds=1))
    assert classify_subscription_display(summary, at=NOW) == ("paid_active", None)
