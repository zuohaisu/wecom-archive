"""Application orchestration for reviewed WeChat full refunds.

This module owns provider I/O ordering only. The provider adapter authenticates
WeChat facts; ``app.services.refunds`` remains the sole authority that changes
Refund, Subscription, Tenant service state and Audit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PaymentOrder, RefundOrder
from app.services.payment_provider import (
    PaymentProvider,
    RefundRequest,
    TrustedRefundEvent,
)
from app.services.refunds import (
    CreateRefundCommand,
    RefundConflictError,
    RefundNotFoundError,
    RefundSummary,
    apply_trusted_refund_event,
    create_refund_request,
    mark_refund_processing,
    reserve_refund_provider_ref,
)


SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class SubmitWechatRefundCommand:
    tenant_id: str
    payment_order_id: str
    idempotency_key: str
    reason_code: str
    approved_by_platform_admin_id: str
    requested_at: datetime


def _provider_ref(refund_id: str) -> str:
    # Stable across retries/crashes, ASCII-only and within WeChat's 64-byte
    # out_refund_no limit. It contains no tenant or customer identifier.
    return "R" + refund_id.replace("-", "")


def _provider_reason(reason_code: str) -> str:
    return {
        "customer_request": "客户申请全额退款",
        "duplicate_payment": "重复支付全额退款",
        "service_failure": "服务异常全额退款",
    }.get(reason_code, "经审核的全额退款")


def _validate_provider_event(
    provider: PaymentProvider, event: TrustedRefundEvent
) -> None:
    if event.provider != provider.code or event.merchant_id != provider.merchant_id:
        raise RefundConflictError("refund provider identity mismatch")


def submit_wechat_refund(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    command: SubmitWechatRefundCommand,
) -> RefundSummary:
    """Create/replay one reviewed request and submit it with a stable ref."""
    with session_factory() as db:
        summary = create_refund_request(
            db,
            CreateRefundCommand(
                tenant_id=command.tenant_id,
                payment_order_id=command.payment_order_id,
                idempotency_key=command.idempotency_key,
                reason_code=command.reason_code,
                approved_by_platform_admin_id=command.approved_by_platform_admin_id,
                requested_at=command.requested_at,
            ),
        )
        db.commit()
        if summary.status != "created":
            return summary
        refund_id = summary.refund_id

    stable_ref = _provider_ref(refund_id)
    with session_factory() as db:
        reserved = reserve_refund_provider_ref(
            db,
            command.tenant_id,
            refund_id,
            provider_ref=stable_ref,
        )
        refund = db.get(RefundOrder, refund_id)
        payment = db.get(PaymentOrder, command.payment_order_id)
        if refund is None or payment is None:
            raise RefundNotFoundError("refund payment context disappeared")
        if (
            refund.provider != provider.code
            or payment.provider != provider.code
            or payment.provider_transaction_id is None
            or reserved.amount_cents != payment.amount_cents
            or reserved.currency != payment.currency
        ):
            raise RefundConflictError("refund provider context mismatch")
        request = RefundRequest(
            provider_ref=stable_ref,
            provider_order_ref=payment.provider_order_ref,
            provider_transaction_id=payment.provider_transaction_id,
            amount_cents=refund.amount_cents,
            total_amount_cents=payment.amount_cents,
            currency=payment.currency,
            reason=_provider_reason(refund.reason_code),
        )
        db.commit()

    submission = provider.create_refund(request)
    if (
        submission.provider != provider.code
        or submission.provider_ref != request.provider_ref
        or submission.provider_order_ref != request.provider_order_ref
        or submission.provider_transaction_id != request.provider_transaction_id
        or submission.amount_cents != request.amount_cents
        or submission.total_amount_cents != request.total_amount_cents
        or submission.currency != request.currency
    ):
        raise RefundConflictError("refund submission result mismatch")

    with session_factory() as db:
        result = mark_refund_processing(
            db,
            command.tenant_id,
            refund_id,
            provider_ref=stable_ref,
            provider_refund_id=submission.provider_refund_id,
            accepted_at=submission.accepted_at,
        )
        db.commit()
        return result


def apply_wechat_refund_event(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    event: TrustedRefundEvent,
) -> RefundSummary:
    """Resolve a signed provider fact without accepting tenant input."""
    _validate_provider_event(provider, event)
    with session_factory() as db:
        refund = db.scalar(
            select(RefundOrder).where(
                RefundOrder.provider == event.provider,
                RefundOrder.provider_ref == event.provider_ref,
            )
        )
        if refund is None:
            raise RefundNotFoundError("refund order does not exist")
        result = apply_trusted_refund_event(db, refund.id, event)
        db.commit()
        return result


def query_and_reconcile_wechat_refund(
    session_factory: SessionFactory,
    provider: PaymentProvider,
    tenant_id: str,
    refund_id: str,
) -> RefundSummary:
    """Recover a missed callback through WeChat's signed query response."""
    with session_factory() as db:
        refund = db.scalar(
            select(RefundOrder).where(
                RefundOrder.id == refund_id,
                RefundOrder.tenant_id == tenant_id,
            )
        )
        if refund is None:
            raise RefundNotFoundError("refund order does not exist")
        if refund.provider != provider.code or not refund.provider_ref:
            raise RefundConflictError("refund is not queryable")
        provider_ref = refund.provider_ref
    event = provider.query_refund(provider_ref)
    return apply_wechat_refund_event(session_factory, provider, event)
