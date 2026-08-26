"""Bounded WeChat payment recovery and T+1 reconciliation for RND-390.

This module owns only scheduling metadata and operational findings.  Every
payment fact still flows through ``query_and_reconcile_order`` and ultimately
``apply_trusted_payment`` / ``SubscriptionActivation``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import PaymentOrder, PaymentRecoveryFinding
from app.services.payment_orders import (
    PaymentActivationPendingError,
    PaymentOrderConflictError,
    PaymentReconciliationMismatchError,
    query_and_reconcile_order,
)
from app.services.payment_provider import PaymentProvider
from app.services.wechat_pay import WECHAT_PAY_PROVIDER, WechatPayProtocolError

SessionFactory = Callable[[], Session]

RECOVERY_BATCH_LIMIT = 20
RECONCILIATION_BATCH_LIMIT = 50
RECOVERY_WINDOW = timedelta(hours=24)
RECONCILIATION_BACKLOG = timedelta(days=30)
RECOVERY_LEASE = timedelta(minutes=4)
PENDING_TIMEOUT_GRACE = timedelta(minutes=5)
MAX_QUERY_ATTEMPTS = 8
_QUERY_DELAYS = (5, 10, 20, 40, 80, 160, 320)

FINDING_PENDING_TIMEOUT = "payment_pending_timeout"
FINDING_CHANNEL_PAID_LOCAL_PENDING = "payment_channel_paid_local_pending"
FINDING_ACTIVATION_PENDING = "payment_activation_pending"
FINDING_QUERY_FAILED = "payment_query_failed"
FINDING_RECONCILIATION_MISMATCH = "payment_reconciliation_mismatch"
FINDING_CALLBACK_SIGNATURE_FAILURE = "payment_callback_signature_failure"
FINDING_CALLBACK_DECRYPT_FAILURE = "payment_callback_decrypt_failure"

_FINDINGS = frozenset(
    {
        FINDING_PENDING_TIMEOUT,
        FINDING_CHANNEL_PAID_LOCAL_PENDING,
        FINDING_ACTIVATION_PENDING,
        FINDING_QUERY_FAILED,
        FINDING_RECONCILIATION_MISMATCH,
        FINDING_CALLBACK_SIGNATURE_FAILURE,
        FINDING_CALLBACK_DECRYPT_FAILURE,
    }
)
_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True)
class RecoveryRunSummary:
    claimed: int = 0
    recovered: int = 0
    pending: int = 0
    manual_recovery: int = 0
    failed: int = 0


@dataclass(frozen=True)
class _Claim:
    order_id: str
    tenant_id: str
    initial_status: str


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("recovery time must include a timezone")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    """Normalize a persisted UTC value across PostgreSQL and SQLite tests."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hash(*parts: object) -> str:
    return hashlib.sha256(
        "\x1f".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()


def _retry_delay(attempt_count: int) -> timedelta:
    minutes = _QUERY_DELAYS[min(max(0, attempt_count - 1), len(_QUERY_DELAYS) - 1)]
    return timedelta(minutes=minutes)


def _finding_key(kind: str, order_id: str | None, at: datetime) -> str:
    if order_id is not None:
        return _hash("payment-recovery", kind, order_id)
    # Public callback failures have no trusted tenant/order identity. Bound
    # them to one finding per kind per UTC day so invalid internet traffic can
    # neither fill the table nor generate an email storm.
    return _hash("payment-recovery-callback", kind, at.date().isoformat())


def _open_finding(
    db: Session,
    *,
    kind: str,
    severity: str,
    at: datetime,
    order: PaymentOrder | None = None,
) -> PaymentRecoveryFinding:
    if kind not in _FINDINGS or severity not in _SEVERITY_ORDER:
        raise ValueError("unsupported payment recovery finding")
    key = _finding_key(kind, order.id if order is not None else None, at)
    finding = db.scalar(
        select(PaymentRecoveryFinding)
        .where(PaymentRecoveryFinding.dedupe_key == key)
        .with_for_update()
    )
    if finding is not None:
        finding.occurrence_count += 1
        finding.last_detected_at = at
        finding.status = "open"
        finding.resolved_at = None
        if _SEVERITY_ORDER[severity] > _SEVERITY_ORDER[finding.severity]:
            finding.severity = severity
        return finding
    finding = PaymentRecoveryFinding(
        id=str(uuid.uuid4()),
        provider=WECHAT_PAY_PROVIDER,
        tenant_id=order.tenant_id if order is not None else None,
        payment_order_id=order.id if order is not None else None,
        kind=kind,
        severity=severity,
        status="open",
        dedupe_key=key,
        occurrence_count=1,
        first_detected_at=at,
        last_detected_at=at,
    )
    try:
        with db.begin_nested():
            db.add(finding)
            db.flush()
        return finding
    except IntegrityError:
        # Concurrent public callbacks can race the first insert. Re-read and
        # coalesce them into the same rate-limited finding.
        finding = db.scalar(
            select(PaymentRecoveryFinding)
            .where(PaymentRecoveryFinding.dedupe_key == key)
            .with_for_update()
        )
        if finding is None:
            raise
        finding.occurrence_count += 1
        finding.last_detected_at = at
        finding.status = "open"
        finding.resolved_at = None
        if _SEVERITY_ORDER[severity] > _SEVERITY_ORDER[finding.severity]:
            finding.severity = severity
        return finding


def _resolve_order_findings(db: Session, order_id: str, at: datetime) -> None:
    rows = db.scalars(
        select(PaymentRecoveryFinding)
        .where(
            PaymentRecoveryFinding.payment_order_id == order_id,
            PaymentRecoveryFinding.status == "open",
        )
        .with_for_update()
    ).all()
    for finding in rows:
        finding.status = "resolved"
        finding.resolved_at = at


def record_callback_failure(
    session_factory: SessionFactory,
    *,
    kind: str,
    at: datetime | None = None,
) -> None:
    """Persist one globally rate-limited, payload-free callback failure."""
    if kind not in {FINDING_CALLBACK_SIGNATURE_FAILURE, FINDING_CALLBACK_DECRYPT_FAILURE}:
        raise ValueError("unsupported callback finding")
    checked_at = _utc(at or datetime.now(timezone.utc))
    with session_factory() as db:
        _open_finding(db, kind=kind, severity="critical", at=checked_at)
        db.commit()


def _recovery_candidate_condition(at: datetime):
    return and_(
        PaymentOrder.provider == WECHAT_PAY_PROVIDER,
        PaymentOrder.recovery_state == "automatic",
        PaymentOrder.created_at >= at - RECOVERY_WINDOW,
        PaymentOrder.status.in_(
            ("creating", "pending", "paid_activation_pending", "failed")
        ),
        or_(
            PaymentOrder.status != "failed",
            PaymentOrder.failure_code == "provider_create_failed",
        ),
        or_(PaymentOrder.next_query_at.is_(None), PaymentOrder.next_query_at <= at),
        or_(
            PaymentOrder.recovery_lease_until.is_(None),
            PaymentOrder.recovery_lease_until <= at,
        ),
    )


def _claim_recovery_candidates(
    session_factory: SessionFactory,
    *,
    at: datetime,
    limit: int,
) -> list[_Claim]:
    with session_factory() as db:
        rows = db.scalars(
            select(PaymentOrder)
            .where(_recovery_candidate_condition(at))
            .order_by(PaymentOrder.next_query_at.asc(), PaymentOrder.created_at.asc())
            .limit(max(1, min(100, limit)))
            .with_for_update(skip_locked=True)
        ).all()
        claims = []
        for order in rows:
            order.recovery_lease_until = at + RECOVERY_LEASE
            order.last_query_at = at
            claims.append(
                _Claim(order.id, order.tenant_id, order.status))
        db.commit()
        return claims


def _claim_reconciliation_candidates(
    session_factory: SessionFactory,
    *,
    at: datetime,
    limit: int,
) -> list[_Claim]:
    today = datetime(at.year, at.month, at.day, tzinfo=timezone.utc)
    prior_start = today - timedelta(days=1)
    prior_end = today
    backlog_start = today - RECONCILIATION_BACKLOG
    in_prior_day = or_(
        and_(PaymentOrder.created_at >= prior_start, PaymentOrder.created_at < prior_end),
        and_(PaymentOrder.paid_at.is_not(None), PaymentOrder.paid_at >= prior_start, PaymentOrder.paid_at < prior_end),
    )
    historical_unreconciled = and_(
        PaymentOrder.created_at >= backlog_start,
        PaymentOrder.last_reconciled_at.is_(None),
    )
    with session_factory() as db:
        rows = db.scalars(
            select(PaymentOrder)
            .where(
                PaymentOrder.provider == WECHAT_PAY_PROVIDER,
                PaymentOrder.recovery_state != "manual_recovery",
                or_(in_prior_day, historical_unreconciled),
                or_(
                    PaymentOrder.last_reconciled_at.is_(None),
                    PaymentOrder.last_reconciled_at < today,
                ),
                or_(
                    PaymentOrder.recovery_lease_until.is_(None),
                    PaymentOrder.recovery_lease_until <= at,
                ),
            )
            .order_by(PaymentOrder.created_at.asc(), PaymentOrder.id.asc())
            .limit(max(1, min(100, limit)))
            .with_for_update(skip_locked=True)
        ).all()
        claims = []
        for order in rows:
            order.recovery_lease_until = at + RECOVERY_LEASE
            order.last_query_at = at
            claims.append(_Claim(order.id, order.tenant_id, order.status))
        db.commit()
        return claims


def _finish_success(
    session_factory: SessionFactory,
    claim: _Claim,
    *,
    at: datetime,
    reconciliation: bool,
) -> str:
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == claim.order_id)
            .with_for_update()
        )
        if order is None:
            return "failed"
        order.recovery_lease_until = None
        if reconciliation:
            order.last_reconciled_at = at
        if claim.initial_status == "pending" and order.status == "succeeded":
            _open_finding(
                db,
                kind=FINDING_CHANNEL_PAID_LOCAL_PENDING,
                severity="info",
                at=at,
                order=order,
            )
        if order.status in {"succeeded", "closed", "failed"}:
            order.recovery_state = "not_required"
            order.recovery_reason_code = None
            order.next_query_at = None
            _resolve_order_findings(db, order.id, at)
            db.commit()
            return "recovered"
        # A verified pending result is not a query failure. It remains bounded
        # by the payment's short expiry and schedules the next provider check.
        order.query_attempt_count += 1
        if order.query_attempt_count >= MAX_QUERY_ATTEMPTS:
            order.recovery_state = "manual_recovery"
            order.recovery_reason_code = FINDING_QUERY_FAILED
            order.next_query_at = None
            _open_finding(
                db,
                kind=FINDING_QUERY_FAILED,
                severity="critical",
                at=at,
                order=order,
            )
            db.commit()
            return "manual"
        order.next_query_at = at + _retry_delay(order.query_attempt_count)
        if order.status == "paid_activation_pending":
            _open_finding(
                db,
                kind=FINDING_ACTIVATION_PENDING,
                severity="critical",
                at=at,
                order=order,
            )
        db.commit()
        return "pending"


