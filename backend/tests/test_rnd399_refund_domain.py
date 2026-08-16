from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit import AuditAction
from app.auth import require_platform_admin
from app.db.base import Base
from app.db.models import (
    AuditLog,
    BillingPlan,
    PaymentEvent,
    PaymentOrder,
    PlanEntitlement,
    PlatformAdmin,
    RefundEvent,
    RefundOrder,
    Subscription,
    SubscriptionActivation,
    SubscriptionHistory,
    SubscriptionTermGrant,
    Tenant,
)
from app.db.session import get_db
from app.main import app
from app.routers.refunds import get_refund_provider
from app.services.entitlements import ANNUAL_PLAN_CODE, ARCHIVE_ACCESS, has_entitlement
from app.services.payment_orders import (
    CreateOrderCommand,
    apply_trusted_payment,
    create_payment_order,
)
from app.services.payment_provider import (
    CheckoutArtifact,
    RefundSubmissionResult,
    TrustedPaymentEvent,
)
from app.services.refunds import (
    CreateRefundCommand,
    RefundConflictError,
    RefundNotFoundError,
    RefundReplayConflictError,
    TrustedRefundEvent,
    apply_trusted_refund_event,
    create_refund_request,
    mark_refund_processing,
)
from app.services.wechat_refunds import (
    SubmitWechatRefundCommand,
    apply_wechat_refund_event,
    query_and_reconcile_wechat_refund,
    submit_wechat_refund,
)

NOW = datetime(2026, 8, 16, 8, 0, tzinfo=timezone.utc)
PLAN_ID = "rnd399-plan"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


class FakeProvider:
    code = "wechat_pay"
    app_id = "wx-rnd399"
    merchant_id = "1900003999"

    def __init__(self):
        self.refund_requests = []
        self.refund_query_calls = 0
        self.refund_query_event = None
        self.refund_notification_event = None

    def create_payment(self, request):
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="qr_code",
            value="weixin://wxpay/bizpayurl?pr=rnd399",
        )

    def verify_and_parse_notification(self, headers, raw_body):
        raise NotImplementedError

    def query_payment(self, provider_order_ref):
        raise NotImplementedError

    def close_payment(self, provider_order_ref):
        raise NotImplementedError

    def create_refund(self, request):
        self.refund_requests.append(request)
        return RefundSubmissionResult(
            provider=self.code,
            provider_ref=request.provider_ref,
            provider_refund_id=f"wechat-{request.provider_ref}",
            provider_order_ref=request.provider_order_ref,
            provider_transaction_id=request.provider_transaction_id,
            state="PROCESSING",
            amount_cents=request.amount_cents,
            total_amount_cents=request.total_amount_cents,
            currency=request.currency,
            accepted_at=NOW + timedelta(hours=1, minutes=1),
        )

    def query_refund(self, provider_ref):
        self.refund_query_calls += 1
        assert self.refund_query_event is not None
        assert self.refund_query_event.provider_ref == provider_ref
        return self.refund_query_event

    def verify_and_parse_refund_notification(self, headers, raw_body):
        assert self.refund_notification_event is not None
        return self.refund_notification_event


def _tables():
    return [
        Tenant.__table__,
        PlatformAdmin.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        SubscriptionHistory.__table__,
        SubscriptionActivation.__table__,
        PaymentOrder.__table__,
        PaymentEvent.__table__,
        SubscriptionTermGrant.__table__,
        RefundOrder.__table__,
        RefundEvent.__table__,
        AuditLog.__table__,
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
                Tenant(id="tenant-a", name="A", slug="tenant-a"),
                Tenant(id="tenant-b", name="B", slug="tenant-b"),
                PlatformAdmin(
                    id="platform-admin",
                    email="refunds@example.test",
                    password_hash="unused",
                    status="active",
                ),
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
                PlanEntitlement(
                    id="rnd399-archive",
                    plan_id=PLAN_ID,
                    capability=ARCHIVE_ACCESS,
                    is_enabled=True,
                ),
            ]
        )
        db.commit()
    return result


