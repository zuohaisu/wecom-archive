"""Durable scheduling and delivery for billing lifecycle notifications.

The database stores only fixed event kinds, coarse failure codes and internal
object identifiers. Provider transaction details, WeCom identities and email
addresses are resolved only while delivering and are never copied into the
outbox or its append-only attempts.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    AdminUser,
    BillingNotificationAttempt,
    BillingNotificationIntent,
    PaymentOrder,
    PaymentRecoveryFinding,
    PlatformAdmin,
    RefundOrder,
    Subscription,
    Tenant,
)
from app.email import email_delivery_ready, send_billing_notification_email
from app.settings import get_wecom_oauth_settings


MAX_DELIVERY_ATTEMPTS = 5
PROCESSING_REFUND_TIMEOUT = timedelta(hours=24)
SCHEDULER_CATCHUP_WINDOW = timedelta(hours=24)
_SUBSCRIPTION_KINDS = frozenset(
    {
        "subscription_expiry_30d",
        "subscription_expiry_7d",
        "subscription_expiry_1d",
        "subscription_expired",
        "subscription_grace_ending_1d",
        "tenant_frozen",
    }
)
_PAYMENT_KINDS = frozenset(
    {
        "payment_activation_pending",
        "payment_pending_timeout",
        "payment_channel_paid_local_pending",
        "payment_query_failed",
        "payment_reconciliation_mismatch",
        "payment_callback_signature_failure",
        "payment_callback_decrypt_failure",
    }
)
_REFUND_KINDS = frozenset(
    {
        "refund_processing_timeout",
        "refund_abnormal",
        "refund_manual_recovery_required",
    }
)

DeliveryFunction = Callable[[str, str, datetime, str, str], bool]


@dataclass(frozen=True)
class BillingNotificationRunSummary:
    scheduled: int = 0
    canceled: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    deferred: int = 0


@dataclass(frozen=True)
class _PlannedEvent:
    tenant_id: str | None
    subject_type: str
    subject_id: str
    kind: str
    audience: str
    context_key: str
    source_revision: int | None
    effective_at: datetime
    scheduled_at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _explicit_utc(value: datetime | None) -> datetime:
    candidate = value or datetime.now(timezone.utc)
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise ValueError("notification time must include a timezone")
    return candidate.astimezone(timezone.utc)


def _hash(*parts: object) -> str:
    canonical = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _subscription_context(subscription: Subscription) -> str:
    return _hash(
        "subscription-term",
        subscription.id,
        _utc(subscription.ends_at).isoformat(),
        _utc(subscription.grace_ends_at).isoformat(),
    )


def _event_dedupe(event: _PlannedEvent) -> str:
    return _hash(
        event.subject_type,
        event.subject_id,
        event.kind,
        event.audience,
        event.context_key,
    )


def _eligible_schedule(scheduled_at: datetime, now: datetime) -> bool:
    # A newly installed scheduler must not blast every historical reminder.
    # It plans all future thresholds and catches at most one day of downtime.
    return scheduled_at >= now - SCHEDULER_CATCHUP_WINDOW


def _subscription_events(
    subscription: Subscription,
    *,
    now: datetime,
) -> list[_PlannedEvent]:
    if subscription.status == "canceled":
        return []
    ends_at = _utc(subscription.ends_at)
    grace_ends_at = _utc(subscription.grace_ends_at)
    context_key = _subscription_context(subscription)
    candidates = (
        ("subscription_expiry_30d", ends_at, ends_at - timedelta(days=30)),
        ("subscription_expiry_7d", ends_at, ends_at - timedelta(days=7)),
        ("subscription_expiry_1d", ends_at, ends_at - timedelta(days=1)),
        ("subscription_expired", ends_at, ends_at),
        (
            "subscription_grace_ending_1d",
            grace_ends_at,
            grace_ends_at - timedelta(days=1),
        ),
        ("tenant_frozen", grace_ends_at, grace_ends_at),
    )
    return [
        _PlannedEvent(
            tenant_id=subscription.tenant_id,
            subject_type="subscription",
            subject_id=subscription.id,
            kind=kind,
            audience="owner",
            context_key=context_key,
            source_revision=subscription.revision,
            effective_at=effective_at,
            scheduled_at=scheduled_at,
        )
        for kind, effective_at, scheduled_at in candidates
        if _eligible_schedule(scheduled_at, now)
    ]


def _payment_events(db: Session) -> list[_PlannedEvent]:
    rows = db.scalars(
        select(PaymentOrder).where(
            PaymentOrder.status == "paid_activation_pending"
        )
    ).all()
    return [
        _PlannedEvent(
            tenant_id=row.tenant_id,
            subject_type="payment_order",
            subject_id=row.id,
            kind="payment_activation_pending",
            audience="operations",
            context_key=_hash("payment_activation_pending", row.id),
            source_revision=None,
            effective_at=_utc(row.paid_at or row.updated_at or row.created_at),
            scheduled_at=_utc(row.paid_at or row.updated_at or row.created_at),
        )
        for row in rows
    ]


def _payment_recovery_finding_events(db: Session) -> list[_PlannedEvent]:
    """Open findings are the source of operations-only payment alerts.

    Global callback cryptographic failures intentionally have no tenant id;
    the outbox schema supports this only for the operations audience.
    """
    rows = db.scalars(
        select(PaymentRecoveryFinding).where(PaymentRecoveryFinding.status == "open")
    ).all()
    return [
        _PlannedEvent(
            tenant_id=row.tenant_id,
            subject_type="payment_recovery_finding",
            subject_id=row.id,
            kind=row.kind,
            audience="operations",
            context_key=_hash("payment_recovery_finding", row.id),
            source_revision=None,
            effective_at=_utc(row.first_detected_at),
            scheduled_at=_utc(row.first_detected_at),
        )
        for row in rows
    ]


def _refund_events(db: Session) -> list[_PlannedEvent]:
    rows = db.scalars(
        select(RefundOrder).where(
            RefundOrder.status.in_(
                ("processing", "abnormal", "manual_recovery_required")
            )
        )
    ).all()
    events: list[_PlannedEvent] = []
    for row in rows:
        if row.status == "processing":
            effective_at = _utc(row.provider_accepted_at or row.requested_at)
            kind = "refund_processing_timeout"
            scheduled_at = effective_at + PROCESSING_REFUND_TIMEOUT
        elif row.status == "abnormal":
            effective_at = _utc(row.updated_at or row.requested_at)
            kind = "refund_abnormal"
            scheduled_at = effective_at
        else:
            effective_at = _utc(row.updated_at or row.requested_at)
            kind = "refund_manual_recovery_required"
            scheduled_at = effective_at
        events.append(
            _PlannedEvent(
                tenant_id=row.tenant_id,
                subject_type="refund_order",
                subject_id=row.id,
                kind=kind,
                audience="operations",
                context_key=_hash(kind, row.id),
                source_revision=None,
                effective_at=effective_at,
                scheduled_at=scheduled_at,
            )
        )
    return events


def _ensure_intent(db: Session, event: _PlannedEvent) -> bool:
    dedupe_key = _event_dedupe(event)
    existing = db.scalar(
        select(BillingNotificationIntent.id).where(
            BillingNotificationIntent.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return False
    try:
        with db.begin_nested():
            db.add(
                BillingNotificationIntent(
                    id=str(uuid.uuid4()),
                    tenant_id=event.tenant_id,
                    subject_type=event.subject_type,
                    subject_id=event.subject_id,
                    kind=event.kind,
                    audience=event.audience,
                    context_key=event.context_key,
                    dedupe_key=dedupe_key,
                    source_revision=event.source_revision,
                    effective_at=event.effective_at,
                    scheduled_at=event.scheduled_at,
                    next_attempt_at=event.scheduled_at,
                    status="pending",
                )
            )
            db.flush()
    except IntegrityError:
        return False
    return True


def _cancel_obsolete_subscription_intents(
    db: Session,
    subscriptions: list[Subscription],
    *,
    now: datetime,
) -> int:
    canceled = 0
    for subscription in subscriptions:
        current_context = _subscription_context(subscription)
        rows = db.scalars(
            select(BillingNotificationIntent).where(
                BillingNotificationIntent.tenant_id == subscription.tenant_id,
                BillingNotificationIntent.subject_type == "subscription",
                BillingNotificationIntent.subject_id == subscription.id,
                BillingNotificationIntent.status == "pending",
            )
        ).all()
        for intent in rows:
            if (
                subscription.status == "canceled"
                or intent.context_key != current_context
            ):
                intent.status = "canceled"
                intent.cancellation_code = "subscription_context_changed"
                intent.canceled_at = now
                canceled += 1
    return canceled


def _cancel_resolved_anomaly_intents(
    db: Session,
    desired_dedupe_keys: set[str],
    *,
    now: datetime,
) -> int:
    rows = db.scalars(
        select(BillingNotificationIntent).where(
            BillingNotificationIntent.status == "pending",
            BillingNotificationIntent.kind.in_(tuple(_PAYMENT_KINDS | _REFUND_KINDS)),
        )
    ).all()
    canceled = 0
    for intent in rows:
        if intent.dedupe_key not in desired_dedupe_keys:
            intent.status = "canceled"
            intent.cancellation_code = "anomaly_resolved"
            intent.canceled_at = now
            canceled += 1
    return canceled


def plan_billing_notification_intents(
    db: Session,
    *,
    at: datetime | None = None,
) -> tuple[int, int]:
    """Plan current intents and cancel obsolete future work without deleting it."""
    checked_at = _explicit_utc(at)
    subscriptions = list(db.scalars(select(Subscription)).all())
    events = [
        event
        for subscription in subscriptions
        for event in _subscription_events(subscription, now=checked_at)
    ]
    anomaly_events = (
        _payment_events(db)
        + _payment_recovery_finding_events(db)
        + _refund_events(db)
    )
    events.extend(anomaly_events)
    scheduled = sum(1 for event in events if _ensure_intent(db, event))
    canceled = _cancel_obsolete_subscription_intents(
        db, subscriptions, now=checked_at
    )
    desired_anomalies = {_event_dedupe(event) for event in anomaly_events}
    canceled += _cancel_resolved_anomaly_intents(
        db, desired_anomalies, now=checked_at
    )
    db.flush()
    return scheduled, canceled


def _admin_link(path: str) -> str | None:
    domain = (get_wecom_oauth_settings().admin_domain or "").strip()
    for prefix in ("https://", "http://"):
        if domain.lower().startswith(prefix):
            domain = domain[len(prefix) :]
            break
    domain = domain.strip("/")
    if not domain or "/" in domain or "@" in domain:
        return None
    return f"https://{domain}{path}"


def _recipient(db: Session, intent: BillingNotificationIntent) -> tuple[str, str] | None:
    if intent.audience == "owner":
        owner = db.scalar(
            select(AdminUser)
            .where(
                AdminUser.tenant_id == intent.tenant_id,
                AdminUser.role == "owner",
                AdminUser.status == "active",
            )
            .order_by(AdminUser.created_at.asc(), AdminUser.id.asc())
        )
        email = (owner.email or "").strip() if owner is not None else ""
        if "@" not in email or email.startswith("@"):
            return None
        return email, owner.ui_locale or "zh-CN"
    operator = db.scalar(
        select(PlatformAdmin)
        .where(PlatformAdmin.status == "active")
        .order_by(PlatformAdmin.created_at.asc(), PlatformAdmin.id.asc())
    )
    email = (operator.email or "").strip() if operator is not None else ""
    if "@" not in email or email.startswith("@"):
        return None
    return email, "zh-CN"


def _applicability(
    db: Session,
    intent: BillingNotificationIntent,
) -> str:
    """Return applicable, wait or cancel for a due locked intent."""
    if intent.subject_type == "subscription":
        subscription = db.get(Subscription, intent.subject_id)
        if (
            subscription is None
            or subscription.tenant_id != intent.tenant_id
            or subscription.status == "canceled"
            or _subscription_context(subscription) != intent.context_key
        ):
            return "cancel"
        if intent.kind == "tenant_frozen":
            tenant = db.get(Tenant, intent.tenant_id)
            if tenant is None or tenant.lifecycle_status == "suspended":
                return "cancel"
            if tenant.lifecycle_status != "frozen":
                return "wait"
        return "applicable"
    if intent.subject_type == "payment_order":
        payment = db.get(PaymentOrder, intent.subject_id)
        return (
            "applicable"
            if payment is not None
            and payment.tenant_id == intent.tenant_id
            and payment.status == "paid_activation_pending"
            else "cancel"
        )
    if intent.subject_type == "payment_recovery_finding":
        finding = db.get(PaymentRecoveryFinding, intent.subject_id)
        return "applicable" if finding is not None and finding.status == "open" else "cancel"
    refund = db.get(RefundOrder, intent.subject_id)
    expected = {
        "refund_processing_timeout": "processing",
        "refund_abnormal": "abnormal",
        "refund_manual_recovery_required": "manual_recovery_required",
    }.get(intent.kind)
    return (
        "applicable"
        if refund is not None
        and refund.tenant_id == intent.tenant_id
        and refund.status == expected
        else "cancel"
    )


def _failure_code_for_delivery(
    db: Session,
    intent: BillingNotificationIntent,
    delivery: DeliveryFunction | None,
) -> tuple[str | None, tuple[str, str] | None, str | None]:
    recipient = _recipient(db, intent)
    if recipient is None:
        return "recipient_unavailable", None, None
    path = "/admin/billing" if intent.audience == "owner" else "/platform/operations"
    link = _admin_link(path)
    if link is None:
        return "action_link_unavailable", recipient, None
    if delivery is None:
        if not email_delivery_ready():
            return "transport_unconfigured", recipient, link
    return None, recipient, link


def _retry_delay(attempt_no: int) -> timedelta:
    seconds = min(6 * 60 * 60, 5 * 60 * (2 ** max(0, attempt_no - 1)))
    return timedelta(seconds=seconds)


def _record_attempt(
    db: Session,
    intent: BillingNotificationIntent,
    *,
    at: datetime,
    sent: bool,
    failure_code: str | None,
) -> str:
    attempt_no = int(intent.attempt_count or 0) + 1
    intent.attempt_count = attempt_no
    db.add(
        BillingNotificationAttempt(
            id=str(uuid.uuid4()),
            intent_id=intent.id,
            tenant_id=intent.tenant_id,
            attempt_no=attempt_no,
            outcome="sent" if sent else "failed",
            failure_code=None if sent else (failure_code or "delivery_failed"),
            attempted_at=at,
        )
    )
    if sent:
        intent.status = "sent"
        intent.sent_at = at
        return "sent"
    if attempt_no >= MAX_DELIVERY_ATTEMPTS:
        intent.status = "failed"
        return "failed"
    intent.next_attempt_at = at + _retry_delay(attempt_no)
    return "retried"


def deliver_due_billing_notifications(
    db: Session,
    *,
    at: datetime | None = None,
    limit: int = 20,
    delivery: DeliveryFunction | None = None,
) -> tuple[int, int, int, int]:
    """Deliver due intents with per-intent commits and sanitized failures."""
    checked_at = _explicit_utc(at)
    intent_ids = list(
        db.scalars(
            select(BillingNotificationIntent.id)
            .where(
                BillingNotificationIntent.status == "pending",
                BillingNotificationIntent.scheduled_at <= checked_at,
                BillingNotificationIntent.next_attempt_at <= checked_at,
            )
            .order_by(
                BillingNotificationIntent.scheduled_at.asc(),
                BillingNotificationIntent.id.asc(),
            )
            .limit(max(1, min(100, limit)))
        ).all()
    )
    db.rollback()
    sent = retried = failed = deferred = 0
    for intent_id in intent_ids:
        try:
            intent = db.scalar(
                select(BillingNotificationIntent)
                .where(
                    BillingNotificationIntent.id == intent_id,
                    BillingNotificationIntent.status == "pending",
                )
                .with_for_update(skip_locked=True)
            )
            if intent is None:
                db.rollback()
                continue
            applicability = _applicability(db, intent)
            if applicability == "cancel":
                intent.status = "canceled"
                intent.cancellation_code = "event_no_longer_applicable"
                intent.canceled_at = checked_at
                db.commit()
                continue
            if applicability == "wait":
                intent.next_attempt_at = checked_at + timedelta(minutes=5)
                db.commit()
                deferred += 1
                continue
            error_code, recipient, link = _failure_code_for_delivery(
                db, intent, delivery
            )
            delivered = False
            if error_code is None and recipient is not None and link is not None:
                email, locale = recipient
                sender = delivery or send_billing_notification_email
                try:
                    delivered = bool(
                        sender(
                            email,
                            intent.kind,
                            _utc(intent.effective_at),
                            link,
                            locale,
                            **({} if delivery is not None else {
                                "operation_id": f"billing-intent/{intent.id}",
                            }),
                        )
                    )
                except Exception:  # noqa: BLE001 - persist only a fixed code
                    error_code = "delivery_exception"
            outcome = _record_attempt(
                db,
                intent,
                at=checked_at,
                sent=delivered,
                failure_code=error_code or "delivery_failed",
            )
            db.commit()
            sent += outcome == "sent"
            retried += outcome == "retried"
            failed += outcome == "failed"
        except Exception:  # noqa: BLE001 - one tenant/intent must not stop others
            db.rollback()
            try:
                intent = db.scalar(
                    select(BillingNotificationIntent)
                    .where(
                        BillingNotificationIntent.id == intent_id,
                        BillingNotificationIntent.status == "pending",
                    )
                    .with_for_update()
                )
                if intent is not None:
                    outcome = _record_attempt(
                        db,
                        intent,
                        at=checked_at,
                        sent=False,
                        failure_code="worker_error",
                    )
                    db.commit()
                    retried += outcome == "retried"
                    failed += outcome == "failed"
                else:
                    db.rollback()
            except Exception:  # noqa: BLE001 - caller still gets aggregate failure
                db.rollback()
                failed += 1
    return sent, retried, failed, deferred


def run_billing_notifications_once(
    db: Session,
    *,
    at: datetime | None = None,
    limit: int = 20,
    delivery: DeliveryFunction | None = None,
) -> BillingNotificationRunSummary:
    checked_at = _explicit_utc(at)
    scheduled, canceled = plan_billing_notification_intents(db, at=checked_at)
    db.commit()
    sent, retried, failed, deferred = deliver_due_billing_notifications(
        db,
        at=checked_at,
        limit=limit,
        delivery=delivery,
    )
    return BillingNotificationRunSummary(
        scheduled=scheduled,
        canceled=canceled,
        sent=sent,
        retried=retried,
        failed=failed,
        deferred=deferred,
    )
