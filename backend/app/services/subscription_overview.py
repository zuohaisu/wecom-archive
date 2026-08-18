"""Owner-facing subscription state derived from billing authority (RND-378)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Tenant
from app.services.entitlements import SubscriptionSummary, get_subscription_summary

EXPIRING_SOON_WINDOW = timedelta(days=30)
SUBSCRIPTION_DISPLAY_STATES = frozenset(
    {
        "trial",
        "paid_active",
        "expiring_soon",
        "grace",
        "expired",
        "canceled",
        "unavailable",
    }
)
SUBSCRIPTION_UNAVAILABLE_REASONS = frozenset(
    {
        "no_subscription",
        "subscription_not_started",
        "subscription_expired",
        "subscription_canceled",
        "subscription_inactive",
    }
)


@dataclass(frozen=True)
class SubscriptionOverview:
    plan_code: str | None
    plan_name: str | None
    stored_status: str | None
    effective_status: str
    display_state: str
    unavailable_reason: str | None
    is_entitled: bool
    starts_at: datetime | None
    ends_at: datetime | None
    grace_ends_at: datetime | None
    cancel_at_period_end: bool
    entitlements: tuple[str, ...]
    renewal_count: int
    measured_at: datetime
    tenant_lifecycle_status: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def classify_subscription_display(
    summary: SubscriptionSummary | None,
    *,
    at: datetime,
) -> tuple[str, str | None]:
    """Return an explicit UI state and a safe, machine-readable reason."""
    checked_at = _as_utc(at)
    if summary is None:
        return "unavailable", "no_subscription"
    if summary.stored_status == "canceled":
        return "canceled", "subscription_canceled"
    if summary.effective_status == "expired" or summary.stored_status == "expired":
        return "expired", "subscription_expired"
    if checked_at < _as_utc(summary.starts_at):
        return "unavailable", "subscription_not_started"
    if not summary.is_entitled:
        return "unavailable", "subscription_inactive"
    if summary.effective_status == "grace" or summary.stored_status == "grace":
        return "grace", None
    if summary.stored_status == "trial":
        return "trial", None
    if _as_utc(summary.ends_at) <= checked_at + EXPIRING_SOON_WINDOW:
        return "expiring_soon", None
    return "paid_active", None


def get_subscription_overview(
    db: Session,
    tenant_id: str,
    *,
    at: datetime | None = None,
) -> SubscriptionOverview:
    """Read the current tenant subscription; browser input never participates."""
    measured_at = _as_utc(at or datetime.now(timezone.utc))
    summary = get_subscription_summary(db, tenant_id, at=measured_at)
    display_state, unavailable_reason = classify_subscription_display(
        summary,
        at=measured_at,
    )
    # RND-404: Tenant service status (active/frozen/suspended) is a second,
    # independent axis from the Subscription display state above (ADR-0005
    # §2.5/§2.6) — the Owner billing page needs both to render an accurate
    # frozen/suspended banner without conflating the two.
    tenant_lifecycle_status = (
        db.scalar(select(Tenant.lifecycle_status).where(Tenant.id == tenant_id))
        or "active"
    )
    if summary is None:
        return SubscriptionOverview(
            plan_code=None,
            plan_name=None,
            stored_status=None,
            effective_status="not_subscribed",
            display_state=display_state,
            unavailable_reason=unavailable_reason,
            is_entitled=False,
            starts_at=None,
            ends_at=None,
            grace_ends_at=None,
            cancel_at_period_end=False,
            entitlements=(),
            renewal_count=0,
            measured_at=measured_at,
            tenant_lifecycle_status=tenant_lifecycle_status,
        )
    return SubscriptionOverview(
        plan_code=summary.plan_code,
        plan_name=summary.plan_name,
        stored_status=summary.stored_status,
        effective_status=summary.effective_status,
        display_state=display_state,
        unavailable_reason=unavailable_reason,
        is_entitled=summary.is_entitled,
        starts_at=summary.starts_at,
        ends_at=summary.ends_at,
        grace_ends_at=summary.grace_ends_at,
        cancel_at_period_end=summary.cancel_at_period_end,
        entitlements=summary.entitlements,
        renewal_count=summary.renewal_count,
        measured_at=measured_at,
        tenant_lifecycle_status=tenant_lifecycle_status,
    )