def _pay(
    factory,
    *,
    key: str,
    transaction_id: str,
    event_id: str,
    at: datetime = NOW,
) -> PaymentOrder:
    provider = FakeProvider()
    created = create_payment_order(
        factory,
        provider,
        CreateOrderCommand(
            tenant_id="tenant-a",
            plan_code=ANNUAL_PLAN_CODE,
            idempotency_key=key,
            now=at,
        ),
    )
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        assert order is not None
        provider_order_ref = order.provider_order_ref
    apply_trusted_payment(
        factory,
        provider,
        TrustedPaymentEvent(
            provider=provider.code,
            provider_event_id=event_id,
            provider_order_ref=provider_order_ref,
            provider_transaction_id=transaction_id,
            event_type="payment_succeeded",
            app_id=provider.app_id,
            merchant_id=provider.merchant_id,
            state="SUCCESS",
            amount_cents=9900,
            currency="CNY",
            succeeded_at=at,
            payload_hash="a" * 64,
            source="callback",
        ),
    )
    with factory() as db:
        order = db.get(PaymentOrder, created.order_id)
        assert order is not None
        return order


def _request(db: Session, order: PaymentOrder, *, key="refund-request-rnd399-0001"):
    return create_refund_request(
        db,
        CreateRefundCommand(
            tenant_id="tenant-a",
            payment_order_id=order.id,
            idempotency_key=key,
            reason_code="customer_approved_full_refund",
            approved_by_platform_admin_id="platform-admin",
            requested_at=NOW + timedelta(hours=1),
        ),
    )


def _refund_event(
    db: Session,
    refund,
    *,
    state="SUCCESS",
    event_id="refund-event-rnd399-1",
    **changes,
):
    payment = db.get(PaymentOrder, refund.payment_order_id)
    assert payment is not None and payment.provider_transaction_id is not None
    values = {
        "provider": "wechat_pay",
        "provider_event_id": event_id,
        "provider_ref": refund.provider_ref,
        "provider_refund_id": "refund-id-rnd399",
        "provider_order_ref": payment.provider_order_ref,
        "provider_transaction_id": payment.provider_transaction_id,
        "merchant_id": FakeProvider.merchant_id,
        "state": state,
        "source": "callback",
        "amount_cents": 9900,
        "total_amount_cents": payment.amount_cents,
        "currency": "CNY",
        "payload_hash": "b" * 64,
        "occurred_at": NOW + timedelta(hours=2),
    }
    values.update(changes)
    return TrustedRefundEvent(**values)


def _accepted_refund(factory, order: PaymentOrder):
    with factory() as db:
        requested = _request(db, order)
        db.commit()
        processing = mark_refund_processing(
            db,
            "tenant-a",
            requested.refund_id,
            provider_ref="refund-provider-rnd399",
            accepted_at=NOW + timedelta(hours=1, minutes=1),
        )
        db.commit()
        return processing


def test_trusted_payment_materializes_one_exact_reversible_term_grant(factory) -> None:
    order = _pay(
        factory,
        key="payment-browser-rnd399-0001",
        transaction_id="payment-transaction-rnd399-0001",
        event_id="payment-event-rnd399-0001",
    )
    provider = FakeProvider()
    with factory() as db:
        grant = db.scalar(select(SubscriptionTermGrant))
        activation = db.get(SubscriptionActivation, order.activation_id)
        assert grant is not None and activation is not None
        assert grant.status == "active"
        assert grant.prior_subscription_existed is False
        assert grant.applied_revision == 1
        assert grant.applied_grace_ends_at == activation.applied_grace_ends_at

    # Exact payment replay cannot create a second grant.
    with factory() as db:
        stored = db.get(PaymentOrder, order.id)
        assert stored is not None
        payment_event = db.scalar(select(PaymentEvent))
    apply_trusted_payment(
        factory,
        provider,
        TrustedPaymentEvent(
            provider=provider.code,
            provider_event_id=payment_event.provider_event_id,
            provider_order_ref=order.provider_order_ref,
            provider_transaction_id=order.provider_transaction_id,
            event_type="payment_succeeded",
            app_id=provider.app_id,
            merchant_id=provider.merchant_id,
            state="SUCCESS",
            amount_cents=9900,
            currency="CNY",
            succeeded_at=NOW,
            payload_hash=payment_event.payload_hash,
            source="callback",
        ),
    )
    with factory() as db:
        assert db.query(SubscriptionTermGrant).count() == 1


