"""Atomic, provider-neutral paid subscription activation for RND-384.

Payment adapters call this boundary only after they have established a trusted
successful payment. Provider requests, signatures and order state do not
belong here.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType
from app.db.models import (
    AdminUser,
    AuditLog,
    BillingPlan,
    Subscription,
    SubscriptionActivation,
    Tenant,
)
from app.services.entitlements import (
    PlanUnavailableError,
    SubscriptionAssignmentError,
    TenantNotFoundError,
    assign_subscription,
)

_SAFE_SOURCE = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
_SAFE_PLAN_CODE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
logger = logging.getLogger(__name__)


class SubscriptionActivationError(ValueError):
    """Base class for a rejected paid-subscription activation command."""


class InvalidActivationCommandError(SubscriptionActivationError):
    pass


class IdempotencyConflictError(SubscriptionActivationError):
    pass


@dataclass(frozen=True)
class ActivationCommand:
    tenant_id: str
    plan_code: str
    source: str
    idempotency_key: str
    trusted_at: datetime
    admin_user_id: str | None = None


@dataclass(frozen=True)
class ActivationResult:
    activation_id: str
    subscription_id: str
    tenant_id: str
    plan_code: str
    activation_kind: str
    starts_at: datetime
    ends_at: datetime
    renewal_count: int
    subscription_revision: int
    replayed: bool


@dataclass(frozen=True)
class _NormalizedCommand:
    tenant_id: str
    plan_code: str
    source: str
    key_hash: str
    command_hash: str
    trusted_at: datetime
    admin_user_id: str | None


SessionFactory = Callable[[], Session]


def _as_explicit_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidActivationCommandError("trusted_at must include a timezone")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize(command: ActivationCommand) -> _NormalizedCommand:
    tenant_id = command.tenant_id.strip()
    plan_code = command.plan_code.strip().lower()
    source = command.source.strip().lower()
    key = command.idempotency_key.strip()
    actor = command.admin_user_id.strip() if command.admin_user_id else None
    if not tenant_id or len(tenant_id) > 36:
        raise InvalidActivationCommandError("invalid tenant_id")
    if not _SAFE_PLAN_CODE.fullmatch(plan_code):
        raise InvalidActivationCommandError("invalid plan_code")
    if not _SAFE_SOURCE.fullmatch(source):
        raise InvalidActivationCommandError("invalid source")
    if len(key) < 16 or len(key) > 256:
        raise InvalidActivationCommandError("invalid idempotency key")
    if actor is not None and len(actor) > 36:
        raise InvalidActivationCommandError("invalid admin_user_id")
    trusted_at = _as_explicit_utc(command.trusted_at)
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    canonical = json.dumps(
        {
            "plan_code": plan_code,
            "source": source,
            "tenant_id": tenant_id,
            "trusted_at": trusted_at.isoformat(timespec="microseconds"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _NormalizedCommand(
        tenant_id=tenant_id,
        plan_code=plan_code,
        source=source,
        key_hash=key_hash,
        command_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        trusted_at=trusted_at,
        admin_user_id=actor,
    )


def _add_calendar_months(value: datetime, months: int) -> datetime:
    if months <= 0:
        raise SubscriptionActivationError("billing period must be positive")
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _existing_attempt(db: Session, command: _NormalizedCommand):
    return db.scalar(
        select(SubscriptionActivation).where(
            SubscriptionActivation.source == command.source,
            SubscriptionActivation.idempotency_key_hash == command.key_hash,
        )
    )


def _ensure_attempt(
    session_factory: SessionFactory, command: _NormalizedCommand
) -> str:
    with session_factory() as db:
        if db.get(Tenant, command.tenant_id) is None:
            raise TenantNotFoundError("tenant does not exist")
        existing = _existing_attempt(db, command)
        if existing is not None:
            return existing.id
        attempt = SubscriptionActivation(
            id=str(uuid.uuid4()),
            tenant_id=command.tenant_id,
            plan_code=command.plan_code,
            source=command.source,
            idempotency_key_hash=command.key_hash,
            command_hash=command.command_hash,
            trusted_at=command.trusted_at,
            status="pending",
        )
        attempt_id = attempt.id
        db.add(attempt)
        try:
            db.commit()
            return attempt_id
        except IntegrityError:
            db.rollback()
            winner = _existing_attempt(db, command)
            if winner is None:
                raise
            return winner.id


def _result(attempt: SubscriptionActivation, *, replayed: bool) -> ActivationResult:
    if (
        attempt.subscription_id is None
        or attempt.subscription_revision is None
        or attempt.applied_renewal_count is None
        or attempt.activation_kind is None
        or attempt.applied_starts_at is None
        or attempt.applied_ends_at is None
    ):
        raise SubscriptionActivationError("applied activation result is incomplete")
    return ActivationResult(
        activation_id=attempt.id,
        subscription_id=attempt.subscription_id,
        tenant_id=attempt.tenant_id,
        plan_code=attempt.plan_code,
        activation_kind=attempt.activation_kind,
        starts_at=_stored_utc(attempt.applied_starts_at),
        ends_at=_stored_utc(attempt.applied_ends_at),
        renewal_count=attempt.applied_renewal_count,
        subscription_revision=attempt.subscription_revision,
        replayed=replayed,
    )


def _failure_code(error: Exception) -> str:
    if isinstance(error, PlanUnavailableError):
        return "plan_unavailable"
    if isinstance(error, TenantNotFoundError):
        return "tenant_not_found"
    if isinstance(error, (SubscriptionAssignmentError, SubscriptionActivationError)):
        return "invalid_command"
    return "internal_error"


def _mark_failed(
    session_factory: SessionFactory,
    attempt_id: str,
    command: _NormalizedCommand,
    failure_code: str,
) -> None:
    with session_factory() as db:
        attempt = db.scalar(
            select(SubscriptionActivation)
            .where(SubscriptionActivation.id == attempt_id)
            .with_for_update()
        )
        if (
            attempt is None
            or attempt.command_hash != command.command_hash
            or attempt.status == "applied"
        ):
            return
        attempt.status = "failed"
        attempt.failure_code = failure_code
        db.commit()


def activate_or_renew_subscription(
    session_factory: SessionFactory, command: ActivationCommand
) -> ActivationResult:
    """Apply one trusted paid term exactly once and return its durable result."""
    normalized = _normalize(command)
    attempt_id = _ensure_attempt(session_factory, normalized)
    try:
        with session_factory() as db:
            attempt = db.scalar(
                select(SubscriptionActivation)
                .where(SubscriptionActivation.id == attempt_id)
                .with_for_update()
            )
            if attempt is None:
                raise SubscriptionActivationError("activation attempt disappeared")
            if attempt.command_hash != normalized.command_hash:
                raise IdempotencyConflictError(
                    "idempotency key was already used for another command"
                )
            if attempt.status == "applied":
                return _result(attempt, replayed=True)

            tenant = db.scalar(
                select(Tenant)
                .where(Tenant.id == normalized.tenant_id)
                .with_for_update()
            )
            if tenant is None:
                raise TenantNotFoundError("tenant does not exist")
            plan = db.scalar(
                select(BillingPlan).where(
                    BillingPlan.code == normalized.plan_code,
                    BillingPlan.is_active.is_(True),
                )
            )
            if plan is None:
                raise PlanUnavailableError("plan is unavailable")
            if normalized.admin_user_id is not None:
                actor_exists = db.scalar(
                    select(AdminUser.id).where(
                        AdminUser.id == normalized.admin_user_id,
                        AdminUser.tenant_id == normalized.tenant_id,
                    )
                )
                if actor_exists is None:
                    raise InvalidActivationCommandError(
                        "admin actor does not belong to the tenant"
                    )
            current = db.scalar(
                select(Subscription)
                .where(Subscription.tenant_id == normalized.tenant_id)
                .with_for_update()
            )
            if (
                current is not None
                and current.status == "active"
                and _stored_utc(current.starts_at) <= normalized.trusted_at
                and _stored_utc(current.ends_at) > normalized.trusted_at
            ):
                activation_kind = "renewal"
                starts_at = _stored_utc(current.starts_at)
                ends_at = _add_calendar_months(
                    _stored_utc(current.ends_at),
                    plan.billing_period_months,
                )
                renewal_count = current.renewal_count + 1
            else:
                activation_kind = "activation"
                starts_at = normalized.trusted_at
                ends_at = _add_calendar_months(
                    normalized.trusted_at, plan.billing_period_months
                )
                renewal_count = 0

            subscription = assign_subscription(
                db,
                tenant_id=normalized.tenant_id,
                plan_code=normalized.plan_code,
                status="active",
                starts_at=starts_at,
                ends_at=ends_at,
                source=normalized.source,
                renewal_count=renewal_count,
            )
            action = (
                AuditAction.SUBSCRIPTION_RENEWED
                if activation_kind == "renewal"
                else AuditAction.SUBSCRIPTION_ACTIVATED
            )
            db.add(
                AuditLog(
                    id=str(uuid.uuid4()),
                    tenant_id=normalized.tenant_id,
                    admin_user_id=normalized.admin_user_id,
                    action=action,
                    object_type=AuditObjectType.SUBSCRIPTION,
                    object_id=subscription.id,
                    detail={
                        "activation_kind": activation_kind,
                        "plan_code": normalized.plan_code,
                        "source": normalized.source,
                        "subscription_revision": subscription.revision,
                        "ends_at": ends_at.isoformat(),
                    },
                    created_at=datetime.now(timezone.utc),
                )
            )
            attempt.status = "applied"
            attempt.failure_code = None
            attempt.subscription_id = subscription.id
            attempt.subscription_revision = subscription.revision
            attempt.applied_renewal_count = renewal_count
            attempt.activation_kind = activation_kind
            attempt.applied_starts_at = starts_at
            attempt.applied_ends_at = ends_at
            attempt.applied_at = datetime.now(timezone.utc)
            db.flush()
            result = _result(attempt, replayed=False)
            db.commit()
            return result
    except IdempotencyConflictError:
        raise
    except Exception as error:
        try:
            _mark_failed(
                session_factory,
                attempt_id,
                normalized,
                _failure_code(error),
            )
        except Exception:
            logger.exception(
                "failed to persist subscription activation failure state"
            )
        raise