def _finish_failure(
    session_factory: SessionFactory,
    claim: _Claim,
    *,
    at: datetime,
    kind: str,
    manual: bool,
    reconciliation: bool,
) -> str:
    with session_factory() as db:
        order = db.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == claim.order_id)
            .with_for_update()
        )
        if order is None:
            return "failed"
        order.recovery_lease_until = None
        if reconciliation:
            # Leave last_reconciled_at unchanged so the next daily run retries
            # a failed/untrusted channel query rather than declaring success.
            pass
        if (
            order.status == "pending"
            and at >= _stored_utc(order.expires_at) + PENDING_TIMEOUT_GRACE
        ):
            _open_finding(
                db,
                kind=FINDING_PENDING_TIMEOUT,
                severity="warning",
                at=at,
                order=order,
            )
        if manual:
            order.recovery_state = "manual_recovery"
            order.recovery_reason_code = kind
            order.next_query_at = None
            _open_finding(
                db,
                kind=kind,
                severity="critical",
                at=at,
                order=order,
            )
            db.commit()
            return "manual"
        order.query_attempt_count += 1
        exhausted = order.query_attempt_count >= MAX_QUERY_ATTEMPTS
        order.recovery_reason_code = kind
        order.recovery_state = "manual_recovery" if exhausted else "automatic"
        order.next_query_at = None if exhausted else at + _retry_delay(order.query_attempt_count)
        _open_finding(
            db,
            kind=kind,
            severity="critical" if exhausted else "warning",
            at=at,
            order=order,
        )
        db.commit()
        return "manual" if exhausted else "failed"