def test_accepted_and_processing_refund_do_not_change_subscription(factory) -> None:
    order = _pay(
        factory,
        key="payment-browser-rnd399-0002",
        transaction_id="payment-transaction-rnd399-0002",
        event_id="payment-event-rnd399-0002",
    )
    with factory() as db:
        before = db.scalar(select(Subscription))
        before_projection = (before.status, before.ends_at, before.revision)
        first = _request(db, order)
        replay = _request(db, order)
        db.commit()
        assert first == replay
        processing = mark_refund_processing(
            db,
            "tenant-a",
            first.refund_id,
            provider_ref="refund-provider-rnd399",
            accepted_at=NOW + timedelta(hours=1, minutes=1),
        )
        db.commit()
        current = db.scalar(select(Subscription))
        assert processing.status == "processing"
        assert (current.status, current.ends_at, current.revision) == before_projection
        assert has_entitlement(db, "tenant-a", ARCHIVE_ACCESS, at=NOW) is True


def test_successful_first_payment_refund_atomically_revokes_entitlement(factory) -> None:
    order = _pay(
        factory,
        key="payment-browser-rnd399-0003",
        transaction_id="payment-transaction-rnd399-0003",
        event_id="payment-event-rnd399-0003",
    )
    refund = _accepted_refund(factory, order)
    with factory() as db:
        result = apply_trusted_refund_event(
            db,
            refund.refund_id,
            _refund_event(db, refund),
        )
        db.commit()
        subscription = db.scalar(select(Subscription))
        grant = db.scalar(select(SubscriptionTermGrant))
        tenant = db.get(Tenant, "tenant-a")
        assert result.status == "succeeded"
        assert result.entitlement_reversed_at is not None
        assert subscription.status == "canceled" and subscription.revision == 2
        assert grant.status == "reversed" and grant.reversed_at is not None
        assert tenant.lifecycle_status == "frozen"
        assert has_entitlement(db, "tenant-a", ARCHIVE_ACCESS, at=NOW) is False
        assert db.query(RefundEvent).count() == 1
        assert db.query(SubscriptionHistory).count() == 2
        assert db.query(AuditLog).filter_by(
            action=AuditAction.REFUND_SUCCEEDED
        ).count() == 1


def test_refunding_latest_renewal_restores_exact_pre_payment_term(factory) -> None:
    first = _pay(
        factory,
        key="payment-browser-rnd399-first",
        transaction_id="payment-transaction-rnd399-first",
        event_id="payment-event-rnd399-first",
    )
    second = _pay(
        factory,
        key="payment-browser-rnd399-second",
        transaction_id="payment-transaction-rnd399-second",
        event_id="payment-event-rnd399-second",
        at=NOW + timedelta(days=1),
    )
    with factory() as db:
        first_activation = db.get(SubscriptionActivation, first.activation_id)
        original_end = first_activation.applied_ends_at
    refund = _accepted_refund(factory, second)
    with factory() as db:
        result = apply_trusted_refund_event(
            db, refund.refund_id, _refund_event(db, refund)
        )
        db.commit()
        subscription = db.scalar(select(Subscription))
        assert result.status == "succeeded"
        assert subscription.ends_at == original_end
        assert subscription.renewal_count == 0
        assert subscription.status == "active"


