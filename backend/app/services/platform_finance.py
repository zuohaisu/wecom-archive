"""Read-only platform finance order listing for the super-admin console.

Backs the 订单明细 page. Confirmed-money semantics mirror
``platform_operations._financial_summary`` exactly: receipts are orders in
``paid_activation_pending``/``succeeded`` counted at ``paid_at``, refunds are
``succeeded`` refunds counted at ``succeeded_at``. The manual ledger
(``manual_financial_transactions``) is intentionally NOT part of this
listing — it never mixes with provider facts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PaymentOrder, RefundOrder, Tenant

CURRENCY = "CNY"

RECEIPT_STATUSES = ("creating", "pending", "paid_activation_pending", "succeeded", "closed", "failed")
REFUND_STATUSES = ("created", "processing", "succeeded", "closed", "abnormal", "manual_recovery_required")
CONFIRMED_RECEIPT_STATUSES = ("paid_activation_pending", "succeeded")
KINDS = ("all", "receipt", "refund")


class PlatformFinanceValidationError(ValueError):
    """Raised for an invalid order-list filter."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _receipt_rows(db: Session, q: Optional[str], status: Optional[str]) -> list[dict]:
    statement = select(PaymentOrder, Tenant.name).join(Tenant, PaymentOrder.tenant_id == Tenant.id).where(
        PaymentOrder.currency == CURRENCY
    )
    if q:
        statement = statement.where(
            Tenant.name.ilike(f"%{q}%") | Tenant.slug.ilike(f"%{q}%")
        )
    if status:
        statement = statement.where(PaymentOrder.status == status)
    rows = []
    for order, tenant_name in db.execute(statement).all():
        paid_at = _as_utc(order.paid_at) if order.paid_at is not None else None
        rows.append(
            {
                "kind": "receipt",
                "order_id": order.id,
                "tenant_id": order.tenant_id,
                "tenant_name": tenant_name,
                "amount_cents": int(order.amount_cents),
                "currency": order.currency,
                "provider": order.provider,
                "status": order.status,
                "plan_name": order.plan_name,
                "reason_code": None,
                "occurred_at": paid_at or _as_utc(order.created_at),
                "created_at": _as_utc(order.created_at),
            }
        )
    return rows


def _refund_rows(db: Session, q: Optional[str], status: Optional[str]) -> list[dict]:
    statement = select(RefundOrder, Tenant.name).join(Tenant, RefundOrder.tenant_id == Tenant.id).where(
        RefundOrder.currency == CURRENCY
    )
    if q:
        statement = statement.where(
            Tenant.name.ilike(f"%{q}%") | Tenant.slug.ilike(f"%{q}%")
        )
    if status:
        statement = statement.where(RefundOrder.status == status)
    rows = []
    for refund, tenant_name in db.execute(statement).all():
        succeeded_at = _as_utc(refund.succeeded_at) if refund.succeeded_at is not None else None
        rows.append(
            {
                "kind": "refund",
                "order_id": refund.id,
                "tenant_id": refund.tenant_id,
                "tenant_name": tenant_name,
                "amount_cents": int(refund.amount_cents),
                "currency": refund.currency,
                "provider": refund.provider,
                "status": refund.status,
                "plan_name": None,
                "reason_code": refund.reason_code,
                "occurred_at": succeeded_at or _as_utc(refund.requested_at),
                "created_at": _as_utc(refund.requested_at),
            }
        )
    return rows


def list_orders(
    db: Session,
    *,
    kind: str = "all",
    status: Optional[str] = None,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
) -> dict:
    """Unified, newest-first order listing with a filter-scoped money summary."""
    if kind not in KINDS:
        raise PlatformFinanceValidationError("invalid_kind_filter")
    if status:
        allowed = set(RECEIPT_STATUSES) if kind == "receipt" else set(REFUND_STATUSES) if kind == "refund" else set(RECEIPT_STATUSES) | set(REFUND_STATUSES)
        if status not in allowed:
            raise PlatformFinanceValidationError("invalid_status_filter")
    rows: list[dict] = []
    if kind in ("all", "receipt"):
        rows.extend(_receipt_rows(db, q, status))
    if kind in ("all", "refund"):
        rows.extend(_refund_rows(db, q, status))
    rows.sort(key=lambda row: (row["occurred_at"], row["created_at"]), reverse=True)

    receipt_cents = sum(r["amount_cents"] for r in rows if r["kind"] == "receipt" and r["status"] in CONFIRMED_RECEIPT_STATUSES)
    refund_cents = sum(r["amount_cents"] for r in rows if r["kind"] == "refund" and r["status"] == "succeeded")
    summary = {
        "currency": CURRENCY,
        "receipt_cents": receipt_cents,
        "receipt_count": sum(1 for r in rows if r["kind"] == "receipt" and r["status"] in CONFIRMED_RECEIPT_STATUSES),
        "refund_cents": refund_cents,
        "refund_count": sum(1 for r in rows if r["kind"] == "refund" and r["status"] == "succeeded"),
        "net_cents": receipt_cents - refund_cents,
    }

    total = len(rows)
    start = (page - 1) * page_size
    return {
        "kind": kind,
        "total": total,
        "page": page,
        "page_size": page_size,
        "summary": summary,
        "items": rows[start : start + page_size],
    }
