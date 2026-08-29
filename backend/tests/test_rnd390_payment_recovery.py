from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AdminUser,
    AuditLog,
    BillingPlan,
    PaymentEvent,
    PaymentOrder,
    PaymentRecoveryFinding,
    PlanEntitlement,
    Subscription,
    SubscriptionActivation,
    SubscriptionHistory,
    SubscriptionTermGrant,
    Tenant,
)
from app.services.entitlements import ANNUAL_PLAN_CODE
from app.services.payment_orders import CreateOrderCommand, create_payment_order
from app.services.payment_provider import CheckoutArtifact, PaymentQueryResult
from app.services.payment_recovery import (
    FINDING_CALLBACK_SIGNATURE_FAILURE,
    FINDING_QUERY_FAILED,
    MAX_QUERY_ATTEMPTS,
    record_callback_failure,
    run_payment_reconciliation_once,
    run_payment_recovery_once,
)
from app.services.wechat_pay import WECHAT_PAY_PROVIDER

NOW = datetime(2028, 1, 30, 8, 0, tzinfo=timezone.utc)
PLAN_ID = "rnd390-plan"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


class FakeProvider:
    code = WECHAT_PAY_PROVIDER
    app_id = "wx-rnd390"
    merchant_id = "1900003901"

    def __init__(self) -> None:
        self.query_result: PaymentQueryResult | None = None
        self.query_error: Exception | None = None
        self.closed: list[str] = []

    def create_payment(self, request):
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="qr_code",
            value="weixin://wxpay/bizpayurl?pr=rnd390-order",
        )

    def verify_and_parse_notification(self, headers, raw_body):
        raise NotImplementedError

    def query_payment(self, provider_order_ref: str) -> PaymentQueryResult:
        if self.query_error is not None:
            raise self.query_error
        assert self.query_result is not None
        return replace(self.query_result, provider_order_ref=provider_order_ref)

    def close_payment(self, provider_order_ref: str) -> None:
        self.closed.append(provider_order_ref)


def _tables():
    return [
        Tenant.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        SubscriptionHistory.__table__,
        SubscriptionActivation.__table__,
        PaymentOrder.__table__,
        PaymentEvent.__table__,
        SubscriptionTermGrant.__table__,
        AdminUser.__table__,
        AuditLog.__table__,
        PaymentRecoveryFinding.__table__,
    ]


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_tables())
    result = sessionmaker(bind=engine, expire_on_commit=False)
    with result() as db:
        db.add_all(
            [
                Tenant(id="tenant-a", name="A", slug="tenant-a", lifecycle_status="active"),
                BillingPlan(
                    id=PLAN_ID,
                    code=ANNUAL_PLAN_CODE,
                    display_name="年度基础套餐",
                    is_active=True,
                    amount_cents=9900,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=5 * 1024**3,
                ),
            ]
        )
        db.commit()
    return result


def _create_order(factory, provider: FakeProvider, *, at: datetime, key: str):
    return create_payment_order(
        factory,
        provider,
        CreateOrderCommand(
            tenant_id="tenant-a",
            plan_code=ANNUAL_PLAN_CODE,
            idempotency_key=key,
            now=at,
        ),
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def test_recovery_retries_verified_pending_order_with_bounded_backoff(factory) -> None:
    provider = FakeProvider()
    created = _create_order(factory, provider, at=NOW, key="rnd390-pending-key")
    provider.query_result = PaymentQueryResult(
        provider=WECHAT_PAY_PROVIDER,
        provider_order_ref="placeholder",
        state="NOTPAY",
        status="pending",
        success=None,
    )

    checked_at = NOW + timedelta(minutes=3)
    summary = run_payment_recovery_once(factory, provider, at=checked_at)

    assert (summary.claimed, summary.pending, summary.recovered, summary.failed) == (1, 1, 0, 0)
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        assert order is not None
        assert order.recovery_state == "automatic"
        assert order.query_attempt_count == 1
        assert _utc(order.last_query_at) == checked_at
        assert _utc(order.next_query_at) == checked_at + timedelta(minutes=5)
        assert db.scalars(select(PaymentRecoveryFinding)).all() == []


def test_recovery_exhaustion_stops_automatic_queries_and_records_sanitized_finding(factory) -> None:
    provider = FakeProvider()
    created = _create_order(factory, provider, at=NOW, key="rnd390-query-failure")
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        assert order is not None
        order.query_attempt_count = MAX_QUERY_ATTEMPTS - 1
        order.next_query_at = NOW + timedelta(minutes=1)
        db.commit()
    provider.query_error = RuntimeError("provider must not leak through recovery")

    summary = run_payment_recovery_once(factory, provider, at=NOW + timedelta(minutes=1))

    assert (summary.claimed, summary.manual_recovery, summary.failed) == (1, 1, 0)
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        finding = db.scalar(
            select(PaymentRecoveryFinding).where(
                PaymentRecoveryFinding.payment_order_id == created.order_id
            )
        )
        assert order is not None and finding is not None
        assert order.recovery_state == "manual_recovery"
        assert order.recovery_reason_code == FINDING_QUERY_FAILED
        assert order.next_query_at is None
        assert finding.kind == FINDING_QUERY_FAILED
        assert finding.severity == "critical"
        assert finding.status == "open"
        assert finding.occurrence_count == 1


def test_daily_reconciliation_marks_terminal_channel_check_without_reopening_checkout(factory) -> None:
    provider = FakeProvider()
    created = _create_order(
        factory,
        provider,
        at=NOW - timedelta(days=2),
        key="rnd390-reconciliation",
    )
    provider.query_result = PaymentQueryResult(
        provider=WECHAT_PAY_PROVIDER,
        provider_order_ref="placeholder",
        state="CLOSED",
        status="closed",
        success=None,
    )

    summary = run_payment_reconciliation_once(factory, provider, at=NOW)

    assert (summary.claimed, summary.recovered, summary.pending) == (1, 1, 0)
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        assert order is not None
        assert order.status == "closed"
        assert order.recovery_state == "not_required"
        assert _utc(order.last_reconciled_at) == NOW
        assert order.checkout_url is None


def test_untrusted_callback_signature_failures_do_not_create_recovery_findings(factory) -> None:
    record_callback_failure(
        factory,
        kind=FINDING_CALLBACK_SIGNATURE_FAILURE,
        at=NOW,
    )
    record_callback_failure(
        factory,
        kind=FINDING_CALLBACK_SIGNATURE_FAILURE,
        at=NOW + timedelta(minutes=1),
    )

    with factory() as db:
        assert db.scalars(select(PaymentRecoveryFinding)).all() == []