def test_refunding_older_payment_after_later_renewal_requires_manual_recovery(factory) -> None:
    first = _pay(
        factory,
        key="payment-browser-rnd399-old",
        transaction_id="payment-transaction-rnd399-old",
        event_id="payment-event-rnd399-old",
    )
    refund = _accepted_refund(factory, first)
    _pay(
        factory,
        key="payment-browser-rnd399-later",
        transaction_id="payment-transaction-rnd399-later",
        event_id="payment-event-rnd399-later",
        at=NOW + timedelta(days=1),
    )
    with factory() as db:
        before = db.scalar(select(Subscription)).ends_at
        result = apply_trusted_refund_event(
            db, refund.refund_id, _refund_event(db, refund)
        )
        db.commit()
        subscription = db.scalar(select(Subscription))
        grant = db.get(SubscriptionTermGrant, result.term_grant_id)
        assert result.status == "manual_recovery_required"
        assert result.failure_code == "later_or_ambiguous_subscription_change"
        assert subscription.ends_at == before
        assert grant.status == "manual_recovery_required"


def test_cross_tenant_forged_event_and_changed_replay_fail_closed(factory) -> None:
    order = _pay(
        factory,
        key="payment-browser-rnd399-0006",
        transaction_id="payment-transaction-rnd399-0006",
        event_id="payment-event-rnd399-0006",
    )
    with factory() as db:
        with pytest.raises(RefundNotFoundError):
            create_refund_request(
                db,
                replace(
                    CreateRefundCommand(
                        tenant_id="tenant-a",
                        payment_order_id=order.id,
                        idempotency_key="refund-request-rnd399-cross",
                        reason_code="customer_approved_full_refund",
                        approved_by_platform_admin_id="platform-admin",
                        requested_at=NOW,
                    ),
                    tenant_id="tenant-b",
                ),
            )
        db.rollback()
    refund = _accepted_refund(factory, order)
    with factory() as db:
        with pytest.raises(RefundConflictError):
            apply_trusted_refund_event(
                db,
                refund.refund_id,
                _refund_event(db, refund, amount_cents=1),
            )
        db.rollback()
        applied = apply_trusted_refund_event(
            db, refund.refund_id, _refund_event(db, refund)
        )
        db.commit()
        replay = apply_trusted_refund_event(
            db, refund.refund_id, _refund_event(db, refund)
        )
        assert replay == applied
        with pytest.raises(RefundReplayConflictError):
            apply_trusted_refund_event(
                db,
                refund.refund_id,
                _refund_event(db, refund, payload_hash="c" * 64),
            )


def test_audit_failure_rolls_back_refund_subscription_event_and_history(factory) -> None:
    order = _pay(
        factory,
        key="payment-browser-rnd399-rollback",
        transaction_id="payment-transaction-rnd399-rollback",
        event_id="payment-event-rnd399-rollback",
    )
    refund = _accepted_refund(factory, order)
    session_class = factory.class_
    injected = {"raised": False}

    def fail_refund_audit(session, _context, _instances):
        if not injected["raised"] and any(
            isinstance(item, AuditLog)
            and item.action == AuditAction.REFUND_SUCCEEDED
            for item in session.new
        ):
            injected["raised"] = True
            raise RuntimeError("injected refund audit failure")

    event.listen(session_class, "before_flush", fail_refund_audit)
    try:
        with factory() as db:
            with pytest.raises(RuntimeError, match="injected refund audit failure"):
                apply_trusted_refund_event(
                    db, refund.refund_id, _refund_event(db, refund)
                )
                db.commit()
            db.rollback()
    finally:
        event.remove(session_class, "before_flush", fail_refund_audit)

    with factory() as db:
        stored_refund = db.get(RefundOrder, refund.refund_id)
        subscription = db.scalar(select(Subscription))
        grant = db.get(SubscriptionTermGrant, refund.term_grant_id)
        assert stored_refund.status == "processing"
        assert subscription.status == "active" and subscription.revision == 1
        assert grant.status == "active"
        assert db.query(RefundEvent).count() == 0
        assert db.query(SubscriptionHistory).count() == 1


