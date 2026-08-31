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
from app.services.wechat_pay import (
    WECHAT_PAY_PROVIDER,
    WechatPayProtocolError,
    WechatPayProviderResponseError,
)

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
        self.query_calls = 0
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
        self.query_calls += 1
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


def _create_failed_order(factory, provider: FakeProvider, *, at: datetime, key: str):
    created = _create_order(factory, provider, at=at, key=key)
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        assert order is not None
        order.status = "failed"
        order.failure_code = "provider_create_failed"
        order.checkout_url = None
        db.commit()
    return created


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


def test_valid_order_not_exist_converges_failed_create_without_retry_or_payment_mutation(factory) -> None:
    provider = FakeProvider()
    created = _create_failed_order(
        factory, provider, at=NOW, key="gh111-order-not-exist"
    )
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        assert order is not None
        order.query_attempt_count = 4
        db.add(
            PaymentRecoveryFinding(
                id="gh111-existing-query-finding",
                provider=WECHAT_PAY_PROVIDER,
                tenant_id="tenant-a",
                payment_order_id=order.id,
                kind=FINDING_QUERY_FAILED,
                severity="warning",
                status="open",
                dedupe_key="1" * 64,
                occurrence_count=1,
                first_detected_at=NOW,
                last_detected_at=NOW,
            )
        )
        db.commit()
    provider.query_result = PaymentQueryResult(
        provider=WECHAT_PAY_PROVIDER,
        provider_order_ref="placeholder",
        state="ORDER_NOT_EXIST",
        status="not_required",
        success=None,
    )

    summary = run_payment_recovery_once(factory, provider, at=NOW + timedelta(minutes=2))
    again = run_payment_recovery_once(factory, provider, at=NOW + timedelta(days=1))
    reconciliation = run_payment_reconciliation_once(
        factory, provider, at=NOW + timedelta(days=1)
    )

    assert (summary.claimed, summary.recovered, summary.manual_recovery, summary.failed) == (1, 1, 0, 0)
    assert again.claimed == reconciliation.claimed == 0
    assert provider.query_calls == 1
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        finding = db.get(PaymentRecoveryFinding, "gh111-existing-query-finding")
        assert order is not None and finding is not None
        assert order.status == "failed"
        assert order.failure_code == "provider_create_failed"
        assert order.provider_state == "ORDER_NOT_EXIST"
        assert order.recovery_state == "not_required"
        assert order.recovery_reason_code is None
        assert order.query_attempt_count == 4
        assert order.next_query_at is None
        assert finding.status == "resolved"
        assert db.query(PaymentEvent).count() == 0
        assert db.query(Subscription).count() == 0
        assert db.query(PlanEntitlement).count() == 0
        assert db.query(SubscriptionTermGrant).count() == 0


@pytest.mark.parametrize(
    "query_error",
    [
        WechatPayProviderResponseError(404, None),
        WechatPayProviderResponseError(404, "UNEXPECTED_PROVIDER_CODE"),
        WechatPayProviderResponseError(500, "SYSTEM_ERROR"),
        WechatPayProtocolError("WeChat Pay request failed"),
    ],
)
def test_unknown_or_transient_provider_query_errors_remain_retryable(factory, query_error) -> None:
    provider = FakeProvider()
    created = _create_failed_order(
        factory, provider, at=NOW, key=f"gh111-retry-case-{query_error.status_code if isinstance(query_error, WechatPayProviderResponseError) else 'transport'}"
    )
    provider.query_error = query_error

    summary = run_payment_recovery_once(factory, provider, at=NOW + timedelta(minutes=2))

    assert (summary.claimed, summary.recovered, summary.manual_recovery, summary.failed) == (1, 0, 0, 1)
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        finding = db.scalar(
            select(PaymentRecoveryFinding).where(
                PaymentRecoveryFinding.payment_order_id == created.order_id
            )
        )
        assert order is not None and finding is not None
        assert order.recovery_state == "automatic"
        assert order.query_attempt_count == 1
        assert _utc(order.next_query_at) == NOW + timedelta(minutes=7)
        assert finding.kind == FINDING_QUERY_FAILED
        assert finding.status == "open"
        assert finding.severity == "warning"


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


@pytest.mark.parametrize(
    ("provider_state", "provider_status", "expected_status"),
    [
        ("CLOSED", "closed", "closed"),
        ("REVOKED", "closed", "closed"),
        ("PAYERROR", "failed", "failed"),
    ],
)
def test_daily_reconciliation_marks_terminal_channel_check_without_reopening_checkout(
    factory, provider_state: str, provider_status: str, expected_status: str
) -> None:
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
        state=provider_state,
        status=provider_status,
        success=None,
    )

    summary = run_payment_reconciliation_once(factory, provider, at=NOW)

    assert (summary.claimed, summary.recovered, summary.pending) == (1, 1, 0)
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        assert order is not None
        assert order.status == expected_status
        assert order.recovery_state == "not_required"
        assert _utc(order.last_reconciled_at) == NOW
        assert order.checkout_url is None


def test_reconciliation_does_not_resolve_an_unrelated_orderless_signature_finding(factory) -> None:
    provider = FakeProvider()
    _create_order(
        factory,
        provider,
        at=NOW - timedelta(days=2),
        key="gh111-reconciliation-order",
    )
    provider.query_result = PaymentQueryResult(
        provider=WECHAT_PAY_PROVIDER,
        provider_order_ref="placeholder",
        state="CLOSED",
        status="closed",
        success=None,
    )
    with factory() as db:
        db.add(
            PaymentRecoveryFinding(
                id="gh111-orderless-signature-finding",
                provider=WECHAT_PAY_PROVIDER,
                tenant_id=None,
                payment_order_id=None,
                kind=FINDING_CALLBACK_SIGNATURE_FAILURE,
                severity="critical",
                status="open",
                dedupe_key="2" * 64,
                occurrence_count=1,
                first_detected_at=NOW,
                last_detected_at=NOW,
            )
        )
        db.commit()

    run_payment_reconciliation_once(factory, provider, at=NOW)

    with factory() as db:
        finding = db.get(PaymentRecoveryFinding, "gh111-orderless-signature-finding")
        assert finding is not None
        assert finding.status == "open"
        assert finding.resolved_at is None


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
