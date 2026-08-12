"""Single server-side storage-capacity truth and media-write gate (RND-385)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Tenant
from app.services.entitlements import get_subscription_summary
from app.services.tenant_storage_rollup import upsert_tenant_storage_daily
from app.services.usageservice import sum_downloaded_storage

CAPACITY_STATES = frozenset(
    {"normal", "warning_80", "warning_90", "full", "over_limit", "unavailable"}
)
WRITE_REASONS = frozenset(
    {"allowed", "quota_exceeded", "subscription_inactive", "usage_unavailable"}
)


class StorageCapacityError(RuntimeError):
    pass


class CapacityTenantNotFoundError(StorageCapacityError):
    pass


@dataclass(frozen=True)
class StorageCapacitySnapshot:
    tenant_id: str
    plan_code: str | None
    subscription_status: str
    quota_bytes: int
    used_bytes: int | None
    remaining_bytes: int | None
    utilization_basis_points: int | None
    state: str
    usage_status: str
    can_accept_new_media: bool
    measured_at: datetime


@dataclass(frozen=True)
class StorageWriteDecision:
    allowed: bool
    reason: str
    incoming_bytes: int
    capacity: StorageCapacitySnapshot


def capacity_from_values(
    *,
    tenant_id: str,
    quota_bytes: int,
    used_bytes: int | None,
    measured_at: datetime,
    plan_code: str | None,
    subscription_status: str,
    entitled: bool,
) -> StorageCapacitySnapshot:
    """Classify one measurement without database or floating-point drift."""
    checked_at = measured_at.astimezone(timezone.utc)
    if used_bytes is None or quota_bytes <= 0 or not entitled:
        return StorageCapacitySnapshot(
            tenant_id=tenant_id,
            plan_code=plan_code,
            subscription_status=subscription_status,
            quota_bytes=max(quota_bytes, 0),
            used_bytes=used_bytes,
            remaining_bytes=None if used_bytes is None else max(quota_bytes - used_bytes, 0),
            utilization_basis_points=None,
            state="unavailable",
            usage_status="unavailable" if used_bytes is None else "available",
            can_accept_new_media=False,
            measured_at=checked_at,
        )
    remaining = max(quota_bytes - used_bytes, 0)
    basis_points = used_bytes * 10_000 // quota_bytes
    if used_bytes > quota_bytes:
        state = "over_limit"
    elif used_bytes == quota_bytes:
        state = "full"
    elif used_bytes * 100 >= quota_bytes * 90:
        state = "warning_90"
    elif used_bytes * 100 >= quota_bytes * 80:
        state = "warning_80"
    else:
        state = "normal"
    return StorageCapacitySnapshot(
        tenant_id=tenant_id,
        plan_code=plan_code,
        subscription_status=subscription_status,
        quota_bytes=quota_bytes,
        used_bytes=used_bytes,
        remaining_bytes=remaining,
        utilization_basis_points=basis_points,
        state=state,
        usage_status="available",
        can_accept_new_media=remaining > 0,
        measured_at=checked_at,
    )


def measure_storage_capacity(
    db: Session,
    tenant_id: str,
    *,
    at: datetime | None = None,
    lock_tenant: bool = False,
) -> StorageCapacitySnapshot:
    """Measure live downloaded bytes and refresh today's existing rollup.

    With ``lock_tenant=True`` the caller owns a tenant-scoped serialization
    lock until commit/rollback. The media worker holds that lock from the exact
    payload-size decision through storage publication and DB persistence, so
    concurrent writes cannot each spend the same remaining bytes.
    """
    measured_at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tenant_query = select(Tenant).where(Tenant.id == tenant_id)
    if lock_tenant:
        tenant_query = tenant_query.with_for_update()
    if db.scalar(tenant_query) is None:
        raise CapacityTenantNotFoundError("tenant does not exist")
    subscription = get_subscription_summary(db, tenant_id, at=measured_at)
    entitled = subscription is not None and subscription.is_entitled
    quota_bytes = subscription.storage_quota_bytes if entitled else 0
    used_bytes = sum_downloaded_storage(db, tenant_id)
    upsert_tenant_storage_daily(
        db,
        tenant_id=tenant_id,
        usage_date=measured_at.date(),
        used_bytes=used_bytes,
    )
    db.flush()
    return capacity_from_values(
        tenant_id=tenant_id,
        quota_bytes=quota_bytes,
        used_bytes=used_bytes,
        measured_at=measured_at,
        plan_code=subscription.plan_code if subscription else None,
        subscription_status=(
            subscription.effective_status if subscription else "not_subscribed"
        ),
        entitled=entitled,
    )


def check_storage_write(
    db: Session,
    tenant_id: str,
    incoming_bytes: int,
    *,
    at: datetime | None = None,
) -> StorageWriteDecision:
    """Fail closed against server-measured capacity under a tenant lock."""
    if incoming_bytes <= 0:
        raise StorageCapacityError("incoming bytes must be positive")
    capacity = measure_storage_capacity(
        db,
        tenant_id,
        at=at,
        lock_tenant=True,
    )
    if capacity.usage_status != "available" or capacity.used_bytes is None:
        reason = "usage_unavailable"
    elif capacity.quota_bytes <= 0 or capacity.state == "unavailable":
        reason = "subscription_inactive"
    elif capacity.used_bytes + incoming_bytes > capacity.quota_bytes:
        reason = "quota_exceeded"
    else:
        reason = "allowed"
    return StorageWriteDecision(
        allowed=reason == "allowed",
        reason=reason,
        incoming_bytes=incoming_bytes,
        capacity=capacity,
    )