def _rnd403_event(
    payment: PaymentOrder,
    refund,
    provider: FakeProvider,
    *,
    state: str = "SUCCESS",
    source: str = "query",
    event_id: str = "refund-query-rnd403",
) -> TrustedRefundEvent:
    assert payment.provider_transaction_id is not None
    assert refund.provider_ref is not None
    assert refund.provider_refund_id is not None
    return TrustedRefundEvent(
        provider=provider.code,
        provider_event_id=event_id,
        provider_ref=refund.provider_ref,
        provider_refund_id=refund.provider_refund_id,
        provider_order_ref=payment.provider_order_ref,
        provider_transaction_id=payment.provider_transaction_id,
        merchant_id=provider.merchant_id,
        state=state,
        source=source,
        amount_cents=payment.amount_cents,
        total_amount_cents=payment.amount_cents,
        currency=payment.currency,
        payload_hash=hashlib.sha256(event_id.encode()).hexdigest(),
        occurred_at=NOW + timedelta(hours=2),
    )


def test_rnd403_submission_is_stable_and_query_success_reverses_once(factory) -> None:
    provider = FakeProvider()
    order = _pay(
        factory,
        key="payment-browser-rnd403-service",
        transaction_id="payment-transaction-rnd403-service",
        event_id="payment-event-rnd403-service",
    )
    command = SubmitWechatRefundCommand(
        tenant_id="tenant-a",
        payment_order_id=order.id,
        idempotency_key="refund-request-rnd403-service",
        reason_code="customer_request",
        approved_by_platform_admin_id="platform-admin",
        requested_at=NOW + timedelta(hours=1),
    )

    submitted = submit_wechat_refund(factory, provider, command)
    replay = submit_wechat_refund(factory, provider, command)

    assert submitted == replay
    assert submitted.status == "processing"
    assert submitted.provider_ref.startswith("R")
    assert len(provider.refund_requests) == 1
    request = provider.refund_requests[0]
    assert request.amount_cents == request.total_amount_cents == 9900
    with factory() as db:
        subscription = db.scalar(select(Subscription))
        payment = db.get(PaymentOrder, order.id)
        refund = db.get(RefundOrder, submitted.refund_id)
        assert subscription.status == "active" and subscription.revision == 1
        assert payment is not None and refund is not None
        provider.refund_query_event = _rnd403_event(
            payment, refund, provider
        )

    recovered = query_and_reconcile_wechat_refund(
        factory, provider, "tenant-a", submitted.refund_id
    )
    duplicate = query_and_reconcile_wechat_refund(
        factory, provider, "tenant-a", submitted.refund_id
    )
    assert recovered == duplicate
    assert recovered.status == "succeeded"

    with factory() as db:
        payment = db.get(PaymentOrder, order.id)
        refund = db.get(RefundOrder, submitted.refund_id)
        assert payment is not None and refund is not None
        callback = _rnd403_event(
            payment,
            refund,
            provider,
            source="callback",
            event_id="refund-callback-rnd403",
        )
    callback_result = apply_wechat_refund_event(factory, provider, callback)
    assert callback_result.status == "succeeded"

    with factory() as db:
        subscription = db.scalar(select(Subscription))
        assert subscription.status == "canceled" and subscription.revision == 2
        assert db.query(RefundEvent).count() == 2

    late_processing = replace(
        callback,
        provider_event_id="refund-late-processing-rnd403",
        state="PROCESSING",
        payload_hash="d" * 64,
    )
    with pytest.raises(RefundConflictError, match="already terminal"):
        apply_wechat_refund_event(factory, provider, late_processing)


