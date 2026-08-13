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
import uuid

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AdminUser,
    ArchiveMessage,
    BillingPlan,
    Contact,
    ManualFinancialTransaction,
    MediaFile,
    PaymentOrder,
    Subscription,
    Tenant,
)
from app.services.entitlements import (
    PlanUnavailableError,
    SubscriptionAssignmentError,
    assign_subscription,
)

_CURRENCY = "CNY"
_ENTITLED_STATUSES = frozenset({"trial", "active"})
_PAID_ORDER_STATUSES = ("paid_activation_pending", "succeeded")


class PlatformOperationsNotFoundError(LookupError):
    """Raised when a requested tenant does not exist."""


class PlatformOperationsValidationError(ValueError):
    """Raised for an invalid manual operations command."""


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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _subscription_state(subscription: Subscription | None, now: datetime) -> tuple[str, bool]:
    if subscription is None:
        return "not_subscribed", False
    if subscription.status == "canceled":
        return "canceled", False
    starts_at = _as_utc(subscription.starts_at)
    ends_at = _as_utc(subscription.ends_at)
    if now < starts_at:
        return "not_started", False
    if subscription.status in _ENTITLED_STATUSES and now >= ends_at:
        return "expired", False
    return subscription.status, subscription.status in _ENTITLED_STATUSES


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
        "subscription_status": snapshot.subscription_status,
        "subscription_ends_at": (
            _as_utc(snapshot.subscription.ends_at) if snapshot.subscription else None
        ),
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
    paid_orders = select(PaymentOrder.paid_at, PaymentOrder.amount_cents).where(
        PaymentOrder.currency == _CURRENCY,
        PaymentOrder.status.in_(_PAID_ORDER_STATUSES),
        PaymentOrder.paid_at.is_not(None),
    )
    manual_transactions = select(
        ManualFinancialTransaction.kind,
        ManualFinancialTransaction.occurred_at,
        ManualFinancialTransaction.amount_cents,
    ).where(ManualFinancialTransaction.currency == _CURRENCY)
    if tenant_id is not None:
        paid_orders = paid_orders.where(PaymentOrder.tenant_id == tenant_id)
        manual_transactions = manual_transactions.where(
            ManualFinancialTransaction.tenant_id == tenant_id
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
    for kind, occurred_at, amount_cents in db.execute(manual_transactions).all():
        add(kind, occurred_at, amount_cents)

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
            "expired": subscription_counts["expired"],
            "canceled": subscription_counts["canceled"],
            "past_due": subscription_counts["past_due"],
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
    at: datetime | None = None,
) -> dict:
    now = _as_utc(at or datetime.now(timezone.utc))
    total = int(db.scalar(select(func.count(Tenant.id))) or 0)
    tenants = db.scalars(
        select(Tenant)
        .order_by(Tenant.created_at.desc(), Tenant.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [_tenant_item(snapshot) for snapshot in _tenant_snapshots(db, tenants, now=now)],
        "page": page,
        "page_size": page_size,
        "total": total,
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
            "subscription_starts_at": (
                _as_utc(snapshot.subscription.starts_at) if snapshot.subscription else None
            ),
            "subscription_source": snapshot.subscription.source if snapshot.subscription else None,
            "subscription_renewal_count": (
                snapshot.subscription.renewal_count if snapshot.subscription else 0
            ),
        }
    )
    revenue = _financial_summary(db, now=now, months=12, tenant_id=tenant_id)
    detail.update(
        {
            "receipt_cents": revenue["receipt_cents"],
            "refund_cents": revenue["refund_cents"],
            "net_revenue_cents": revenue["net_revenue_cents"],
        }
    )
    return detail


def set_service_status(db: Session, tenant_id: str, lifecycle_status: str) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise PlatformOperationsNotFoundError("tenant does not exist")
    if lifecycle_status not in {"active", "suspended"}:
        raise PlatformOperationsValidationError("unsupported service status")
    tenant.lifecycle_status = lifecycle_status
    tenant.is_active = lifecycle_status == "active"
    db.flush()
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
