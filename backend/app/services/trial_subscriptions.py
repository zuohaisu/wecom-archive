"""Trusted, once-only free-trial assignments for RND-394.

This service does not decide when an organization is configuration-ready.  Its
caller must pass the server-trusted completion time after the provisioning flow
has established that fact.  The service only accepts an organization that has
both a third-party WeCom authorization binding and an active owner account.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType
from app.db.models import (
    AdminUser,
    AuditLog,
    Subscription,
    SubscriptionHistory,
    Tenant,
    ThirdPartyOrganizationBinding,
)
from app.services.entitlements import ANNUAL_PLAN_CODE, assign_subscription

TRIAL_DURATION = timedelta(days=15)
TRIAL_SOURCE = "self_service_trial"


class TrialSubscriptionError(ValueError):
    """Base class for rejected trusted-trial grants."""


class InvalidTrialCommandError(TrialSubscriptionError):
    pass


class TrialNotEligibleError(TrialSubscriptionError):
    pass


class TrialAlreadyUsedError(TrialSubscriptionError):
    pass


@dataclass(frozen=True)
class TrialGrantCommand:
    tenant_id: str
    trusted_at: datetime


@dataclass(frozen=True)
class TrialGrantResult:
    subscription_id: str
    tenant_id: str
    starts_at: datetime
    ends_at: datetime
    subscription_revision: int
    replayed: bool


def _as_explicit_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTrialCommandError("trusted_at must include a timezone")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def grant_self_service_trial(db: Session, command: TrialGrantCommand) -> TrialGrantResult:
    """Grant one 15-day annual-plan trial after trusted configuration completion.

    The caller owns the transaction.  Locking the tenant makes concurrent
    completion notifications serialize; an active matching trial is returned as
    a replay, while any historical trial prevents a second grant.
    """
    tenant_id = command.tenant_id.strip()
    if not tenant_id or len(tenant_id) > 36:
        raise InvalidTrialCommandError("invalid tenant_id")
    trusted_at = _as_explicit_utc(command.trusted_at)

    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if tenant is None:
        raise TrialNotEligibleError("tenant does not exist")
    if tenant.lifecycle_status not in {"provisioning", "active"}:
        raise TrialNotEligibleError("tenant is not eligible for a trial")

    binding_exists = db.scalar(
        select(ThirdPartyOrganizationBinding.id)
        .where(ThirdPartyOrganizationBinding.tenant_id == tenant_id)
        .with_for_update()
    )
    owner = db.scalar(
        select(AdminUser)
        .where(
            AdminUser.tenant_id == tenant_id,
            AdminUser.role == "owner",
            AdminUser.status == "active",
        )
        .with_for_update()
    )
    if binding_exists is None or owner is None:
        raise TrialNotEligibleError("trusted organization ownership is required")

    current = db.scalar(
        select(Subscription)
        .where(Subscription.tenant_id == tenant_id)
        .with_for_update()
    )
    if (
        current is not None
        and current.status == "trial"
        and current.source == TRIAL_SOURCE
        and _stored_utc(current.starts_at) <= trusted_at < _stored_utc(current.ends_at)
    ):
        return TrialGrantResult(
            subscription_id=current.id,
            tenant_id=tenant_id,
            starts_at=_stored_utc(current.starts_at),
            ends_at=_stored_utc(current.ends_at),
            subscription_revision=current.revision,
            replayed=True,
        )

    already_used = db.scalar(
        select(SubscriptionHistory.id)
        .where(
            SubscriptionHistory.tenant_id == tenant_id,
            SubscriptionHistory.status == "trial",
            SubscriptionHistory.source == TRIAL_SOURCE,
        )
        .limit(1)
    )
    if current is not None or already_used is not None:
        raise TrialAlreadyUsedError("the organization has already used its trial")

    ends_at = trusted_at + TRIAL_DURATION
    subscription = assign_subscription(
        db,
        tenant_id=tenant_id,
        plan_code=ANNUAL_PLAN_CODE,
        status="trial",
        starts_at=trusted_at,
        ends_at=ends_at,
        source=TRIAL_SOURCE,
    )
    db.add(
        AuditLog(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            admin_user_id=owner.id,
            action=AuditAction.SUBSCRIPTION_TRIAL_STARTED,
            object_type=AuditObjectType.SUBSCRIPTION,
            object_id=subscription.id,
            detail={"duration_hours": int(TRIAL_DURATION.total_seconds() // 3600), "plan_code": ANNUAL_PLAN_CODE},
            created_at=datetime.now(timezone.utc),
        )
    )
    db.flush()
    return TrialGrantResult(
        subscription_id=subscription.id,
        tenant_id=tenant_id,
        starts_at=trusted_at,
        ends_at=ends_at,
        subscription_revision=subscription.revision,
        replayed=False,
    )
