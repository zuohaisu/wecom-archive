"""Provider-neutral payment-order orchestration for RND-380."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    BillingPlan,
    PaymentEvent,
    PaymentOrder,
    SubscriptionActivation,
    Tenant,
)
from app.services.payment_provider import (
    PaymentProvider,
    PaymentRequest,
    TrustedPaymentEvent,
)
from app.services.refunds import ensure_payment_term_grant
from app.services.subscription_activation import (
    ActivationCommand,
    activate_or_renew_subscription,
)

ORDER_TTL_MINUTES = 15
_SAFE_PLAN_CODE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class PaymentOrderError(RuntimeError):
    pass


class InvalidPaymentOrderError(PaymentOrderError):
    pass


class PaymentOrderNotFoundError(PaymentOrderError):
    pass


class PaymentOrderConflictError(PaymentOrderError):
    pass


class PaymentReplayConflictError(PaymentOrderConflictError):
    pass


class PaymentActivationPendingError(PaymentOrderError):
    pass


@dataclass(frozen=True)
class CreateOrderCommand:
    tenant_id: str
    plan_code: str
    idempotency_key: str
    now: datetime


@dataclass(frozen=True)
class PaymentOrderSummary:
    order_id: str
    plan_code: str
    plan_name: str
    amount_cents: int
    currency: str
    provider: str
    status: str
    provider_state: str | None
    created_at: datetime
    expires_at: datetime
    paid_at: datetime | None
    activated_at: datetime | None
    subscription_ends_at: datetime | None
    failure_code: str | None
    qr_available: bool


SessionFactory = Callable[[], Session]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _explicit_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidPaymentOrderError("now must include a timezone")
    return value.astimezone(timezone.utc)


def _key_hash(raw: str) -> str:
    normalized = raw.strip()
    if len(normalized) < 16 or len(normalized) > 256:
        raise InvalidPaymentOrderError("invalid idempotency key")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_plan_code(raw: str) -> str:
    normalized = raw.strip().lower()
    if not _SAFE_PLAN_CODE.fullmatch(normalized):
        raise InvalidPaymentOrderError("invalid plan code")
    return normalized


def _provider_ref() -> str:
    return "W" + uuid.uuid4().hex[:31]


def _summary(db: Session, order: PaymentOrder) -> PaymentOrderSummary:
    subscription_ends_at = None
    if order.activation_id:
        activation = db.get(SubscriptionActivation, order.activation_id)
        if activation is not None and activation.applied_ends_at is not None:
            subscription_ends_at = _utc(activation.applied_ends_at)
    return PaymentOrderSummary(
        order_id=order.id,
        plan_code=order.plan_code,
        plan_name=order.plan_name,
        amount_cents=order.amount_cents,
        currency=order.currency,
        provider=order.provider,
        status=order.status,
        provider_state=order.provider_state,
        created_at=_utc(order.created_at),
        expires_at=_utc(order.expires_at),
        paid_at=_utc(order.paid_at) if order.paid_at else None,
        activated_at=_utc(order.activated_at) if order.activated_at else None,
        subscription_ends_at=subscription_ends_at,
        failure_code=order.failure_code,
        qr_available=(
            order.status == "pending"
            and order.checkout_url is not None
            and _utc(order.expires_at) > datetime.now(timezone.utc)
        ),
    )


def get_order(
    db: Session, tenant_id: str, order_id: str
) -> PaymentOrderSummary:
    order = db.scalar(
        select(PaymentOrder).where(
            PaymentOrder.id == order_id,
            PaymentOrder.tenant_id == tenant_id,
        )
    )
    if order is None:
        raise PaymentOrderNotFoundError("payment order does not exist")
    return _summary(db, order)


def get_latest_order(db: Session, tenant_id: str) -> PaymentOrderSummary | None:
    order = db.scalar(
        select(PaymentOrder)
        .where(PaymentOrder.tenant_id == tenant_id)
        .order_by(PaymentOrder.created_at.desc(), PaymentOrder.id.desc())
        .limit(1)
    )
    return _summary(db, order) if order is not None else None


def get_checkout_url(db: Session, tenant_id: str, order_id: str) -> str:
    order = db.scalar(
        select(PaymentOrder).where(
            PaymentOrder.id == order_id,
            PaymentOrder.tenant_id == tenant_id,
        )
    )
    if order is None:
        raise PaymentOrderNotFoundError("payment order does not exist")
    if (
        order.status != "pending"
        or order.checkout_url is None
        or _utc(order.expires_at) <= datetime.now(timezone.utc)
    ):
        raise PaymentOrderConflictError("payment QR code is unavailable")
    return order.checkout_url


def _mark_create_failed(
    session_factory: SessionFactory, order_id: str, failure_code: str
) -> None:
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id)
            .with_for_update()
        )
        if order is None or order.status != "creating":
            return
        order.status = "failed"
        order.failure_code = failure_code
        order.checkout_url = None
        db.commit()


def create_payment_order(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    command: CreateOrderCommand,
) -> PaymentOrderSummary:
    tenant_id = command.tenant_id.strip()
    if not tenant_id or len(tenant_id) > 36:
        raise InvalidPaymentOrderError("invalid tenant")
    plan_code = _safe_plan_code(command.plan_code)
    idempotency_hash = _key_hash(command.idempotency_key)
    now = _explicit_utc(command.now)

    with session_factory() as db:
        tenant = db.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        if tenant is None or tenant.lifecycle_status not in {"provisioning", "active"}:
            raise InvalidPaymentOrderError("tenant cannot purchase a plan")
        existing = db.scalar(
            select(PaymentOrder).where(
                PaymentOrder.tenant_id == tenant_id,
                PaymentOrder.idempotency_key_hash == idempotency_hash,
            )
        )
        if existing is not None:
            if existing.plan_code != plan_code or existing.provider != provider.code:
                raise PaymentOrderConflictError(
                    "idempotency key was already used for another order"
                )
            return _summary(db, existing)
        plan = db.scalar(
            select(BillingPlan).where(
                BillingPlan.code == plan_code,
                BillingPlan.is_active.is_(True),
            )
        )
        if plan is None:
            raise InvalidPaymentOrderError("plan is unavailable")
        order = PaymentOrder(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            plan_id=plan.id,
            plan_code=plan.code,
            plan_name=plan.display_name,
            amount_cents=plan.amount_cents,
            currency=plan.currency,
            provider=provider.code,
            provider_order_ref=_provider_ref(),
            status="creating",
            idempotency_key_hash=idempotency_hash,
            created_at=now,
            expires_at=now + timedelta(minutes=ORDER_TTL_MINUTES),
        )
        order_id = order.id
        provider_order_ref = order.provider_order_ref
        request = PaymentRequest(
            provider_order_ref=provider_order_ref,
            description=f"365企微会话存档-{plan.display_name}",
            amount_cents=plan.amount_cents,
            currency=plan.currency,
            expires_at=order.expires_at,
        )
        db.add(order)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            winner = db.scalar(
                select(PaymentOrder).where(
                    PaymentOrder.tenant_id == tenant_id,
                    PaymentOrder.idempotency_key_hash == idempotency_hash,
                )
            )
            if winner is None:
                raise
            if winner.plan_code != plan_code or winner.provider != provider.code:
                raise PaymentOrderConflictError(
                    "idempotency key was already used for another order"
                )
            return _summary(db, winner)

    try:
        artifact = provider.create_payment(request)
        if (
            artifact.provider_order_ref != provider_order_ref
            or artifact.kind != "qr_code"
            or not artifact.value
        ):
            raise PaymentOrderError("payment provider returned an invalid checkout")
    except Exception:
        _mark_create_failed(session_factory, order_id, "provider_create_failed")
        raise

    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id)
            .with_for_update()
        )
        if order is None:
            raise PaymentOrderError("payment order disappeared")
        if order.status == "creating":
            order.status = "pending"
            order.provider_state = "NOTPAY"
            order.checkout_url = artifact.value
            db.commit()
        return _summary(db, order)


def _validate_event(provider: PaymentProvider, order: PaymentOrder, event: TrustedPaymentEvent) -> None:
    if (
        event.provider != provider.code
        or event.provider != order.provider
        or event.provider_order_ref != order.provider_order_ref
        or event.app_id != provider.app_id
        or event.merchant_id != provider.merchant_id
        or event.state != "SUCCESS"
        or event.event_type != "payment_succeeded"
        or event.amount_cents != order.amount_cents
        or event.currency != order.currency
        or event.source not in {"callback", "query"}
        or not event.provider_event_id
        or len(event.provider_event_id) > 128
        or not event.provider_transaction_id
        or len(event.provider_transaction_id) > 64
        or len(event.payload_hash) != 64
    ):
        raise PaymentOrderConflictError("trusted payment does not match the order")


def _mark_activation_pending(
    session_factory: SessionFactory, order_id: str, provider_transaction_id: str
) -> None:
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id)
            .with_for_update()
        )
        if (
            order is None
            or order.provider_transaction_id != provider_transaction_id
            or order.status == "succeeded"
        ):
            return
        order.status = "paid_activation_pending"
        order.failure_code = "subscription_update_failed"
        db.commit()


def apply_trusted_payment(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    event: TrustedPaymentEvent,
) -> PaymentOrderSummary:
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(
                PaymentOrder.provider == provider.code,
                PaymentOrder.provider_order_ref == event.provider_order_ref,
            )
            .with_for_update()
        )
        if order is None:
            raise PaymentOrderNotFoundError("payment order does not exist")
        _validate_event(provider, order, event)
        duplicate = db.scalar(
            select(PaymentEvent).where(
                PaymentEvent.provider == event.provider,
                PaymentEvent.provider_event_id == event.provider_event_id,
            )
        )
        if duplicate is not None:
            if (
                duplicate.order_id != order.id
                or duplicate.payload_hash != event.payload_hash
                or duplicate.provider_transaction_id
                != event.provider_transaction_id
            ):
                raise PaymentReplayConflictError(
                    "provider event ID was replayed with different content"
                )
        else:
            db.add(
                PaymentEvent(
                    id=str(uuid.uuid4()),
                    order_id=order.id,
                    provider=event.provider,
                    provider_event_id=event.provider_event_id,
                    provider_transaction_id=event.provider_transaction_id,
                    event_type=event.event_type,
                    source=event.source,
                    payload_hash=event.payload_hash,
                    occurred_at=_explicit_utc(event.succeeded_at),
                )
            )
        if (
            order.provider_transaction_id is not None
            and order.provider_transaction_id != event.provider_transaction_id
        ):
            raise PaymentOrderConflictError(
                "order already has another provider transaction"
            )
        transaction_owner = db.scalar(
            select(PaymentOrder.id).where(
                PaymentOrder.provider == event.provider,
                PaymentOrder.provider_transaction_id
                == event.provider_transaction_id,
                PaymentOrder.id != order.id,
            )
        )
        if transaction_owner is not None:
            raise PaymentOrderConflictError(
                "provider transaction already belongs to another order"
            )
        already_succeeded = order.status == "succeeded" and order.activation_id is not None
        order.provider_transaction_id = event.provider_transaction_id
        order.provider_state = "SUCCESS"
        order.paid_at = _explicit_utc(event.succeeded_at)
        order.checkout_url = None
        if not already_succeeded:
            order.status = "paid_activation_pending"
            order.failure_code = None
        order_id = order.id
        tenant_id = order.tenant_id
        plan_code = order.plan_code
        try:
            db.commit()
        except IntegrityError as error:
            db.rollback()
            raise PaymentOrderConflictError("payment event conflicts with existing data") from error
        if already_succeeded:
            ensure_payment_term_grant(db, order)
            db.commit()
            return _summary(db, order)

    try:
        activation = activate_or_renew_subscription(
            session_factory,
            ActivationCommand(
                tenant_id=tenant_id,
                plan_code=plan_code,
                source=event.provider,
                idempotency_key=event.provider_transaction_id,
                trusted_at=_explicit_utc(event.succeeded_at),
            ),
        )
    except Exception as error:
        _mark_activation_pending(
            session_factory, order_id, event.provider_transaction_id
        )
        raise PaymentActivationPendingError(
            "payment succeeded but subscription activation is pending"
        ) from error

    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id)
            .with_for_update()
        )
        if order is None or order.provider_transaction_id != event.provider_transaction_id:
            raise PaymentOrderConflictError("payment order changed during activation")
        order.status = "succeeded"
        order.activation_id = activation.activation_id
        order.activated_at = datetime.now(timezone.utc)
        order.failure_code = None
        db.flush()
        ensure_payment_term_grant(db, order)
        db.commit()
        return _summary(db, order)


def query_and_reconcile_order(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    tenant_id: str,
    order_id: str,
    *,
    now: datetime,
) -> PaymentOrderSummary:
    checked_at = _explicit_utc(now)
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder).where(
                PaymentOrder.id == order_id,
                PaymentOrder.tenant_id == tenant_id,
            )
        )
        if order is None:
            raise PaymentOrderNotFoundError("payment order does not exist")
        if order.provider != provider.code:
            raise PaymentOrderConflictError("payment provider mismatch")
        provider_order_ref = order.provider_order_ref
        if order.status == "succeeded":
            return _summary(db, order)

    result = provider.query_payment(provider_order_ref)
    if (
        result.provider != provider.code
        or result.provider_order_ref != provider_order_ref
        or result.status not in {"succeeded", "pending", "closed", "failed"}
    ):
        raise PaymentOrderConflictError("payment query result mismatch")
    if result.success is not None:
        if result.status != "succeeded":
            raise PaymentOrderConflictError("payment query result mismatch")
        return apply_trusted_payment(session_factory, provider, result.success)
    if result.status == "succeeded":
        raise PaymentOrderConflictError("successful payment query has no trusted event")

    should_close = False
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(
                PaymentOrder.id == order_id,
                PaymentOrder.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if order is None:
            raise PaymentOrderNotFoundError("payment order does not exist")
        if order.status in {"paid_activation_pending", "succeeded"}:
            return _summary(db, order)
        order.provider_state = result.state
        if result.status in {"closed", "failed"}:
            order.status = result.status
            order.failure_code = (
                None if order.status == "closed" else "provider_payment_failed"
            )
            order.closed_at = checked_at
            order.checkout_url = None
        elif result.status == "pending" and _utc(order.expires_at) <= checked_at:
            should_close = True
        db.commit()
        if not should_close:
            return _summary(db, order)

    provider.close_payment(provider_order_ref)
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id)
            .with_for_update()
        )
        if order is None:
            raise PaymentOrderNotFoundError("payment order does not exist")
        if order.status in {"creating", "pending"}:
            order.status = "closed"
            order.provider_state = "CLOSED"
            order.closed_at = checked_at
            order.checkout_url = None
            db.commit()
        return _summary(db, order)


def close_order(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    tenant_id: str,
    order_id: str,
    *,
    now: datetime,
) -> PaymentOrderSummary:
    closed_at = _explicit_utc(now)
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder).where(
                PaymentOrder.id == order_id,
                PaymentOrder.tenant_id == tenant_id,
            )
        )
        if order is None:
            raise PaymentOrderNotFoundError("payment order does not exist")
        if order.provider != provider.code:
            raise PaymentOrderConflictError("payment provider mismatch")
        if order.status == "closed":
            return _summary(db, order)
        if order.status not in {"creating", "pending", "failed"}:
            raise PaymentOrderConflictError("paid orders cannot be closed")
        provider_order_ref = order.provider_order_ref

    provider.close_payment(provider_order_ref)
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id)
            .with_for_update()
        )
        if order is None:
            raise PaymentOrderNotFoundError("payment order does not exist")
        if order.status not in {"paid_activation_pending", "succeeded"}:
            order.status = "closed"
            order.provider_state = "CLOSED"
            order.closed_at = closed_at
            order.checkout_url = None
            order.failure_code = None
            db.commit()
        return _summary(db, order)
