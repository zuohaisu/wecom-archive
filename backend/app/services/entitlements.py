"""Authoritative subscription and entitlement service for RND-376.

Callers supply a server-resolved tenant id. Plan price, quota and capabilities
are always loaded from the database; no browser-provided commercial fields are
accepted by this boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    BillingPlan,
    PlanEntitlement,
    Subscription,
    SubscriptionHistory,
    Tenant,
)

ANNUAL_PLAN_CODE = "annual_base_cny_99"
ARCHIVE_ACCESS = "archive_access"
UNLIMITED_SEATS = "unlimited_seats"
# RND-259 paid white-label capabilities.  Plans/add-ons grant these through
# the existing normalized plan_entitlements table; no browser claim can grant
# either capability.
CUSTOM_BRANDING = "custom_branding"
CUSTOM_DOMAIN = "custom_domain"
ENTITLED_STATUSES = frozenset({"trial", "active", "grace"})
SUBSCRIPTION_STATUSES = frozenset(
    {"trial", "active", "grace", "expired", "canceled"}
)
SUBSCRIPTION_GRACE_PERIOD = timedelta(days=7)


class SubscriptionAssignmentError(ValueError):
    """Base class for rejected authoritative subscription assignments."""


class TenantNotFoundError(SubscriptionAssignmentError):
    pass


class PlanUnavailableError(SubscriptionAssignmentError):
    pass


@dataclass(frozen=True)
class SubscriptionSummary:
    subscription_id: str
    tenant_id: str
    plan_code: str
    plan_name: str
    stored_status: str
    effective_status: str
    is_entitled: bool
    starts_at: datetime
    ends_at: datetime
    grace_ends_at: datetime
    cancel_at_period_end: bool
    amount_cents: int
    currency: str
    billing_period_months: int
    storage_quota_bytes: int
    entitlements: tuple[str, ...]
    renewal_count: int
    revision: int
    source: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _at(value: datetime | None) -> datetime:
    return _as_utc(value or datetime.now(timezone.utc))


def subscription_grace_ends_at(subscription: Subscription) -> datetime:
    value = subscription.grace_ends_at
    if value is None:  # Defensive compatibility while a deployment is migrating.
        return _as_utc(subscription.ends_at) + SUBSCRIPTION_GRACE_PERIOD
    return _as_utc(value)


def effective_subscription_status(subscription: Subscription, *, at: datetime) -> str:
    """Return the time-effective status from the one authoritative policy."""
    checked_at = _as_utc(at)
    if subscription.status in {"expired", "canceled"}:
        return subscription.status
    if checked_at < _as_utc(subscription.starts_at):
        return subscription.status
    if checked_at >= subscription_grace_ends_at(subscription):
        return "expired"
    if checked_at >= _as_utc(subscription.ends_at):
        return "grace"
    return subscription.status


def _is_effectively_entitled(subscription: Subscription, at: datetime) -> bool:
    return (
        effective_subscription_status(subscription, at=at)
        in {"trial", "active", "grace"}
        and _as_utc(subscription.starts_at) <= at
        < subscription_grace_ends_at(subscription)
    )


def _current(
    db: Session, tenant_id: str
) -> tuple[Subscription, BillingPlan] | None:
    return db.execute(
        select(Subscription, BillingPlan)
        .join(BillingPlan, BillingPlan.id == Subscription.plan_id)
        .where(Subscription.tenant_id == tenant_id)
    ).one_or_none()


def _plan_entitlements(db: Session, plan_id: str) -> tuple[str, ...]:
    return tuple(
        db.scalars(
            select(PlanEntitlement.capability)
            .where(
                PlanEntitlement.plan_id == plan_id,
                PlanEntitlement.is_enabled.is_(True),
            )
            .order_by(PlanEntitlement.capability)
        ).all()
    )


def get_subscription_summary(
    db: Session, tenant_id: str, *, at: datetime | None = None
) -> SubscriptionSummary | None:
    """Return the current server-authoritative subscription summary."""
    row = _current(db, tenant_id)
    if row is None:
        return None
    subscription, plan = row
    checked_at = _at(at)
    entitled = _is_effectively_entitled(subscription, checked_at)
    effective_status = effective_subscription_status(subscription, at=checked_at)
    return SubscriptionSummary(
        subscription_id=subscription.id,
        tenant_id=subscription.tenant_id,
        plan_code=plan.code,
        plan_name=plan.display_name,
        stored_status=subscription.status,
        effective_status=effective_status,
        is_entitled=entitled,
        starts_at=_as_utc(subscription.starts_at),
        ends_at=_as_utc(subscription.ends_at),
        grace_ends_at=subscription_grace_ends_at(subscription),
        cancel_at_period_end=bool(subscription.cancel_at_period_end),
        amount_cents=plan.amount_cents,
        currency=plan.currency,
        billing_period_months=plan.billing_period_months,
        storage_quota_bytes=plan.storage_quota_bytes,
        entitlements=_plan_entitlements(db, plan.id) if entitled else (),
        renewal_count=subscription.renewal_count,
        revision=subscription.revision,
        source=subscription.source,
    )


def get_entitlements(
    db: Session, tenant_id: str, *, at: datetime | None = None
) -> frozenset[str]:
    summary = get_subscription_summary(db, tenant_id, at=at)
    if summary is None or not summary.is_entitled:
        return frozenset()
    return frozenset(summary.entitlements)


def has_entitlement(
    db: Session,
    tenant_id: str,
    capability: str,
    *,
    at: datetime | None = None,
) -> bool:
    return capability in get_entitlements(db, tenant_id, at=at)


def get_storage_quota(
    db: Session, tenant_id: str, *, at: datetime | None = None
) -> int:
    summary = get_subscription_summary(db, tenant_id, at=at)
    if summary is None or not summary.is_entitled:
        return 0
    return summary.storage_quota_bytes


def assign_subscription(
    db: Session,
    *,
    tenant_id: str,
    plan_code: str,
    status: str,
    starts_at: datetime,
    ends_at: datetime,
    source: str,
    renewal_count: int = 0,
    grace_ends_at: datetime | None = None,
    cancel_at_period_end: bool = False,
) -> Subscription:
    """Assign the current subscription and append one immutable snapshot.

    The caller owns the surrounding transaction and commit. Payment idempotency
    and renewal date calculation intentionally belong to RND-384.
    """
    if status not in SUBSCRIPTION_STATUSES:
        raise SubscriptionAssignmentError("unsupported subscription status")
    normalized_starts_at = _as_utc(starts_at)
    normalized_ends_at = _as_utc(ends_at)
    normalized_grace_ends_at = _as_utc(
        grace_ends_at or normalized_ends_at + SUBSCRIPTION_GRACE_PERIOD
    )
    if normalized_starts_at >= normalized_ends_at:
        raise SubscriptionAssignmentError("subscription end must follow start")
    if normalized_ends_at >= normalized_grace_ends_at:
        raise SubscriptionAssignmentError("subscription grace end must follow end")
    normalized_source = source.strip()
    if not normalized_source or len(normalized_source) > 32:
        raise SubscriptionAssignmentError("invalid subscription source")
    if renewal_count < 0:
        raise SubscriptionAssignmentError("renewal_count must be non-negative")
    if db.get(Tenant, tenant_id) is None:
        raise TenantNotFoundError("tenant does not exist")

    plan = db.scalar(
        select(BillingPlan).where(
            BillingPlan.code == plan_code,
            BillingPlan.is_active.is_(True),
        )
    )
    if plan is None:
        raise PlanUnavailableError("plan is unavailable")

    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.tenant_id == tenant_id)
        .with_for_update()
    )
    change_kind = "assigned"
    if subscription is None:
        subscription = Subscription(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            revision=1,
        )
        db.add(subscription)
    else:
        subscription.revision += 1
        change_kind = "reassigned"

    subscription.plan_id = plan.id
    subscription.status = status
    subscription.starts_at = normalized_starts_at
    subscription.ends_at = normalized_ends_at
    subscription.grace_ends_at = normalized_grace_ends_at
    subscription.cancel_at_period_end = bool(cancel_at_period_end)
    subscription.source = normalized_source
    subscription.renewal_count = renewal_count
    db.flush()
    append_subscription_history(db, subscription, change_kind=change_kind)
    return subscription


def append_subscription_history(
    db: Session,
    subscription: Subscription,
    *,
    change_kind: str,
) -> SubscriptionHistory:
    """Append the full current subscription projection at its current revision."""
    if not change_kind or len(change_kind) > 32:
        raise SubscriptionAssignmentError("invalid subscription change kind")
    history = SubscriptionHistory(
        id=str(uuid.uuid4()),
        subscription_id=subscription.id,
        tenant_id=subscription.tenant_id,
        plan_id=subscription.plan_id,
        status=subscription.status,
        starts_at=_as_utc(subscription.starts_at),
        ends_at=_as_utc(subscription.ends_at),
        grace_ends_at=subscription_grace_ends_at(subscription),
        cancel_at_period_end=bool(subscription.cancel_at_period_end),
        source=subscription.source,
        renewal_count=subscription.renewal_count,
        revision=subscription.revision,
        change_kind=change_kind,
    )
    db.add(history)
    db.flush()
    return history