def _run_claims(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    claims: list[_Claim],
    *,
    at: datetime,
    reconciliation: bool,
) -> RecoveryRunSummary:
    recovered = pending = manual_recovery = failed = 0
    for claim in claims:
        try:
            query_and_reconcile_order(
                session_factory,
                provider,
                claim.tenant_id,
                claim.order_id,
                now=at,
                force_channel_query=reconciliation,
            )
            result = _finish_success(
                session_factory,
                claim,
                at=at,
                reconciliation=reconciliation,
            )
        except PaymentActivationPendingError:
            result = _finish_failure(
                session_factory,
                claim,
                at=at,
                kind=FINDING_ACTIVATION_PENDING,
                manual=False,
                reconciliation=reconciliation,
            )
        except (PaymentReconciliationMismatchError, PaymentOrderConflictError):
            result = _finish_failure(
                session_factory,
                claim,
                at=at,
                kind=FINDING_RECONCILIATION_MISMATCH,
                manual=True,
                reconciliation=reconciliation,
            )
        except (WechatPayProtocolError, Exception):  # provider/DB fault is isolated
            result = _finish_failure(
                session_factory,
                claim,
                at=at,
                kind=FINDING_QUERY_FAILED,
                manual=False,
                reconciliation=reconciliation,
            )
        recovered += result == "recovered"
        pending += result == "pending"
        manual_recovery += result == "manual"
        failed += result == "failed"
    return RecoveryRunSummary(
        claimed=len(claims),
        recovered=recovered,
        pending=pending,
        manual_recovery=manual_recovery,
        failed=failed,
    )


