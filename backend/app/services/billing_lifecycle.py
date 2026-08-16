"""Authoritative Subscription and Tenant service lifecycle transitions.

This module owns time-based billing lifecycle policy. Callers must not recreate
grace/frozen/suspended decisions with local string and timestamp comparisons.
Every mutation is tenant-scoped, row-locked, idempotent, and audit-backed; the
caller owns the surrounding transaction and commit.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType
from app.db.models import AdminUser, AuditLog, PlatformAdmin, Subscription, Tenant
from app.services.entitlements import (
    append_subscription_history,
    effective_subscription_status,
)

_SAFE_REASON_CODE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class BillingLifecycleError(ValueError):
    """Base class for rejected lifecycle commands."""


class BillingLifecycleNotFoundError(BillingLifecycleError):
    pass


class BillingLifecycleAuthorizationError(BillingLifecycleError):
    pass


class BillingLifecycleConflictError(BillingLifecycleError):
    pass


@dataclass(frozen=True)
class BillingLifecycleResult:
    tenant_id: str
    subscription_status: str | None
    tenant_lifecycle_status: str
    subscription_revision: int | None
    tenant_lifecycle_revision: int
    subscription_changed: bool
    tenant_changed: bool


@dataclass(frozen=True)
class TenantServiceTransitionResult:
    tenant_id: str
    lifecycle_status: str
    lifecycle_revision: int
    changed: bool


def _explicit_utc(value: datetime | None) -> datetime:
    candidate = value or datetime.now(timezone.utc)
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise BillingLifecycleError("lifecycle time must include a timezone")
    return candidate.astimezone(timezone.utc)


def _tenant_id(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 36:
        raise BillingLifecycleError("invalid tenant_id")
    return normalized


def _locked_tenant(db: Session, tenant_id: str) -> Tenant:
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    if tenant is None:
        raise BillingLifecycleNotFoundError("tenant does not exist")
    return tenant


def _locked_subscription(db: Session, tenant_id: str) -> Subscription | None:
    return db.scalar(
        select(Subscription)
        .where(Subscription.tenant_id == tenant_id)
        .with_for_update()
    )


def _audit(
    db: Session,
    *,
    tenant_id: str,
    action: str,
    object_type: str,
    object_id: str,
    at: datetime,
    detail: dict,
) -> None:
    db.add(
        AuditLog(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            admin_user_id=None,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail=detail,
            created_at=at,
        )
    )


def _transition_subscription(
    db: Session,
    subscription: Subscription,
    *,
    target_status: str,
    at: datetime,
) -> bool:
    if subscription.status == target_status:
        return False
    if target_status not in {"grace", "expired"}:
        raise BillingLifecycleConflictError("unsupported lifecycle transition")
    previous_status = subscription.status
    subscription.status = target_status
    subscription.revision += 1
    append_subscription_history(
        db,
        subscription,
        change_kind=f"lifecycle_{target_status}",
    )
    _audit(
        db,
        tenant_id=subscription.tenant_id,
        action=(
            AuditAction.SUBSCRIPTION_GRACE_STARTED
            if target_status == "grace"
            else AuditAction.SUBSCRIPTION_EXPIRED
        ),
        object_type=AuditObjectType.SUBSCRIPTION,
        object_id=subscription.id,
        at=at,
        detail={
            "previous_status": previous_status,
            "status": target_status,
            "subscription_revision": subscription.revision,
        },
    )
    return True


def _target_tenant_status(
    tenant: Tenant,
    subscription: Subscription | None,
    *,
    at: datetime,
    provisioning_hint: bool | None = None,
) -> str:
    # An explicit provisioning hint is supplied only by the audited resume
    # command, where the pre-suspension state must be projected again.
    if tenant.lifecycle_status == "suspended" and provisioning_hint is None:
        return "suspended"
    effective = (
        effective_subscription_status(subscription, at=at)
        if subscription is not None
        else None
    )
    if effective in {"trial", "active", "grace"}:
        keep_provisioning = (
            tenant.lifecycle_status == "provisioning"
            if provisioning_hint is None
            else provisioning_hint
        )
        return "provisioning" if keep_provisioning else "active"
    if subscription is None and (
        tenant.lifecycle_status == "provisioning" or provisioning_hint is True
    ):
        return "provisioning"
    return "frozen"


def _apply_projected_tenant_status(
    db: Session,
    tenant: Tenant,
    *,
    target_status: str,
    at: datetime,
) -> bool:
    if tenant.lifecycle_status == target_status:
        return False
    if tenant.lifecycle_status == "suspended":
        return False
    previous_status = tenant.lifecycle_status
    tenant.lifecycle_status = target_status
    tenant.is_active = target_status == "active"
    tenant.lifecycle_revision += 1
    if target_status == "frozen":
        tenant.frozen_at = tenant.frozen_at or at
        action = AuditAction.TENANT_BILLING_FROZEN
    elif target_status == "active":
        tenant.frozen_at = None
        action = AuditAction.TENANT_BILLING_RESTORED
    else:
        raise BillingLifecycleConflictError("unsupported projected tenant status")
    _audit(
        db,
        tenant_id=tenant.id,
        action=action,
        object_type=AuditObjectType.TENANT,
        object_id=tenant.id,
        at=at,
        detail={
            "previous_status": previous_status,
            "lifecycle_status": target_status,
            "lifecycle_revision": tenant.lifecycle_revision,
        },
    )
    return True


def reconcile_tenant_billing_lifecycle(
    db: Session,
    tenant_id: str,
    *,
    at: datetime | None = None,
) -> BillingLifecycleResult:
    """Persist the effective Subscription and Tenant service projection."""
    normalized_tenant_id = _tenant_id(tenant_id)
    checked_at = _explicit_utc(at)
    tenant = _locked_tenant(db, normalized_tenant_id)
    subscription = _locked_subscription(db, normalized_tenant_id)
    subscription_changed = False
    if subscription is not None and subscription.status != "canceled":
        effective = effective_subscription_status(subscription, at=checked_at)
        if effective in {"grace", "expired"}:
            subscription_changed = _transition_subscription(
                db,
                subscription,
                target_status=effective,
                at=checked_at,
            )

    target_tenant_status = _target_tenant_status(
        tenant,
        subscription,
        at=checked_at,
    )
    tenant_changed = _apply_projected_tenant_status(
        db,
        tenant,
        target_status=target_tenant_status,
        at=checked_at,
    )
    db.flush()
    return BillingLifecycleResult(
        tenant_id=tenant.id,
        subscription_status=subscription.status if subscription is not None else None,
        tenant_lifecycle_status=tenant.lifecycle_status,
        subscription_revision=(
            subscription.revision if subscription is not None else None
        ),
        tenant_lifecycle_revision=tenant.lifecycle_revision,
        subscription_changed=subscription_changed,
        tenant_changed=tenant_changed,
    )


def restore_tenant_after_paid_subscription(
    db: Session,
    tenant: Tenant,
    subscription: Subscription,
    *,
    at: datetime,
) -> bool:
    """Clear only a billing freeze; a manual suspension always wins."""
    checked_at = _explicit_utc(at)
    if tenant.lifecycle_status in {"suspended", "provisioning", "active"}:
        return False
    target_status = _target_tenant_status(tenant, subscription, at=checked_at)
    changed = _apply_projected_tenant_status(
        db,
        tenant,
        target_status=target_status,
        at=checked_at,
    )
    db.flush()
    return changed


def set_cancel_at_period_end(
    db: Session,
    tenant_id: str,
    *,
    enabled: bool,
    owner_admin_user_id: str,
    at: datetime | None = None,
) -> BillingLifecycleResult:
    """Record a renewal intent only; never shorten service or move money."""
    normalized_tenant_id = _tenant_id(tenant_id)
    checked_at = _explicit_utc(at)
    tenant = _locked_tenant(db, normalized_tenant_id)
    owner = db.scalar(
        select(AdminUser.id).where(
            AdminUser.id == owner_admin_user_id,
            AdminUser.tenant_id == normalized_tenant_id,
            AdminUser.role == "owner",
            AdminUser.status == "active",
        )
    )
    if owner is None:
        raise BillingLifecycleAuthorizationError("active tenant owner is required")
    subscription = _locked_subscription(db, normalized_tenant_id)
    if subscription is None:
        raise BillingLifecycleNotFoundError("subscription does not exist")
    changed = bool(subscription.cancel_at_period_end) != enabled
    if changed:
        subscription.cancel_at_period_end = enabled
        subscription.revision += 1
        append_subscription_history(
            db,
            subscription,
            change_kind=("cancel_intent_set" if enabled else "cancel_intent_cleared"),
        )
        _audit(
            db,
            tenant_id=tenant.id,
            action=AuditAction.SUBSCRIPTION_CANCEL_INTENT_CHANGED,
            object_type=AuditObjectType.SUBSCRIPTION,
            object_id=subscription.id,
            at=checked_at,
            detail={
                "cancel_at_period_end": enabled,
                "owner_admin_user_id": owner_admin_user_id,
                "subscription_revision": subscription.revision,
            },
        )
        db.flush()
    return BillingLifecycleResult(
        tenant_id=tenant.id,
        subscription_status=subscription.status,
        tenant_lifecycle_status=tenant.lifecycle_status,
        subscription_revision=subscription.revision,
        tenant_lifecycle_revision=tenant.lifecycle_revision,
        subscription_changed=changed,
        tenant_changed=False,
    )


def _platform_admin_exists(db: Session, platform_admin_id: str) -> bool:
    return (
        db.scalar(
            select(PlatformAdmin.id).where(
                PlatformAdmin.id == platform_admin_id,
                PlatformAdmin.status == "active",
            )
        )
        is not None
    )


def suspend_tenant_service(
    db: Session,
    tenant_id: str,
    *,
    platform_admin_id: str,
    reason_code: str,
    at: datetime | None = None,
) -> TenantServiceTransitionResult:
    normalized_tenant_id = _tenant_id(tenant_id)
    checked_at = _explicit_utc(at)
    normalized_reason = reason_code.strip().lower()
    if not _SAFE_REASON_CODE.fullmatch(normalized_reason):
        raise BillingLifecycleError("invalid suspension reason code")
    tenant = _locked_tenant(db, normalized_tenant_id)
    if not _platform_admin_exists(db, platform_admin_id):
        raise BillingLifecycleAuthorizationError("active platform admin is required")
    if tenant.lifecycle_status == "suspended":
        if (
            tenant.suspended_by_platform_admin_id == platform_admin_id
            and tenant.suspension_reason == normalized_reason
        ):
            return TenantServiceTransitionResult(
                tenant_id=tenant.id,
                lifecycle_status=tenant.lifecycle_status,
                lifecycle_revision=tenant.lifecycle_revision,
                changed=False,
            )
        raise BillingLifecycleConflictError("tenant is already suspended")

    previous_status = tenant.lifecycle_status
    tenant.lifecycle_status = "suspended"
    tenant.is_active = False
    tenant.lifecycle_revision += 1
    tenant.suspended_at = checked_at
    tenant.suspension_reason = normalized_reason
    tenant.suspended_by_platform_admin_id = platform_admin_id
    tenant.suspension_previous_status = previous_status
    _audit(
        db,
        tenant_id=tenant.id,
        action=AuditAction.PLATFORM_TENANT_SUSPENDED,
        object_type=AuditObjectType.TENANT,
        object_id=tenant.id,
        at=checked_at,
        detail={
            "platform_admin_id": platform_admin_id,
            "reason_code": normalized_reason,
            "previous_status": previous_status,
            "lifecycle_revision": tenant.lifecycle_revision,
        },
    )
    db.flush()
    return TenantServiceTransitionResult(
        tenant_id=tenant.id,
        lifecycle_status=tenant.lifecycle_status,
        lifecycle_revision=tenant.lifecycle_revision,
        changed=True,
    )


def resume_tenant_service(
    db: Session,
    tenant_id: str,
    *,
    platform_admin_id: str,
    at: datetime | None = None,
) -> TenantServiceTransitionResult:
    normalized_tenant_id = _tenant_id(tenant_id)
    checked_at = _explicit_utc(at)
    tenant = _locked_tenant(db, normalized_tenant_id)
    if not _platform_admin_exists(db, platform_admin_id):
        raise BillingLifecycleAuthorizationError("active platform admin is required")
    if tenant.lifecycle_status != "suspended":
        return TenantServiceTransitionResult(
            tenant_id=tenant.id,
            lifecycle_status=tenant.lifecycle_status,
            lifecycle_revision=tenant.lifecycle_revision,
            changed=False,
        )

    subscription = _locked_subscription(db, normalized_tenant_id)
    previous_service_status = tenant.suspension_previous_status or "active"
    target_status = _target_tenant_status(
        tenant,
        subscription,
        at=checked_at,
        provisioning_hint=previous_service_status == "provisioning",
    )

    tenant.lifecycle_status = target_status
    tenant.is_active = target_status == "active"
    tenant.lifecycle_revision += 1
    if target_status == "active":
        tenant.frozen_at = None
    elif target_status == "frozen":
        tenant.frozen_at = tenant.frozen_at or checked_at
    prior_reason = tenant.suspension_reason
    tenant.suspended_at = None
    tenant.suspension_reason = None
    tenant.suspended_by_platform_admin_id = None
    tenant.suspension_previous_status = None
    _audit(
        db,
        tenant_id=tenant.id,
        action=AuditAction.PLATFORM_TENANT_RESUMED,
        object_type=AuditObjectType.TENANT,
        object_id=tenant.id,
        at=checked_at,
        detail={
            "platform_admin_id": platform_admin_id,
            "prior_reason_code": prior_reason,
            "lifecycle_status": target_status,
            "lifecycle_revision": tenant.lifecycle_revision,
        },
    )
    db.flush()
    return TenantServiceTransitionResult(
        tenant_id=tenant.id,
        lifecycle_status=tenant.lifecycle_status,
        lifecycle_revision=tenant.lifecycle_revision,
        changed=True,
    )