def test_rnd403_http_surface_is_platform_controlled_and_callback_driven(factory) -> None:
    provider = FakeProvider()
    order = _pay(
        factory,
        key="payment-browser-rnd403-api",
        transaction_id="payment-transaction-rnd403-api",
        event_id="payment-event-rnd403-api",
    )

    def override_db():
        with factory() as db:
            yield db

    old_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_refund_provider] = lambda: provider
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            path = "/api/platform/operations/tenants/tenant-a/refunds"
            unauthorized = client.post(
                path,
                headers={"Idempotency-Key": "refund-request-rnd403-api"},
                json={
                    "payment_order_id": order.id,
                    "reason_code": "customer_request",
                    "confirmation": "tenant-a",
                },
            )
            assert unauthorized.status_code == 401

            with factory() as db:
                platform_admin = db.get(PlatformAdmin, "platform-admin")
                assert platform_admin is not None
                db.expunge(platform_admin)
            app.dependency_overrides[require_platform_admin] = lambda: platform_admin
            submitted = client.post(
                path,
                headers={"Idempotency-Key": "refund-request-rnd403-api"},
                json={
                    "payment_order_id": order.id,
                    "reason_code": "customer_request",
                    "confirmation": "tenant-a",
                },
            )
            assert submitted.status_code == 202
            assert submitted.json()["status"] == "processing"

            with factory() as db:
                payment = db.get(PaymentOrder, order.id)
                refund = db.get(RefundOrder, submitted.json()["refund_id"])
                assert payment is not None and refund is not None
                provider.refund_query_event = _rnd403_event(
                    payment,
                    refund,
                    provider,
                    source="query",
                    event_id="refund-http-query-rnd405",
                )
                provider.refund_notification_event = _rnd403_event(
                    payment,
                    refund,
                    provider,
                    source="callback",
                    event_id="refund-http-callback-rnd403",
                )
            query_path = (
                path
                + "/"
                + submitted.json()["refund_id"]
                + "/query"
            )
            query_headers = {
                "Idempotency-Key": "refund-query-rnd405-api",
            }
            query_payload = {
                "reason_code": "provider_reconciliation",
                "confirmation": "tenant-a",
            }
            queried = client.post(
                query_path,
                headers=query_headers,
                json=query_payload,
            )
            query_replay = client.post(
                query_path,
                headers=query_headers,
                json=query_payload,
            )
            assert queried.status_code == 200
            assert queried.json()["status"] == "succeeded"
            assert query_replay.status_code == 200
            assert provider.refund_query_calls == 1
            callback = client.post(
                "/api/refunds/wechat/notify",
                content=b"signed-provider-envelope",
            )
            assert callback.status_code == 204
            with factory() as db:
                stored = db.get(RefundOrder, submitted.json()["refund_id"])
                assert stored is not None and stored.status == "succeeded"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)

    paths = {route.path for route in app.routes}
    assert "/api/tenants/{tenant_id}/refunds" not in paths


def _postgres_test_url() -> str | None:
    url = os.getenv("RND399_TEST_DATABASE_URL", "").strip()
    if not url:
        return None
    if not urlparse(url).path.lstrip("/").endswith("test"):
        raise RuntimeError("RND399_TEST_DATABASE_URL must name a database ending in test")
    return url


def _delete_postgres_fixture(factory, tenant_id: str, platform_admin_id: str) -> None:
    with factory() as db:
        refund_ids = db.scalars(
            select(RefundOrder.id).where(RefundOrder.tenant_id == tenant_id)
        ).all()
        order_ids = db.scalars(
            select(PaymentOrder.id).where(PaymentOrder.tenant_id == tenant_id)
        ).all()
        if refund_ids:
            db.query(RefundEvent).filter(
                RefundEvent.refund_order_id.in_(refund_ids)
            ).delete(synchronize_session=False)
        db.query(RefundOrder).filter_by(tenant_id=tenant_id).delete()
        db.query(SubscriptionTermGrant).filter_by(tenant_id=tenant_id).delete()
        if order_ids:
            db.query(PaymentEvent).filter(
                PaymentEvent.order_id.in_(order_ids)
            ).delete(synchronize_session=False)
        db.query(PaymentOrder).filter_by(tenant_id=tenant_id).delete()
        db.query(AuditLog).filter_by(tenant_id=tenant_id).delete()
        db.query(SubscriptionActivation).filter_by(tenant_id=tenant_id).delete()
        db.query(SubscriptionHistory).filter_by(tenant_id=tenant_id).delete()
        db.query(Subscription).filter_by(tenant_id=tenant_id).delete()
        db.query(Tenant).filter_by(id=tenant_id).delete()
        db.query(PlatformAdmin).filter_by(id=platform_admin_id).delete()
        db.commit()


