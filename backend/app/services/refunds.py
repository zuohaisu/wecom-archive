"""Provider-neutral full-refund authority and exact term-grant reversal.

Provider adapters may persist only trusted facts through this service. A
created/processing refund never changes subscription access. Only a trusted
SUCCESS event can reverse a grant, and ambiguous state fails closed into an
explicit manual-recovery queue.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType
from app.db.models import (
    AuditLog,
    PaymentOrder,
    PlatformAdmin,
    RefundEvent,
    RefundOrder,
    Subscription,
    SubscriptionActivation,
    SubscriptionTermGrant,
)
from app.services.billing_lifecycle import reconcile_tenant_billing_lifecycle
from app.services.entitlements import append_subscription_history
from app.services.payment_provider import TrustedRefundEvent

_SAFE_REASON = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_REFUND_STATES = frozenset({"PROCESSING", "SUCCESS", "CLOSED", "ABNORMAL"})


class RefundError(ValueError):
    pass


class RefundNotFoundError(RefundError):
    pass


class RefundAuthorizationError(RefundError):
    pass


class RefundConflictError(RefundError):
    pass


class RefundReplayConflictError(RefundConflictError):
    pass


@dataclass(frozen=True)
class CreateRefundCommand:
    tenant_id: str
    payment_order_id: str
    idempotency_key: str
    reason_code: str
    approved_by_platform_admin_id: str
    requested_at: datetime


@dataclass(frozen=True)
class RefundSummary:
    refund_id: str
    tenant_id: str
    payment_order_id: str
    term_grant_id: str
    amount_cents: int
    currency: str
    provider: str
    provider_ref: str | None
    provider_refund_id: str | None
    provider_state: str | None
    status: str
    reason_code: str
    failure_code: str | None
    requested_at: datetime
    succeeded_at: datetime | None
    entitlement_reversed_at: datetime | None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _explicit_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RefundError("refund time must include a timezone")
    return value.astimezone(timezone.utc)


def _safe_id(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 36:
        raise RefundError(f"invalid {name}")
    return normalized


def _key_hash(value: str) -> str:
    normalized = value.strip()
    if len(normalized) < 16 or len(normalized) > 256:
        raise RefundError("invalid idempotency key")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _summary(refund: RefundOrder) -> RefundSummary:
    return RefundSummary(
        refund_id=refund.id,
        tenant_id=refund.tenant_id,
        payment_order_id=refund.payment_order_id,
        term_grant_id=refund.term_grant_id,
        amount_cents=refund.amount_cents,
        currency=refund.currency,
        provider=refund.provider,
        provider_ref=refund.provider_ref,
        provider_refund_id=refund.provider_refund_id,
        provider_state=refund.provider_state,
        status=refund.status,
        reason_code=refund.reason_code,
        failure_code=refund.failure_code,
        requested_at=_utc(refund.requested_at),
        succeeded_at=_utc(refund.succeeded_at) if refund.succeeded_at else None,
        entitlement_reversed_at=(
            _utc(refund.entitlement_reversed_at)
            if refund.entitlement_reversed_at
            else None
        ),
    )


def get_refund_summary(
    db: Session,
    tenant_id: str,
    refund_id: str,
) -> RefundSummary:
    refund = db.scalar(
        select(RefundOrder).where(
            RefundOrder.id == _safe_id(refund_id, "refund_id"),
            RefundOrder.tenant_id == _safe_id(tenant_id, "tenant_id"),
        )
    )
    if refund is None:
        raise RefundNotFoundError("refund order does not exist")
    return _summary(refund)


def get_latest_refund_for_tenant(
    db: Session,
    tenant_id: str,
) -> RefundSummary | None:
    """Read-only: the tenant's most recently requested refund, or None.

    RND-404: backs the Owner billing page's read-only refund status display
    (processing/succeeded/abnormal/manual_recovery_required). Owners never
    initiate a refund through this path — only platform admins can, via
    ``app.routers.refunds`` — so this stays a plain scoped read.
    """
    refund = db.scalar(
        select(RefundOrder)
        .where(RefundOrder.tenant_id == _safe_id(tenant_id, "tenant_id"))
        .order_by(RefundOrder.requested_at.desc())
        .limit(1)
    )
    return _summary(refund) if refund is not None else None


def _audit(
    db: Session,
    refund: RefundOrder,
    *,
    action: str,
    at: datetime,
    detail: dict,
) -> None:
    db.add(
        AuditLog(
            id=str(uuid.uuid4()),
            tenant_id=refund.tenant_id,
            admin_user_id=None,
            action=action,
            object_type=AuditObjectType.REFUND,
            object_id=refund.id,
            detail=detail,
            created_at=at,
        )
    )


def ensure_payment_term_grant(
    db: Session,
    order: PaymentOrder,
) -> SubscriptionTermGrant:
    """Materialize at most one exact term grant for a succeeded payment."""
    existing = db.scalar(
        select(SubscriptionTermGrant).where(
            SubscriptionTermGrant.payment_order_id == order.id
        )
    )
    if existing is not None:
        return existing
    if order.status != "succeeded" or order.activation_id is None:
        raise RefundConflictError("payment order has no applied activation")
    activation = db.get(SubscriptionActivation, order.activation_id)
    if (
        activation is None
        or activation.status != "applied"
        or activation.subscription_id is None
        or activation.subscription_revision is None
        or activation.applied_starts_at is None
        or activation.applied_ends_at is None
        or activation.applied_renewal_count is None
        or activation.activation_kind is None
    ):
        raise RefundConflictError("payment activation result is incomplete")
    reversible = (
        activation.prior_subscription_existed is not None
        and activation.applied_grace_ends_at is not None
    )
    applied_grace = (
        _utc(activation.applied_grace_ends_at)
        if activation.applied_grace_ends_at is not None
        else _utc(activation.applied_ends_at) + timedelta(days=7)
    )
    grant = SubscriptionTermGrant(
        id=str(uuid.uuid4()),
        payment_order_id=order.id,
        activation_id=activation.id,
        tenant_id=order.tenant_id,
        subscription_id=activation.subscription_id,
        status="active" if reversible else "manual_recovery_required",
        failure_code=None if reversible else "legacy_snapshot_unavailable",
        activation_kind=activation.activation_kind,
        prior_subscription_existed=activation.prior_subscription_existed,
        prior_plan_id=activation.prior_plan_id,
        prior_status=activation.prior_status,
        prior_starts_at=activation.prior_starts_at,
        prior_ends_at=activation.prior_ends_at,
        prior_grace_ends_at=activation.prior_grace_ends_at,
        prior_cancel_at_period_end=activation.prior_cancel_at_period_end,
        prior_source=activation.prior_source,
        prior_renewal_count=activation.prior_renewal_count,
        prior_revision=activation.prior_revision,
        applied_plan_id=order.plan_id,
        applied_status="active",
        applied_starts_at=_utc(activation.applied_starts_at),
        applied_ends_at=_utc(activation.applied_ends_at),
        applied_grace_ends_at=applied_grace,
        applied_cancel_at_period_end=False,
        applied_source=activation.source,
        applied_renewal_count=activation.applied_renewal_count,
        applied_revision=activation.subscription_revision,
    )
    db.add(grant)
    db.flush()
    return grant


def create_refund_request(
    db: Session,
    command: CreateRefundCommand,
) -> RefundSummary:
    tenant_id = _safe_id(command.tenant_id, "tenant_id")
    payment_order_id = _safe_id(command.payment_order_id, "payment_order_id")
    platform_admin_id = _safe_id(
        command.approved_by_platform_admin_id,
        "approved_by_platform_admin_id",
    )
    requested_at = _explicit_utc(command.requested_at)
    reason_code = command.reason_code.strip().lower()
    if not _SAFE_REASON.fullmatch(reason_code):
        raise RefundError("invalid refund reason code")
    key_hash = _key_hash(command.idempotency_key)

    approver = db.scalar(
        select(PlatformAdmin.id).where(
            PlatformAdmin.id == platform_admin_id,
            PlatformAdmin.status == "active",
        )
    )
    if approver is None:
        raise RefundAuthorizationError("active platform admin approval is required")
    order = db.scalar(
        select(PaymentOrder)
        .where(
            PaymentOrder.id == payment_order_id,
            PaymentOrder.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if order is None:
        raise RefundNotFoundError("payment order does not exist")
    if order.status != "succeeded" or order.provider_transaction_id is None:
        raise RefundConflictError("only a succeeded provider payment is refundable")
    key_owner = db.scalar(
        select(RefundOrder).where(
            RefundOrder.tenant_id == tenant_id,
            RefundOrder.idempotency_key_hash == key_hash,
        )
    )
    if key_owner is not None and key_owner.payment_order_id != order.id:
        raise RefundConflictError(
            "idempotency key was already used for another refund"
        )
    existing = db.scalar(
        select(RefundOrder).where(RefundOrder.payment_order_id == order.id)
    )
    if existing is not None:
        if (
            existing.idempotency_key_hash != key_hash
            or existing.tenant_id != tenant_id
            or existing.reason_code != reason_code
            or existing.approved_by_platform_admin_id != platform_admin_id
        ):
            raise RefundConflictError("payment already has another refund request")
        return _summary(existing)

    grant = ensure_payment_term_grant(db, order)
    manual = grant.status != "active"
    refund = RefundOrder(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        payment_order_id=order.id,
        term_grant_id=grant.id,
        amount_cents=order.amount_cents,
        currency=order.currency,
        provider=order.provider,
        status=("manual_recovery_required" if manual else "created"),
        reason_code=reason_code,
        approved_by_platform_admin_id=platform_admin_id,
        idempotency_key_hash=key_hash,
        failure_code=("term_grant_not_reversible" if manual else None),
        requested_at=requested_at,
    )
    db.add(refund)
    db.flush()
    _audit(
        db,
        refund,
        action=AuditAction.REFUND_REQUESTED,
        at=requested_at,
        detail={
            "payment_order_id": order.id,
            "term_grant_id": grant.id,
            "amount_cents": order.amount_cents,
            "currency": order.currency,
            "reason_code": reason_code,
            "approved_by_platform_admin_id": platform_admin_id,
        },
    )
    if manual:
        _audit(
            db,
            refund,
            action=AuditAction.REFUND_MANUAL_RECOVERY_REQUIRED,
            at=requested_at,
            detail={"failure_code": refund.failure_code},
        )
    db.flush()
    return _summary(refund)


def reserve_refund_provider_ref(
    db: Session,
    tenant_id: str,
    refund_id: str,
    *,
    provider_ref: str,
) -> RefundSummary:
    """Persist one stable out_refund_no before any provider network call."""
    normalized_tenant = _safe_id(tenant_id, "tenant_id")
    normalized_refund = _safe_id(refund_id, "refund_id")
    normalized_provider_ref = provider_ref.strip()
    if not normalized_provider_ref or len(normalized_provider_ref) > 64:
        raise RefundError("invalid provider refund reference")
    refund = db.scalar(
        select(RefundOrder)
        .where(
            RefundOrder.id == normalized_refund,
            RefundOrder.tenant_id == normalized_tenant,
        )
        .with_for_update()
    )
    if refund is None:
        raise RefundNotFoundError("refund order does not exist")
    if refund.provider_ref is not None:
        if refund.provider_ref != normalized_provider_ref:
            raise RefundConflictError("refund already owns another provider reference")
        return _summary(refund)
    if refund.status != "created":
        raise RefundConflictError("refund is not eligible for provider submission")
    refund.provider_ref = normalized_provider_ref
    db.flush()
    return _summary(refund)


def mark_refund_processing(
    db: Session,
    tenant_id: str,
    refund_id: str,
    *,
    provider_ref: str,
    provider_refund_id: str | None = None,
    accepted_at: datetime,
) -> RefundSummary:
    normalized_tenant = _safe_id(tenant_id, "tenant_id")
    normalized_refund = _safe_id(refund_id, "refund_id")
    checked_at = _explicit_utc(accepted_at)
    normalized_provider_ref = provider_ref.strip()
    if not normalized_provider_ref or len(normalized_provider_ref) > 64:
        raise RefundError("invalid provider refund reference")
    normalized_provider_refund_id = (
        provider_refund_id.strip() if provider_refund_id is not None else None
    )
    if normalized_provider_refund_id is not None and (
        not normalized_provider_refund_id or len(normalized_provider_refund_id) > 64
    ):
        raise RefundError("invalid provider refund ID")
    refund = db.scalar(
        select(RefundOrder)
        .where(
            RefundOrder.id == normalized_refund,
            RefundOrder.tenant_id == normalized_tenant,
        )
        .with_for_update()
    )
    if refund is None:
        raise RefundNotFoundError("refund order does not exist")
    if refund.status == "processing" and refund.provider_ref == normalized_provider_ref:
        if (
            normalized_provider_refund_id is not None
            and refund.provider_refund_id not in (None, normalized_provider_refund_id)
        ):
            raise RefundConflictError("refund provider ID changed")
        if refund.provider_refund_id is None:
            refund.provider_refund_id = normalized_provider_refund_id
            db.flush()
        return _summary(refund)
    if refund.status != "created":
        raise RefundConflictError("refund is not eligible for provider submission")
    if refund.provider_ref not in (None, normalized_provider_ref):
        raise RefundConflictError("refund already owns another provider reference")
    refund.provider_ref = normalized_provider_ref
    refund.provider_refund_id = normalized_provider_refund_id
    refund.provider_state = "PROCESSING"
    refund.status = "processing"
    refund.provider_accepted_at = checked_at
    _audit(
        db,
        refund,
        action=AuditAction.REFUND_PROCESSING,
        at=checked_at,
        detail={"provider": refund.provider, "provider_ref": normalized_provider_ref},
    )
    db.flush()
    return _summary(refund)


def _validate_event(
    db: Session, refund: RefundOrder, event: TrustedRefundEvent
) -> datetime:
    occurred_at = _explicit_utc(event.occurred_at)
    payment = db.get(PaymentOrder, refund.payment_order_id)
    if (
        payment is None
        or event.provider != refund.provider
        or event.provider_ref != refund.provider_ref
        or event.provider_order_ref != payment.provider_order_ref
        or event.provider_transaction_id != payment.provider_transaction_id
        or event.total_amount_cents != payment.amount_cents
        or event.state not in _REFUND_STATES
        or event.source not in {"callback", "query"}
        or event.amount_cents != refund.amount_cents
        or event.currency != refund.currency
        or not event.provider_event_id
        or len(event.provider_event_id) > 128
        or not event.provider_refund_id
        or len(event.provider_refund_id) > 64
        or not event.merchant_id
        or len(event.merchant_id) > 32
        or len(event.payload_hash) != 64
    ):
        raise RefundConflictError("trusted refund fact does not match the refund order")
    if refund.provider_refund_id not in (None, event.provider_refund_id):
        raise RefundConflictError("trusted refund provider ID changed")
    if refund.provider_refund_id is None:
        refund.provider_refund_id = event.provider_refund_id
    return occurred_at


def _event_matches(stored: RefundEvent, event: TrustedRefundEvent) -> bool:
    return (
        stored.provider_ref == event.provider_ref
        and (
            stored.provider_refund_id is None
            or stored.provider_refund_id == event.provider_refund_id
        )
        and (
            stored.provider_order_ref is None
            or stored.provider_order_ref == event.provider_order_ref
        )
        and (
            stored.provider_transaction_id is None
            or stored.provider_transaction_id == event.provider_transaction_id
        )
        and stored.state == event.state
        and stored.source == event.source
        and stored.amount_cents == event.amount_cents
        and stored.currency == event.currency
        and stored.payload_hash == event.payload_hash
    )


def _same_time(left: datetime, right: datetime) -> bool:
    return _utc(left) == _utc(right)


def _grant_matches_current(
    grant: SubscriptionTermGrant,
    subscription: Subscription,
) -> bool:
    return (
        grant.status == "active"
        and subscription.id == grant.subscription_id
        and subscription.plan_id == grant.applied_plan_id
        and _same_time(subscription.starts_at, grant.applied_starts_at)
        and _same_time(subscription.ends_at, grant.applied_ends_at)
        and _same_time(subscription.grace_ends_at, grant.applied_grace_ends_at)
        and bool(subscription.cancel_at_period_end)
        == bool(grant.applied_cancel_at_period_end)
        and subscription.source == grant.applied_source
        and subscription.renewal_count == grant.applied_renewal_count
    )


def _prior_snapshot_complete(grant: SubscriptionTermGrant) -> bool:
    if grant.prior_subscription_existed is False:
        return True
    return grant.prior_subscription_existed is True and all(
        value is not None
        for value in (
            grant.prior_plan_id,
            grant.prior_status,
            grant.prior_starts_at,
            grant.prior_ends_at,
            grant.prior_grace_ends_at,
            grant.prior_cancel_at_period_end,
            grant.prior_source,
            grant.prior_renewal_count,
            grant.prior_revision,
        )
    )


def _manual_recovery(
    db: Session,
    refund: RefundOrder,
    grant: SubscriptionTermGrant,
    *,
    failure_code: str,
    at: datetime,
) -> None:
    refund.status = "manual_recovery_required"
    refund.failure_code = failure_code
    refund.provider_state = "SUCCESS"
    refund.succeeded_at = at
    grant.status = "manual_recovery_required"
    grant.failure_code = failure_code
    _audit(
        db,
        refund,
        action=AuditAction.REFUND_MANUAL_RECOVERY_REQUIRED,
        at=at,
        detail={"failure_code": failure_code, "term_grant_id": grant.id},
    )


def _reverse_grant(
    db: Session,
    refund: RefundOrder,
    grant: SubscriptionTermGrant,
    subscription: Subscription,
    *,
    at: datetime,
) -> None:
    if grant.prior_subscription_existed:
        subscription.plan_id = grant.prior_plan_id
        subscription.status = grant.prior_status
        subscription.starts_at = grant.prior_starts_at
        subscription.ends_at = grant.prior_ends_at
        subscription.grace_ends_at = grant.prior_grace_ends_at
        subscription.cancel_at_period_end = grant.prior_cancel_at_period_end
        subscription.source = grant.prior_source
        subscription.renewal_count = grant.prior_renewal_count
    else:
        # No pre-payment subscription existed. Preserve the historical paid
        # interval but cancel its entitlement projection rather than deleting
        # rows referenced by activation/history/payment evidence.
        subscription.status = "canceled"
        subscription.cancel_at_period_end = False
    subscription.revision += 1
    append_subscription_history(db, subscription, change_kind="refund_reversal")
    grant.status = "reversed"
    grant.failure_code = None
    grant.reversed_at = at
    refund.status = "succeeded"
    refund.failure_code = None
    refund.provider_state = "SUCCESS"
    refund.succeeded_at = at
    refund.entitlement_reversed_at = at
    _audit(
        db,
        refund,
        action=AuditAction.REFUND_SUCCEEDED,
        at=at,
        detail={
            "payment_order_id": refund.payment_order_id,
            "term_grant_id": grant.id,
            "subscription_revision": subscription.revision,
        },
    )
    reconcile_tenant_billing_lifecycle(db, refund.tenant_id, at=at)


def apply_trusted_refund_event(
    db: Session,
    refund_id: str,
    event: TrustedRefundEvent,
) -> RefundSummary:
    normalized_refund = _safe_id(refund_id, "refund_id")
    refund = db.scalar(
        select(RefundOrder)
        .where(RefundOrder.id == normalized_refund)
        .with_for_update()
    )
    if refund is None:
        raise RefundNotFoundError("refund order does not exist")
    occurred_at = _validate_event(db, refund, event)
    duplicate = db.scalar(
        select(RefundEvent).where(
            RefundEvent.provider == event.provider,
            RefundEvent.provider_event_id == event.provider_event_id,
        )
    )
    if duplicate is not None:
        if duplicate.refund_order_id != refund.id or not _event_matches(
            duplicate, event
        ):
            raise RefundReplayConflictError(
                "provider refund event ID was replayed with different content"
            )
        return _summary(refund)
    terminal_state = {
        "succeeded": "SUCCESS",
        "closed": "CLOSED",
        "abnormal": "ABNORMAL",
        "manual_recovery_required": "SUCCESS",
    }.get(refund.status)
    if terminal_state is not None and event.state != terminal_state:
        raise RefundConflictError("refund is already terminal")
    db.add(
        RefundEvent(
            id=str(uuid.uuid4()),
            refund_order_id=refund.id,
            provider=event.provider,
            provider_event_id=event.provider_event_id,
            provider_ref=event.provider_ref,
            provider_refund_id=event.provider_refund_id,
            provider_order_ref=event.provider_order_ref,
            provider_transaction_id=event.provider_transaction_id,
            state=event.state,
            source=event.source,
            amount_cents=event.amount_cents,
            currency=event.currency,
            payload_hash=event.payload_hash,
            occurred_at=occurred_at,
        )
    )
    if terminal_state is not None:
        db.flush()
        return _summary(refund)
    refund.provider_state = event.state
    if event.state == "PROCESSING":
        refund.status = "processing"
    elif event.state == "CLOSED":
        refund.status = "closed"
        refund.closed_at = occurred_at
        _audit(
            db,
            refund,
            action=AuditAction.REFUND_CLOSED,
            at=occurred_at,
            detail={"provider_state": event.state},
        )
    elif event.state == "ABNORMAL":
        refund.status = "abnormal"
        refund.failure_code = "provider_abnormal"
        _audit(
            db,
            refund,
            action=AuditAction.REFUND_ABNORMAL,
            at=occurred_at,
            detail={"provider_state": event.state},
        )
    else:
        grant = db.scalar(
            select(SubscriptionTermGrant)
            .where(SubscriptionTermGrant.id == refund.term_grant_id)
            .with_for_update()
        )
        subscription = (
            db.scalar(
                select(Subscription)
                .where(Subscription.id == grant.subscription_id)
                .with_for_update()
            )
            if grant is not None
            else None
        )
        if grant is None or subscription is None:
            raise RefundConflictError("refund term grant disappeared")
        if not _prior_snapshot_complete(grant):
            _manual_recovery(
                db,
                refund,
                grant,
                failure_code="prior_snapshot_incomplete",
                at=occurred_at,
            )
        elif not _grant_matches_current(grant, subscription):
            _manual_recovery(
                db,
                refund,
                grant,
                failure_code="later_or_ambiguous_subscription_change",
                at=occurred_at,
            )
        else:
            _reverse_grant(
                db,
                refund,
                grant,
                subscription,
                at=occurred_at,
            )
    db.flush()
    return _summary(refund)
