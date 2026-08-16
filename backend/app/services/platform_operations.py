"""Read models and controlled writes for the platform-only operations console.

This module deliberately returns tenant and commercial metadata only.  It never
loads archive-message content, encrypted payloads, media paths, payment provider
references, or customer identities.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional
import hashlib
import json
import re
import uuid

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.db.models import (
    AdminUser,
    ArchiveMessage,
    AuditLog,
    BillingPlan,
    Contact,
    ManualFinancialTransaction,
    MediaFile,
    PaymentEvent,
    PaymentOrder,
    RefundEvent,
    RefundOrder,
    Subscription,
    SubscriptionActivation,
    Tenant,
)
from app.services.billing_lifecycle import (
    BillingLifecycleError,
    BillingLifecycleNotFoundError,
    resume_tenant_service,
    suspend_tenant_service,
)
from app.services.entitlements import (
    PlanUnavailableError,
    SubscriptionAssignmentError,
    assign_subscription,
    effective_subscription_status,
    subscription_grace_ends_at,
)

_CURRENCY = "CNY"
_PAID_ORDER_STATUSES = ("paid_activation_pending", "succeeded")
_PAYMENT_EXCEPTION_STATUSES = ("paid_activation_pending", "failed")
_REFUND_PENDING_STATUSES = ("created", "processing")
_REFUND_EXCEPTION_STATUSES = ("closed", "abnormal", "manual_recovery_required")
_SAFE_REASON_CODE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CONTROL_AUDIT_ACTION = AuditAction.PLATFORM_CONTROL_AUTHORIZED
_CONTROL_ACTIONS = frozenset(
    {
        "service.suspend",
        "service.resume",
        "payment.query",
        "refund.submit",
        "refund.query",
    }
)


class PlatformOperationsNotFoundError(LookupError):
    """Raised when a requested tenant does not exist."""


class PlatformOperationsValidationError(ValueError):
    """Raised for an invalid manual operations command."""


class PlatformOperationsConflictError(PlatformOperationsValidationError):
    """Raised when an idempotency key is reused for another command."""


@dataclass(frozen=True)
class TenantSnapshot:
    tenant: Tenant
    plan: BillingPlan | None
    subscription: Subscription | None
    subscription_status: str
    is_entitled: bool
    admin_account_count: int
    active_admin_account_count: int
    observed_user_count: int
    message_count: int
    media_file_count: int
    storage_bytes: int
    last_active_at: datetime | None
    provider_receipt_cents: int
    provider_refund_cents: int
    manual_receipt_cents: int
    manual_refund_cents: int
    payment_exception_count: int
    refund_pending_count: int
    refund_abnormal_count: int
    refund_manual_recovery_count: int
    refund_closed_count: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _subscription_state(subscription: Subscription | None, now: datetime) -> tuple[str, bool]:
    if subscription is None:
        return "not_subscribed", False
    starts_at = _as_utc(subscription.starts_at)
    if now < starts_at:
        return "not_started", False
    effective_status = effective_subscription_status(subscription, at=now)
    return effective_status, effective_status in {"trial", "active", "grace"}


def _rows_by_tenant(
    db: Session,
    model_tenant_id,
    statement,
    tenant_ids: set[str] | None,
) -> list:
    if tenant_ids is not None:
        if not tenant_ids:
            return []
        statement = statement.where(model_tenant_id.in_(tenant_ids))
    return db.execute(statement).all()


def _tenant_snapshots(
    db: Session,
    tenants: Iterable[Tenant],
    *,
    now: datetime,
) -> list[TenantSnapshot]:
    tenant_rows = list(tenants)
    tenant_ids = {tenant.id for tenant in tenant_rows}
    if not tenant_rows:
        return []

    message_rows = _rows_by_tenant(
        db,
        ArchiveMessage.tenant_id,
        select(ArchiveMessage.tenant_id, func.count(ArchiveMessage.id)).group_by(
            ArchiveMessage.tenant_id
        ),
        tenant_ids,
    )
    messages = {tenant_id: int(count or 0) for tenant_id, count in message_rows}

    media_rows = _rows_by_tenant(
        db,
        MediaFile.tenant_id,
        select(
            MediaFile.tenant_id,
            func.count(MediaFile.id),
            func.coalesce(
                func.sum(
                    case(
                        (MediaFile.download_status == "downloaded", MediaFile.file_size),
                        else_=0,
                    )
                ),
                0,
            ),
        ).group_by(MediaFile.tenant_id),
        tenant_ids,
    )
    media = {
        tenant_id: (int(count or 0), int(storage_bytes or 0))
        for tenant_id, count, storage_bytes in media_rows
    }

    account_rows = _rows_by_tenant(
        db,
        AdminUser.tenant_id,
        select(
            AdminUser.tenant_id,
            func.count(AdminUser.id),
            func.coalesce(
                func.sum(case((AdminUser.status == "active", 1), else_=0)), 0
            ),
            func.max(func.coalesce(AdminUser.last_active_at, AdminUser.last_login_at)),
        ).group_by(AdminUser.tenant_id),
        tenant_ids,
    )
    accounts = {
        tenant_id: (int(count or 0), int(active_count or 0), last_active_at)
        for tenant_id, count, active_count, last_active_at in account_rows
    }

    observed_rows = _rows_by_tenant(
        db,
        Contact.tenant_id,
        select(Contact.tenant_id, func.count(func.distinct(Contact.wecom_userid))).group_by(
            Contact.tenant_id
        ),
        tenant_ids,
    )
    observed_users = {tenant_id: int(count or 0) for tenant_id, count in observed_rows}

    payment_rows = _rows_by_tenant(
        db,
        PaymentOrder.tenant_id,
        select(
            PaymentOrder.tenant_id,
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                PaymentOrder.currency == _CURRENCY,
                                PaymentOrder.status.in_(_PAID_ORDER_STATUSES),
                            ),
                            PaymentOrder.amount_cents,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (PaymentOrder.status.in_(_PAYMENT_EXCEPTION_STATUSES), 1),
                        else_=0,
                    )
                ),
                0,
            ),
        ).group_by(PaymentOrder.tenant_id),
        tenant_ids,
    )
    payments = {
        tenant_id: (int(receipts or 0), int(exceptions or 0))
        for tenant_id, receipts, exceptions in payment_rows
    }

    refund_rows = _rows_by_tenant(
        db,
        RefundOrder.tenant_id,
        select(
            RefundOrder.tenant_id,
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                RefundOrder.currency == _CURRENCY,
                                RefundOrder.status == "succeeded",
                            ),
                            RefundOrder.amount_cents,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (RefundOrder.status.in_(_REFUND_PENDING_STATUSES), 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((RefundOrder.status == "abnormal", 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (RefundOrder.status == "manual_recovery_required", 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((RefundOrder.status == "closed", 1), else_=0)),
                0,
            ),
        ).group_by(RefundOrder.tenant_id),
        tenant_ids,
    )
    refunds = {
        tenant_id: (
            int(amount or 0),
            int(pending or 0),
            int(abnormal or 0),
            int(manual or 0),
            int(closed or 0),
        )
        for tenant_id, amount, pending, abnormal, manual, closed in refund_rows
    }

    manual_rows = _rows_by_tenant(
        db,
        ManualFinancialTransaction.tenant_id,
        select(
            ManualFinancialTransaction.tenant_id,
            func.coalesce(
                func.sum(
                    case(
                        (ManualFinancialTransaction.kind == "receipt", ManualFinancialTransaction.amount_cents),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (ManualFinancialTransaction.kind == "refund", ManualFinancialTransaction.amount_cents),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .where(ManualFinancialTransaction.currency == _CURRENCY)
        .group_by(ManualFinancialTransaction.tenant_id),
        tenant_ids,
    )
    manual_financial = {
        tenant_id: (int(receipts or 0), int(refunds or 0))
        for tenant_id, receipts, refunds in manual_rows
    }

    subscription_rows = _rows_by_tenant(
        db,
        Subscription.tenant_id,
        select(Subscription, BillingPlan)
        .outerjoin(BillingPlan, BillingPlan.id == Subscription.plan_id),
        tenant_ids,
    )
    subscriptions = {
        subscription.tenant_id: (subscription, plan)
        for subscription, plan in subscription_rows
    }

    snapshots = []
    for tenant in tenant_rows:
        subscription, plan = subscriptions.get(tenant.id, (None, None))
        state, entitled = _subscription_state(subscription, now)
        account_count, active_account_count, last_active_at = accounts.get(
            tenant.id, (0, 0, None)
        )
        media_count, storage_bytes = media.get(tenant.id, (0, 0))
        provider_receipts, payment_exceptions = payments.get(tenant.id, (0, 0))
        (
            provider_refunds,
            refund_pending,
            refund_abnormal,
            refund_manual,
            refund_closed,
        ) = refunds.get(tenant.id, (0, 0, 0, 0, 0))
        manual_receipts, manual_refunds = manual_financial.get(tenant.id, (0, 0))
        snapshots.append(
            TenantSnapshot(
                tenant=tenant,
                plan=plan,
                subscription=subscription,
                subscription_status=state,
                is_entitled=entitled,
                admin_account_count=account_count,
                active_admin_account_count=active_account_count,
                observed_user_count=observed_users.get(tenant.id, 0),
                message_count=messages.get(tenant.id, 0),
                media_file_count=media_count,
                storage_bytes=storage_bytes,
                last_active_at=last_active_at,
                provider_receipt_cents=provider_receipts,
                provider_refund_cents=provider_refunds,
                manual_receipt_cents=manual_receipts,
                manual_refund_cents=manual_refunds,
                payment_exception_count=payment_exceptions,
                refund_pending_count=refund_pending,
                refund_abnormal_count=refund_abnormal,
                refund_manual_recovery_count=refund_manual,
                refund_closed_count=refund_closed,
            )
        )
    return snapshots


def _storage_values(snapshot: TenantSnapshot) -> tuple[int | None, int | None]:
    if not snapshot.is_entitled or snapshot.plan is None:
        return None, None
    quota_bytes = int(snapshot.plan.storage_quota_bytes)
    if quota_bytes <= 0:
        return quota_bytes, None
    return quota_bytes, snapshot.storage_bytes * 10_000 // quota_bytes


def _tenant_item(snapshot: TenantSnapshot) -> dict:
    quota_bytes, utilization = _storage_values(snapshot)
    return {
        "tenant_id": snapshot.tenant.id,
        "tenant_name": snapshot.tenant.name,
        "tenant_slug": snapshot.tenant.slug,
        "lifecycle_status": snapshot.tenant.lifecycle_status,
        "is_active": bool(snapshot.tenant.is_active),
        "created_at": snapshot.tenant.created_at,
        "subscription_plan_code": snapshot.plan.code if snapshot.plan else None,
        "subscription_plan_name": snapshot.plan.display_name if snapshot.plan else None,
        "subscription_stored_status": (
            snapshot.subscription.status if snapshot.subscription else None
        ),
        "subscription_status": snapshot.subscription_status,
        "subscription_starts_at": (
            _as_utc(snapshot.subscription.starts_at) if snapshot.subscription else None
        ),
        "subscription_ends_at": (
            _as_utc(snapshot.subscription.ends_at) if snapshot.subscription else None
        ),
        "subscription_grace_ends_at": (
            subscription_grace_ends_at(snapshot.subscription)
            if snapshot.subscription
            else None
        ),
        "cancel_at_period_end": (
            bool(snapshot.subscription.cancel_at_period_end)
            if snapshot.subscription
            else False
        ),
        "frozen_at": (
            _as_utc(snapshot.tenant.frozen_at) if snapshot.tenant.frozen_at else None
        ),
        "suspended_at": (
            _as_utc(snapshot.tenant.suspended_at)
            if snapshot.tenant.suspended_at
            else None
        ),
        "suspension_reason": snapshot.tenant.suspension_reason,
        "provider_receipt_cents": snapshot.provider_receipt_cents,
        "provider_refund_cents": snapshot.provider_refund_cents,
        "provider_net_revenue_cents": (
            snapshot.provider_receipt_cents - snapshot.provider_refund_cents
        ),
        "manual_receipt_cents": snapshot.manual_receipt_cents,
        "manual_refund_cents": snapshot.manual_refund_cents,
        "manual_net_cents": (
            snapshot.manual_receipt_cents - snapshot.manual_refund_cents
        ),
        "payment_exception_count": snapshot.payment_exception_count,
        "refund_pending_count": snapshot.refund_pending_count,
        "refund_abnormal_count": snapshot.refund_abnormal_count,
        "refund_manual_recovery_count": snapshot.refund_manual_recovery_count,
        "refund_closed_count": snapshot.refund_closed_count,
        "admin_account_count": snapshot.admin_account_count,
        "message_count": snapshot.message_count,
        "media_file_count": snapshot.media_file_count,
        "storage_bytes": snapshot.storage_bytes,
        "storage_quota_bytes": quota_bytes,
        "storage_utilization_basis_points": utilization,
    }


def _period_keys(now: datetime, months: int) -> list[str]:
    year, month = now.year, now.month
    keys = []
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return list(reversed(keys))


def _financial_summary(
    db: Session,
    *,
    now: datetime,
    months: int,
    tenant_id: str | None = None,
) -> dict:
    """Provider-confirmed receipts/refunds only; never mix the manual ledger."""
    paid_orders = select(PaymentOrder.paid_at, PaymentOrder.amount_cents).where(
        PaymentOrder.currency == _CURRENCY,
        PaymentOrder.status.in_(_PAID_ORDER_STATUSES),
        PaymentOrder.paid_at.is_not(None),
    )
    successful_refunds = select(
        RefundOrder.succeeded_at,
        RefundOrder.amount_cents,
    ).where(
        RefundOrder.currency == _CURRENCY,
        RefundOrder.status == "succeeded",
        RefundOrder.succeeded_at.is_not(None),
    )
    if tenant_id is not None:
        paid_orders = paid_orders.where(PaymentOrder.tenant_id == tenant_id)
        successful_refunds = successful_refunds.where(
            RefundOrder.tenant_id == tenant_id
        )

    receipts = 0
    refunds = 0
    period_values: dict[str, dict[str, int]] = defaultdict(
        lambda: {"receipt_cents": 0, "refund_cents": 0}
    )
    period_keys = set(_period_keys(now, months))

    def add(kind: str, occurred_at: datetime, amount_cents: int) -> None:
        nonlocal receipts, refunds
        amount = int(amount_cents or 0)
        if kind == "receipt":
            receipts += amount
        else:
            refunds += amount
        period = _as_utc(occurred_at).strftime("%Y-%m")
        if period in period_keys:
            period_values[period][f"{kind}_cents"] += amount

    for paid_at, amount_cents in db.execute(paid_orders).all():
        add("receipt", paid_at, amount_cents)
    for succeeded_at, amount_cents in db.execute(successful_refunds).all():
        add("refund", succeeded_at, amount_cents)

    periods = []
    for period in _period_keys(now, months):
        values = period_values[period]
        periods.append(
            {
                "period": period,
                "receipt_cents": values["receipt_cents"],
                "refund_cents": values["refund_cents"],
                "net_revenue_cents": values["receipt_cents"] - values["refund_cents"],
            }
        )
    return {
        "currency": _CURRENCY,
        "receipt_cents": receipts,
        "refund_cents": refunds,
        "net_revenue_cents": receipts - refunds,
        "periods": periods,
    }


def _manual_financial_summary(
    db: Session,
    *,
    tenant_id: str | None = None,
) -> dict:
    statement = select(
        ManualFinancialTransaction.kind,
        ManualFinancialTransaction.amount_cents,
    ).where(ManualFinancialTransaction.currency == _CURRENCY)
    if tenant_id is not None:
        statement = statement.where(
            ManualFinancialTransaction.tenant_id == tenant_id
        )
    receipts = 0
    refunds = 0
    for kind, amount_cents in db.execute(statement).all():
        if kind == "receipt":
            receipts += int(amount_cents)
        else:
            refunds += int(amount_cents)
    return {
        "currency": _CURRENCY,
        "receipt_cents": receipts,
        "refund_cents": refunds,
        "net_cents": receipts - refunds,
    }


def _exception_counts(db: Session, tenant_id: str | None = None) -> dict:
    payment = select(PaymentOrder.status, func.count(PaymentOrder.id)).where(
        PaymentOrder.status.in_(_PAYMENT_EXCEPTION_STATUSES)
    )
    refund = select(RefundOrder.status, func.count(RefundOrder.id)).where(
        RefundOrder.status.in_((*_REFUND_PENDING_STATUSES, *_REFUND_EXCEPTION_STATUSES))
    )
    if tenant_id is not None:
        payment = payment.where(PaymentOrder.tenant_id == tenant_id)
        refund = refund.where(RefundOrder.tenant_id == tenant_id)
    payment = payment.group_by(PaymentOrder.status)
    refund = refund.group_by(RefundOrder.status)
    payment_counts = {state: int(count) for state, count in db.execute(payment).all()}
    refund_counts = {state: int(count) for state, count in db.execute(refund).all()}
    return {
        "payment_activation_pending": payment_counts.get(
            "paid_activation_pending", 0
        ),
        "payment_failed": payment_counts.get("failed", 0),
        "refund_pending": sum(
            refund_counts.get(state, 0) for state in _REFUND_PENDING_STATUSES
        ),
        "refund_abnormal": refund_counts.get("abnormal", 0),
        "refund_closed": refund_counts.get("closed", 0),
        "refund_manual_recovery": refund_counts.get(
            "manual_recovery_required", 0
        ),
    }


def get_dashboard(db: Session, *, months: int = 12, at: datetime | None = None) -> dict:
    """Return the platform summary using only aggregate and metadata fields."""
    now = _as_utc(at or datetime.now(timezone.utc))
    tenants = db.scalars(select(Tenant)).all()
    snapshots = _tenant_snapshots(db, tenants, now=now)
    subscription_counts = Counter(snapshot.subscription_status for snapshot in snapshots)

    distribution = Counter(
        (
            snapshot.plan.code if snapshot.plan else None,
            snapshot.plan.display_name if snapshot.plan else None,
            snapshot.subscription_status,
        )
        for snapshot in snapshots
    )
    distribution_items = [
        {
            "plan_code": plan_code,
            "plan_name": plan_name,
            "status": status,
            "tenant_count": count,
        }
        for (plan_code, plan_name, status), count in sorted(
            distribution.items(), key=lambda item: ((item[0][0] or ""), item[0][2])
        )
    ]

    quota_risks = []
    for snapshot in snapshots:
        quota_bytes, utilization = _storage_values(snapshot)
        if quota_bytes is None or quota_bytes <= 0 or utilization is None or utilization < 8_000:
            continue
        state = "over_quota" if utilization > 10_000 else "at_quota" if utilization == 10_000 else "near_quota"
        quota_risks.append(
            {
                "tenant_id": snapshot.tenant.id,
                "tenant_name": snapshot.tenant.name,
                "plan_code": snapshot.plan.code,
                "quota_bytes": quota_bytes,
                "used_bytes": snapshot.storage_bytes,
                "utilization_basis_points": utilization,
                "state": state,
            }
        )
    quota_risks.sort(
        key=lambda item: (item["utilization_basis_points"], item["tenant_name"]), reverse=True
    )

    def date_sort_key(value: datetime | None) -> datetime:
        return _as_utc(value) if value is not None else datetime.min.replace(tzinfo=timezone.utc)

    recent_tenants = sorted(
        snapshots, key=lambda item: date_sort_key(item.tenant.created_at), reverse=True
    )[:5]
    recent_active = sorted(
        (item for item in snapshots if item.last_active_at is not None),
        key=lambda item: date_sort_key(item.last_active_at),
        reverse=True,
    )[:5]

    return {
        "tenant_counts": {
            "total": len(snapshots),
            "trial": subscription_counts["trial"],
            "active": subscription_counts["active"],
            "grace": subscription_counts["grace"],
            "expired": subscription_counts["expired"],
            "canceled": subscription_counts["canceled"],
            "frozen": sum(
                1 for snapshot in snapshots if snapshot.tenant.lifecycle_status == "frozen"
            ),
            "suspended": sum(
                1 for snapshot in snapshots if snapshot.tenant.lifecycle_status == "suspended"
            ),
        },
        "account_counts": {
            "admin_accounts": sum(snapshot.admin_account_count for snapshot in snapshots),
            "active_admin_accounts": sum(
                snapshot.active_admin_account_count for snapshot in snapshots
            ),
            "observed_users": sum(snapshot.observed_user_count for snapshot in snapshots),
        },
        "usage_totals": {
            "message_count": sum(snapshot.message_count for snapshot in snapshots),
            "media_file_count": sum(snapshot.media_file_count for snapshot in snapshots),
            "storage_bytes": sum(snapshot.storage_bytes for snapshot in snapshots),
        },
        "subscription_distribution": distribution_items,
        "revenue": _financial_summary(db, now=now, months=months),
        "manual_financial": _manual_financial_summary(db),
        "exceptions": _exception_counts(db),
        "recent_tenants": [
            {
                "tenant_id": snapshot.tenant.id,
                "tenant_name": snapshot.tenant.name,
                "tenant_slug": snapshot.tenant.slug,
                "occurred_at": _as_utc(snapshot.tenant.created_at),
            }
            for snapshot in recent_tenants
        ],
        "recently_active_tenants": [
            {
                "tenant_id": snapshot.tenant.id,
                "tenant_name": snapshot.tenant.name,
                "tenant_slug": snapshot.tenant.slug,
                "occurred_at": _as_utc(snapshot.last_active_at),
            }
            for snapshot in recent_active
        ],
        "storage_quota_risks": quota_risks,
        "measured_at": now,
    }


def list_tenants(
    db: Session,
    *,
    page: int,
    page_size: int,
    lifecycle_status: str | None = None,
    subscription_status: str | None = None,
    refund_status: str | None = None,
    exception_type: str | None = None,
    ends_from: datetime | None = None,
    ends_to: datetime | None = None,
    sort: str = "created_desc",
    at: datetime | None = None,
) -> dict:
    now = _as_utc(at or datetime.now(timezone.utc))
    statement = select(Tenant).outerjoin(
        Subscription, Subscription.tenant_id == Tenant.id
    )
    if lifecycle_status is not None:
        if lifecycle_status not in {"provisioning", "active", "frozen", "suspended"}:
            raise PlatformOperationsValidationError("invalid lifecycle filter")
        statement = statement.where(Tenant.lifecycle_status == lifecycle_status)
    if subscription_status is not None:
        if subscription_status == "not_subscribed":
            condition = Subscription.id.is_(None)
        elif subscription_status == "not_started":
            condition = Subscription.starts_at > now
        elif subscription_status in {"trial", "active"}:
            condition = and_(
                Subscription.status == subscription_status,
                Subscription.starts_at <= now,
                Subscription.ends_at > now,
            )
        elif subscription_status == "grace":
            condition = and_(
                Subscription.status.notin_(("expired", "canceled")),
                Subscription.ends_at <= now,
                Subscription.grace_ends_at > now,
            )
        elif subscription_status == "expired":
            condition = or_(
                Subscription.status == "expired",
                and_(
                    Subscription.status.notin_(("canceled",)),
                    Subscription.grace_ends_at <= now,
                ),
            )
        elif subscription_status == "canceled":
            condition = Subscription.status == "canceled"
        else:
            raise PlatformOperationsValidationError("invalid subscription filter")
        statement = statement.where(condition)
    if refund_status is not None:
        if refund_status == "pending":
            refund_states = _REFUND_PENDING_STATUSES
        elif refund_status in {
            "succeeded",
            "closed",
            "abnormal",
            "manual_recovery_required",
        }:
            refund_states = (refund_status,)
        else:
            raise PlatformOperationsValidationError("invalid refund filter")
        statement = statement.where(
            select(RefundOrder.id)
            .where(
                RefundOrder.tenant_id == Tenant.id,
                RefundOrder.status.in_(refund_states),
            )
            .exists()
        )
    payment_exception = (
        select(PaymentOrder.id)
        .where(
            PaymentOrder.tenant_id == Tenant.id,
            PaymentOrder.status.in_(_PAYMENT_EXCEPTION_STATUSES),
        )
        .exists()
    )
    refund_exception = (
        select(RefundOrder.id)
        .where(
            RefundOrder.tenant_id == Tenant.id,
            RefundOrder.status.in_(_REFUND_EXCEPTION_STATUSES),
        )
        .exists()
    )
    if exception_type == "payment":
        statement = statement.where(payment_exception)
    elif exception_type == "refund":
        statement = statement.where(refund_exception)
    elif exception_type == "any":
        statement = statement.where(or_(payment_exception, refund_exception))
    elif exception_type == "none":
        statement = statement.where(~payment_exception, ~refund_exception)
    elif exception_type is not None:
        raise PlatformOperationsValidationError("invalid exception filter")
    if ends_from is not None:
        statement = statement.where(Subscription.ends_at >= _as_utc(ends_from))
    if ends_to is not None:
        statement = statement.where(Subscription.ends_at <= _as_utc(ends_to))
    if ends_from is not None and ends_to is not None and _as_utc(ends_from) > _as_utc(ends_to):
        raise PlatformOperationsValidationError("invalid expiry range")

    orderings = {
        "created_desc": (Tenant.created_at.desc(), Tenant.id.desc()),
        "name_asc": (Tenant.name.asc(), Tenant.id.asc()),
        "ends_asc": (
            case((Subscription.ends_at.is_(None), 1), else_=0),
            Subscription.ends_at.asc(),
            Tenant.id.asc(),
        ),
        "ends_desc": (
            case((Subscription.ends_at.is_(None), 1), else_=0),
            Subscription.ends_at.desc(),
            Tenant.id.asc(),
        ),
    }
    if sort not in orderings:
        raise PlatformOperationsValidationError("invalid tenant sort")
    total = int(
        db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    )
    tenants = db.scalars(
        statement.order_by(*orderings[sort])
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [_tenant_item(snapshot) for snapshot in _tenant_snapshots(db, tenants, now=now)],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def _masked_ref(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if len(normalized) <= 6:
        return "••••••"
    return f"••••{normalized[-6:]}"


def _tenant_commercial_evidence(db: Session, tenant_id: str) -> dict:
    orders = db.scalars(
        select(PaymentOrder)
        .where(PaymentOrder.tenant_id == tenant_id)
        .order_by(PaymentOrder.created_at.desc(), PaymentOrder.id.desc())
        .limit(25)
    ).all()
    order_ids = [order.id for order in orders]
    payment_events: dict[str, PaymentEvent] = {}
    if order_ids:
        for event in db.scalars(
            select(PaymentEvent)
            .where(PaymentEvent.order_id.in_(order_ids))
            .order_by(PaymentEvent.created_at.desc(), PaymentEvent.id.desc())
        ).all():
            payment_events.setdefault(event.order_id, event)

    refunds = db.scalars(
        select(RefundOrder)
        .where(RefundOrder.tenant_id == tenant_id)
        .order_by(RefundOrder.requested_at.desc(), RefundOrder.id.desc())
        .limit(25)
    ).all()
    refund_ids = [refund.id for refund in refunds]
    refund_events: dict[str, RefundEvent] = {}
    if refund_ids:
        for event in db.scalars(
            select(RefundEvent)
            .where(RefundEvent.refund_order_id.in_(refund_ids))
            .order_by(RefundEvent.created_at.desc(), RefundEvent.id.desc())
        ).all():
            refund_events.setdefault(event.refund_order_id, event)

    manual_rows = db.scalars(
        select(ManualFinancialTransaction)
        .where(ManualFinancialTransaction.tenant_id == tenant_id)
        .order_by(
            ManualFinancialTransaction.occurred_at.desc(),
            ManualFinancialTransaction.id.desc(),
        )
        .limit(25)
    ).all()
    audit_rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(25)
    ).all()
    activation_rows = db.scalars(
        select(SubscriptionActivation)
        .where(
            SubscriptionActivation.tenant_id == tenant_id,
            SubscriptionActivation.status == "failed",
        )
        .order_by(
            SubscriptionActivation.created_at.desc(),
            SubscriptionActivation.id.desc(),
        )
        .limit(25)
    ).all()

    payment_items = []
    exceptions = []
    for order in orders:
        latest_event = payment_events.get(order.id)
        item = {
            "order_id": order.id,
            "plan_name": order.plan_name,
            "amount_cents": order.amount_cents,
            "currency": order.currency,
            "provider": order.provider,
            "status": order.status,
            "provider_state": order.provider_state,
            "provider_order_ref_masked": _masked_ref(order.provider_order_ref),
            "provider_transaction_ref_masked": _masked_ref(
                order.provider_transaction_id
            ),
            "latest_event_id_masked": _masked_ref(
                latest_event.provider_event_id if latest_event else None
            ),
            "failure_code": order.failure_code,
            "created_at": _as_utc(order.created_at),
            "paid_at": _as_utc(order.paid_at) if order.paid_at else None,
        }
        payment_items.append(item)
        if order.status in _PAYMENT_EXCEPTION_STATUSES:
            exceptions.append(
                {
                    "kind": "payment",
                    "subject_id": order.id,
                    "status": order.status,
                    "failure_code": order.failure_code,
                    "occurred_at": _as_utc(order.updated_at or order.created_at),
                }
            )

    refund_items = []
    for refund in refunds:
        latest_event = refund_events.get(refund.id)
        item = {
            "refund_id": refund.id,
            "payment_order_id": refund.payment_order_id,
            "amount_cents": refund.amount_cents,
            "currency": refund.currency,
            "provider": refund.provider,
            "status": refund.status,
            "provider_state": refund.provider_state,
            "provider_ref_masked": _masked_ref(refund.provider_ref),
            "provider_refund_id_masked": _masked_ref(refund.provider_refund_id),
            "latest_event_id_masked": _masked_ref(
                latest_event.provider_event_id if latest_event else None
            ),
            "reason_code": refund.reason_code,
            "failure_code": refund.failure_code,
            "requested_at": _as_utc(refund.requested_at),
            "succeeded_at": (
                _as_utc(refund.succeeded_at) if refund.succeeded_at else None
            ),
        }
        refund_items.append(item)
        if refund.status in _REFUND_EXCEPTION_STATUSES:
            exceptions.append(
                {
                    "kind": "refund",
                    "subject_id": refund.id,
                    "status": refund.status,
                    "failure_code": refund.failure_code,
                    "occurred_at": _as_utc(refund.updated_at or refund.requested_at),
                }
            )
    for activation in activation_rows:
        exceptions.append(
            {
                "kind": "activation",
                "subject_id": activation.id,
                "status": activation.status,
                "failure_code": activation.failure_code,
                "occurred_at": _as_utc(
                    activation.updated_at or activation.created_at
                ),
            }
        )
    exceptions.sort(key=lambda item: item["occurred_at"], reverse=True)

    return {
        "payment_orders": payment_items,
        "refund_orders": refund_items,
        "manual_financial_transactions": [
            {
                "transaction_id": row.id,
                "kind": row.kind,
                "amount_cents": row.amount_cents,
                "currency": row.currency,
                "occurred_at": _as_utc(row.occurred_at),
                "reference_masked": _masked_ref(row.reference),
            }
            for row in manual_rows
        ],
        "recent_exceptions": exceptions[:25],
        "audit_events": [
            {
                "audit_id": row.id,
                "action": row.action,
                "object_type": row.object_type,
                "object_id": row.object_id,
                "created_at": _as_utc(row.created_at),
            }
            for row in audit_rows
        ],
    }


def get_tenant_detail(
    db: Session,
    tenant_id: str,
    *,
    at: datetime | None = None,
) -> dict:
    now = _as_utc(at or datetime.now(timezone.utc))
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise PlatformOperationsNotFoundError("tenant does not exist")
    snapshot = _tenant_snapshots(db, [tenant], now=now)[0]
    detail = _tenant_item(snapshot)
    detail.update(
        {
            "active_admin_account_count": snapshot.active_admin_account_count,
            "observed_user_count": snapshot.observed_user_count,
            "subscription_source": snapshot.subscription.source if snapshot.subscription else None,
            "subscription_renewal_count": (
                snapshot.subscription.renewal_count if snapshot.subscription else 0
            ),
        }
    )
    revenue = _financial_summary(db, now=now, months=12, tenant_id=tenant_id)
    detail.update(
        {
            # Compatibility aliases now have one unambiguous meaning:
            # provider-confirmed money only, never the manual ledger.
            "receipt_cents": revenue["receipt_cents"],
            "refund_cents": revenue["refund_cents"],
            "net_revenue_cents": revenue["net_revenue_cents"],
            "manual_financial": _manual_financial_summary(
                db, tenant_id=tenant_id
            ),
            "exceptions": _exception_counts(db, tenant_id=tenant_id),
        }
    )
    detail.update(_tenant_commercial_evidence(db, tenant_id))
    return detail


def _idempotency_hash(value: str) -> str:
    normalized = value.strip()
    if len(normalized) < 16 or len(normalized) > 256:
        raise PlatformOperationsValidationError("invalid idempotency key")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def authorize_platform_operation(
    db: Session,
    *,
    tenant_id: str,
    platform_admin_id: str,
    action: str,
    target_id: str | None,
    reason_code: str,
    confirmation: str,
    idempotency_key: str,
) -> bool:
    """Authorize and durably deduplicate one high-risk platform command.

    Returns ``True`` only for an exact replay. The tenant row lock serializes
    concurrent first use of the same key; only key/command hashes are audited.
    """
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    if tenant is None:
        raise PlatformOperationsNotFoundError("tenant does not exist")
    if action not in _CONTROL_ACTIONS:
        raise PlatformOperationsValidationError("invalid control action")
    normalized_reason = reason_code.strip().lower()
    if not _SAFE_REASON_CODE.fullmatch(normalized_reason):
        raise PlatformOperationsValidationError("invalid operation reason")
    if confirmation.strip() != tenant.slug:
        raise PlatformOperationsValidationError("operation confirmation mismatch")
    key_hash = _idempotency_hash(idempotency_key)
    command_hash = hashlib.sha256(
        json.dumps(
            {
                "tenant_id": tenant.id,
                "platform_admin_id": platform_admin_id,
                "action": action,
                "target_id": target_id,
                "reason_code": normalized_reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    prior = db.scalars(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant.id,
            AuditLog.action == _CONTROL_AUDIT_ACTION,
            AuditLog.object_type == AuditObjectType.TENANT,
            AuditLog.object_id == tenant.id,
        )
    ).all()
    for row in prior:
        detail = row.detail if isinstance(row.detail, dict) else {}
        if detail.get("idempotency_key_hash") != key_hash:
            continue
        if detail.get("command_hash") != command_hash:
            raise PlatformOperationsConflictError(
                "idempotency key was already used for another operation"
            )
        return True
    if not write_audit(
        db,
        tenant_id=tenant.id,
        admin_user_id=None,
        action=_CONTROL_AUDIT_ACTION,
        object_type=AuditObjectType.TENANT,
        object_id=tenant.id,
        detail={
            "platform_admin_id": platform_admin_id,
            "operation": action,
            "target_id": target_id,
            "reason_code": normalized_reason,
            "idempotency_key_hash": key_hash,
            "command_hash": command_hash,
        },
    ):
        raise PlatformOperationsValidationError("operation audit failed")
    db.flush()
    return False


def set_service_status(
    db: Session,
    tenant_id: str,
    lifecycle_status: str,
    *,
    platform_admin_id: str,
    reason_code: str,
    confirmation: str,
    idempotency_key: str,
) -> Tenant:
    if lifecycle_status not in {"active", "suspended"}:
        raise PlatformOperationsValidationError("unsupported service status")
    replay = authorize_platform_operation(
        db,
        tenant_id=tenant_id,
        platform_admin_id=platform_admin_id,
        action=(
            "service.suspend" if lifecycle_status == "suspended" else "service.resume"
        ),
        target_id=tenant_id,
        reason_code=reason_code,
        confirmation=confirmation,
        idempotency_key=idempotency_key,
    )
    if replay:
        tenant = db.get(Tenant, tenant_id)
        if tenant is None:
            raise PlatformOperationsNotFoundError("tenant does not exist")
        return tenant
    try:
        if lifecycle_status == "suspended":
            suspend_tenant_service(
                db,
                tenant_id,
                platform_admin_id=platform_admin_id,
                reason_code=reason_code,
            )
        else:
            resume_tenant_service(
                db,
                tenant_id,
                platform_admin_id=platform_admin_id,
            )
    except BillingLifecycleNotFoundError as error:
        raise PlatformOperationsNotFoundError(str(error)) from error
    except BillingLifecycleError as error:
        raise PlatformOperationsValidationError(str(error)) from error
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:  # Defensive: lifecycle service just locked this row.
        raise PlatformOperationsNotFoundError("tenant does not exist")
    return tenant


def update_subscription(
    db: Session,
    *,
    tenant_id: str,
    plan_code: str,
    status: str,
    starts_at: datetime,
    ends_at: datetime,
):
    if db.get(Tenant, tenant_id) is None:
        raise PlatformOperationsNotFoundError("tenant does not exist")
    current = db.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id))
    renewal_count = current.renewal_count if current is not None else 0
    if current is not None and _as_utc(ends_at) > _as_utc(current.ends_at):
        renewal_count += 1
    try:
        subscription = assign_subscription(
            db,
            tenant_id=tenant_id,
            plan_code=plan_code,
            status=status,
            starts_at=starts_at,
            ends_at=ends_at,
            source="platform_manual",
            renewal_count=renewal_count,
        )
    except (PlanUnavailableError, SubscriptionAssignmentError) as error:
        raise PlatformOperationsValidationError(str(error)) from error
    plan = db.get(BillingPlan, subscription.plan_id)
    if plan is None:  # Defensive; assign_subscription already loads an active plan.
        raise PlatformOperationsValidationError("plan is unavailable")
    return subscription, plan


def record_manual_financial_transaction(
    db: Session,
    *,
    tenant_id: str,
    platform_admin_id: str,
    kind: str,
    amount_cents: int,
    occurred_at: datetime | None,
    reference: Optional[str],
    note: Optional[str],
    at: datetime | None = None,
) -> ManualFinancialTransaction:
    if db.get(Tenant, tenant_id) is None:
        raise PlatformOperationsNotFoundError("tenant does not exist")
    if kind not in {"receipt", "refund"} or amount_cents <= 0:
        raise PlatformOperationsValidationError("invalid financial transaction")
    now = _as_utc(at or datetime.now(timezone.utc))
    happened_at = _as_utc(occurred_at or now)
    if happened_at > now:
        raise PlatformOperationsValidationError("financial transaction cannot be future dated")
    transaction = ManualFinancialTransaction(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        kind=kind,
        amount_cents=amount_cents,
        currency=_CURRENCY,
        occurred_at=happened_at,
        reference=reference.strip() if reference else None,
        note=note.strip() if note else None,
        recorded_by_platform_admin_id=platform_admin_id,
    )
    db.add(transaction)
    db.flush()
    return transaction