def finalize_manual_payment_query(
    db: Session,
    order_id: str,
    *,
    at: datetime | None = None,
) -> None:
    """Clear manual recovery only after a successful terminal trusted query."""
    checked_at = _utc(at or datetime.now(timezone.utc))
    order = db.scalar(
        select(PaymentOrder)
        .where(PaymentOrder.id == order_id)
        .with_for_update()
    )
    if order is None or order.status not in {"succeeded", "closed", "failed"}:
        return
    order.recovery_state = "not_required"
    order.recovery_reason_code = None
    order.next_query_at = None
    order.recovery_lease_until = None
    _resolve_order_findings(db, order.id, checked_at)
    db.commit()


def run_payment_recovery_once(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    *,
    at: datetime | None = None,
    limit: int = RECOVERY_BATCH_LIMIT,
) -> RecoveryRunSummary:
    checked_at = _utc(at or datetime.now(timezone.utc))
    claims = _claim_recovery_candidates(session_factory, at=checked_at, limit=limit)
    return _run_claims(
        session_factory, provider, claims, at=checked_at, reconciliation=False
    )


def run_payment_reconciliation_once(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    *,
    at: datetime | None = None,
    limit: int = RECONCILIATION_BATCH_LIMIT,
) -> RecoveryRunSummary:
    checked_at = _utc(at or datetime.now(timezone.utc))
    claims = _claim_reconciliation_candidates(
        session_factory, at=checked_at, limit=limit
    )
    return _run_claims(
        session_factory, provider, claims, at=checked_at, reconciliation=True
    )