@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason="RND399_TEST_DATABASE_URL is not configured for isolated PostgreSQL proof",
)
def test_concurrent_postgresql_success_event_reverses_once() -> None:
    engine = create_engine(_postgres_test_url(), pool_size=4)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    platform_admin_id = str(uuid.uuid4())
    provider = FakeProvider()
    with factory() as db:
        db.add_all(
            [
                Tenant(id=tenant_id, name="RND-399", slug=f"rnd399-{suffix}"),
                PlatformAdmin(
                    id=platform_admin_id,
                    email=f"rnd399-{suffix}@example.test",
                    password_hash="unused",
                    status="active",
                ),
            ]
        )
        db.commit()
    try:
        created = create_payment_order(
            factory,
            provider,
            CreateOrderCommand(
                tenant_id=tenant_id,
                plan_code=ANNUAL_PLAN_CODE,
                idempotency_key=f"rnd399-payment-{suffix}",
                now=NOW,
            ),
        )
        with factory() as db:
            order = db.get(PaymentOrder, created.order_id)
            assert order is not None
            provider_order_ref = order.provider_order_ref
        apply_trusted_payment(
            factory,
            provider,
            TrustedPaymentEvent(
                provider=provider.code,
                provider_event_id=f"payment-{suffix}",
                provider_order_ref=provider_order_ref,
                provider_transaction_id=f"transaction-{suffix}",
                event_type="payment_succeeded",
                app_id=provider.app_id,
                merchant_id=provider.merchant_id,
                state="SUCCESS",
                amount_cents=9900,
                currency="CNY",
                succeeded_at=NOW,
                payload_hash=uuid.uuid4().hex * 2,
                source="callback",
            ),
        )
        with factory() as db:
            requested = create_refund_request(
                db,
                CreateRefundCommand(
                    tenant_id=tenant_id,
                    payment_order_id=created.order_id,
                    idempotency_key=f"rnd399-refund-{suffix}",
                    reason_code="approved_full_refund",
                    approved_by_platform_admin_id=platform_admin_id,
                    requested_at=NOW + timedelta(hours=1),
                ),
            )
            db.commit()
            processing = mark_refund_processing(
                db,
                tenant_id,
                requested.refund_id,
                provider_ref=f"refund-{suffix}",
                accepted_at=NOW + timedelta(hours=1, minutes=1),
            )
            db.commit()
        trusted = TrustedRefundEvent(
            provider=provider.code,
            provider_event_id=f"refund-event-{suffix}",
            provider_ref=processing.provider_ref,
            provider_refund_id=f"refund-id-{suffix}",
            provider_order_ref=provider_order_ref,
            provider_transaction_id=f"transaction-{suffix}",
            merchant_id=provider.merchant_id,
            state="SUCCESS",
            source="callback",
            amount_cents=9900,
            total_amount_cents=9900,
            currency="CNY",
            payload_hash=uuid.uuid4().hex * 2,
            occurred_at=NOW + timedelta(hours=2),
        )

        def apply_once(_index):
            with factory() as db:
                result = apply_trusted_refund_event(db, processing.refund_id, trusted)
                db.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(apply_once, range(2)))
        assert {result.status for result in results} == {"succeeded"}
        with factory() as db:
            assert db.query(RefundEvent).filter_by(
                refund_order_id=processing.refund_id
            ).count() == 1
            assert db.query(SubscriptionHistory).filter_by(
                tenant_id=tenant_id
            ).count() == 2
            assert db.query(AuditLog).filter_by(
                tenant_id=tenant_id,
                action=AuditAction.REFUND_SUCCEEDED,
            ).count() == 1
    finally:
        _delete_postgres_fixture(factory, tenant_id, platform_admin_id)
        engine.dispose()
