from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AdminSession,
    AdminUser,
    AuditLog,
    BillingPlan,
    PaymentEvent,
    PaymentOrder,
    PlanEntitlement,
    Subscription,
    SubscriptionActivation,
    SubscriptionHistory,
    SubscriptionTermGrant,
    Tenant,
    TenantStorageDaily,
)
from app.db.session import get_db
from app.main import create_app
from app.routers import billing, refunds as refunds_router
from app.schemas.refunds import SubmitRefundIn
from app.services.alipay import AlipayProtocolError
from app.services.entitlements import ANNUAL_PLAN_CODE, ARCHIVE_ACCESS, UNLIMITED_SEATS
from app.services.payment_provider import (
    CheckoutArtifact,
    PaymentQueryResult,
    TrustedPaymentEvent,
)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PLAN_ID = "rnd416-plan"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


class FakeAlipayProvider:
    code = "alipay"
    app_id = "2026082300000416"
    merchant_id = "2088230000000416"

    def __init__(self) -> None:
        self.notification: TrustedPaymentEvent | None = None
        self.query_result: PaymentQueryResult | None = None
        self.closed: list[str] = []

    def create_payment(self, request):
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="redirect",
            value="https://cashier.example.test/alipay?opaque=checkout-material",
        )

    def verify_and_parse_notification(self, headers, raw_body):
        if raw_body != b"signed-alipay-notification" or self.notification is None:
            raise AlipayProtocolError("invalid notification")
        return self.notification

    def query_payment(self, provider_order_ref):
        assert self.query_result is not None
        assert self.query_result.provider_order_ref == provider_order_ref
        return self.query_result

    def close_payment(self, provider_order_ref):
        self.closed.append(provider_order_ref)


class FakeWechatProvider:
    code = "wechat_pay"
    app_id = "wx-rnd416"
    merchant_id = "1900000416"

    def __init__(self) -> None:
        self.closed: list[str] = []

    def create_payment(self, request):
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="qr_code",
            value="weixin://wxpay/bizpayurl?pr=rnd416",
        )

    def query_payment(self, provider_order_ref):
        return PaymentQueryResult(
            provider=self.code,
            provider_order_ref=provider_order_ref,
            state="NOTPAY",
            status="pending",
            success=None,
        )

    def close_payment(self, provider_order_ref):
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
        AdminSession.__table__,
        AuditLog.__table__,
        TenantStorageDaily.__table__,
    ]


def _setup(monkeypatch):
    monkeypatch.setenv("ALIPAY_ENABLED", "false")
    monkeypatch.setenv("WECHAT_PAY_ENABLED", "false")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_tables())
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                Tenant(
                    id="tenant-rnd416",
                    name="RND-416",
                    slug="tenant-rnd416",
                    lifecycle_status="active",
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
                    id="rnd416-archive",
                    plan_id=PLAN_ID,
                    capability=ARCHIVE_ACCESS,
                    is_enabled=True,
                ),
                PlanEntitlement(
                    id="rnd416-seats",
                    plan_id=PLAN_ID,
                    capability=UNLIMITED_SEATS,
                    is_enabled=True,
                ),
                AdminUser(
                    id="owner-rnd416",
                    tenant_id="tenant-rnd416",
                    wecom_user_id="owner-rnd416",
                    name="Owner",
                    role="owner",
                    status="active",
                ),
                AdminSession(
                    id="session-rnd416",
                    admin_user_id="owner-rnd416",
                    tenant_id="tenant-rnd416",
                    wecom_user_id="owner-rnd416",
                    session_scope="admin",
                    expires_at=NOW + timedelta(hours=1),
                    is_revoked=False,
                ),
            ]
        )
        db.commit()
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    alipay = FakeAlipayProvider()
    wechat = FakeWechatProvider()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[billing.get_payment_provider] = lambda: alipay
    app.dependency_overrides[billing.get_alipay_payment_provider] = lambda: alipay
    monkeypatch.setattr(billing, "get_alipay_payment_provider", lambda: alipay)
    monkeypatch.setattr(billing, "alipay_is_enabled", lambda: True)
    monkeypatch.setattr(billing, "get_wechat_payment_provider", lambda: wechat)
    client = TestClient(app)
    client.cookies.set("session_id", "session-rnd416")
    return client, factory, alipay, wechat


def _create(client: TestClient):
    return client.post(
        "/api/billing/orders",
        headers={"Idempotency-Key": "browser-idempotency-rnd416-0001"},
        json={"plan_code": ANNUAL_PLAN_CODE},
    )


def test_alipay_checkout_redirect_is_authenticated_and_not_exposed_in_order_json(
    monkeypatch,
) -> None:
    client, factory, _alipay, _wechat = _setup(monkeypatch)

    created = _create(client)
    assert created.status_code == 201
    body = created.json()
    assert body["provider"] == "alipay"
    assert body["checkout_kind"] == "redirect"
    assert body["provider_state"] == "WAIT_BUYER_PAY"
    assert body["qr_available"] is False
    assert "checkout_url" not in body
    assert "opaque=checkout-material" not in created.text

    checkout_path = f"/api/billing/orders/{body['order_id']}/checkout"
    assert TestClient(client.app).get(checkout_path, follow_redirects=False).status_code == 401
    checkout = client.get(checkout_path, follow_redirects=False)
    assert checkout.status_code == 303
    assert checkout.headers["location"] == "https://cashier.example.test/alipay?opaque=checkout-material"
    assert checkout.headers["cache-control"] == "no-store"
    assert checkout.headers["referrer-policy"] == "no-referrer"
    assert client.get(f"/api/billing/orders/{body['order_id']}/qr").status_code == 409
    with factory() as db:
        assert db.get(PaymentOrder, body["order_id"]).status == "pending"
        assert db.query(Subscription).count() == 0


def test_return_page_does_not_activate_and_only_alipay_callback_activates_once(monkeypatch) -> None:
    client, factory, alipay, _wechat = _setup(monkeypatch)
    order_id = _create(client).json()["order_id"]
    assert client.get("/admin/billing").status_code == 200
    with factory() as db:
        order = db.get(PaymentOrder, order_id)
        assert order is not None
        alipay.notification = TrustedPaymentEvent(
            provider="alipay",
            provider_event_id="notify-rnd416-1",
            provider_order_ref=order.provider_order_ref,
            provider_transaction_id="2026082322001416000000000001",
            event_type="payment_succeeded",
            app_id=alipay.app_id,
            merchant_id=alipay.merchant_id,
            state="SUCCESS",
            amount_cents=9900,
            currency="CNY",
            succeeded_at=NOW,
            payload_hash="a" * 64,
            source="callback",
        )
        assert db.query(Subscription).count() == 0

    callback = client.post(
        "/api/payments/alipay/notify", content=b"signed-alipay-notification"
    )
    duplicate = client.post(
        "/api/payments/alipay/notify", content=b"signed-alipay-notification"
    )

    assert callback.status_code == duplicate.status_code == 200
    assert callback.text == duplicate.text == "success"
    with factory() as db:
        assert db.get(PaymentOrder, order_id).status == "succeeded"
        assert db.query(PaymentEvent).count() == 1
        assert db.query(Subscription).count() == 1
        assert db.query(SubscriptionHistory).count() == 1


def test_invalid_alipay_notification_cannot_grant_entitlements(monkeypatch) -> None:
    client, factory, _alipay, _wechat = _setup(monkeypatch)
    order_id = _create(client).json()["order_id"]

    rejected = client.post("/api/payments/alipay/notify", content=b"invalid")

    assert rejected.status_code == 400
    assert rejected.text == "failure"
    with factory() as db:
        assert db.get(PaymentOrder, order_id).status == "pending"
        assert db.query(PaymentEvent).count() == db.query(Subscription).count() == 0


def test_alipay_callback_with_wrong_total_cannot_activate(monkeypatch) -> None:
    client, factory, alipay, _wechat = _setup(monkeypatch)
    order_id = _create(client).json()["order_id"]
    with factory() as db:
        order = db.get(PaymentOrder, order_id)
        assert order is not None
        alipay.notification = TrustedPaymentEvent(
            provider="alipay",
            provider_event_id="notify-rnd416-wrong-total",
            provider_order_ref=order.provider_order_ref,
            provider_transaction_id="2026082322001416000000000003",
            event_type="payment_succeeded",
            app_id=alipay.app_id,
            merchant_id=alipay.merchant_id,
            state="SUCCESS",
            amount_cents=1,
            currency="CNY",
            succeeded_at=NOW,
            payload_hash="c" * 64,
            source="callback",
        )

    rejected = client.post(
        "/api/payments/alipay/notify", content=b"signed-alipay-notification"
    )

    assert rejected.status_code == 400
    assert rejected.text == "failure"
    with factory() as db:
        assert db.get(PaymentOrder, order_id).status == "pending"
        assert db.query(PaymentEvent).count() == db.query(Subscription).count() == 0


def test_alipay_query_uses_existing_provider_and_activates_without_callback(monkeypatch) -> None:
    client, factory, alipay, _wechat = _setup(monkeypatch)
    created = _create(client).json()
    with factory() as db:
        order = db.get(PaymentOrder, created["order_id"])
        assert order is not None
        event = TrustedPaymentEvent(
            provider="alipay",
            provider_event_id="query:2026082322001416000000000002",
            provider_order_ref=order.provider_order_ref,
            provider_transaction_id="2026082322001416000000000002",
            event_type="payment_succeeded",
            app_id=alipay.app_id,
            merchant_id=alipay.merchant_id,
            state="SUCCESS",
            amount_cents=9900,
            currency="CNY",
            succeeded_at=NOW,
            payload_hash="b" * 64,
            source="query",
        )
        alipay.query_result = PaymentQueryResult(
            provider="alipay",
            provider_order_ref=order.provider_order_ref,
            state="TRADE_SUCCESS",
            status="succeeded",
            success=event,
        )

    refreshed = client.post(f"/api/billing/orders/{created['order_id']}/refresh")

    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "succeeded"
    with factory() as db:
        assert db.query(Subscription).count() == 1


def test_platform_wechat_refund_rejects_alipay_orders_before_any_control_write(monkeypatch) -> None:
    db = Mock()
    monkeypatch.setattr(
        refunds_router,
        "get_order",
        lambda *_args: SimpleNamespace(provider="alipay"),
    )
    authorize = Mock()
    monkeypatch.setattr(
        refunds_router.platform_operations,
        "authorize_platform_operation",
        authorize,
    )

    with pytest.raises(HTTPException) as error:
        refunds_router.submit_platform_refund(
            tenant_id="tenant-rnd416",
            payload=SubmitRefundIn(
                payment_order_id="order-rnd416-alipay",
                reason_code="approved",
                confirmation="tenant-rnd416",
            ),
            idempotency_key="refund-idempotency-rnd416-0001",
            platform_admin=SimpleNamespace(id="platform-admin"),
            db=db,
            provider=FakeWechatProvider(),
        )

    assert error.value.status_code == 409
    assert error.value.detail == "refund_conflict"
    authorize.assert_not_called()
    db.commit.assert_not_called()


def test_existing_wechat_order_still_uses_wechat_provider_when_alipay_is_primary(monkeypatch) -> None:
    client, factory, _alipay, wechat = _setup(monkeypatch)
    with factory() as db:
        order = PaymentOrder(
            id="order-rnd416-wechat",
            tenant_id="tenant-rnd416",
            plan_id=PLAN_ID,
            plan_code=ANNUAL_PLAN_CODE,
            plan_name="年度基础套餐",
            amount_cents=9900,
            currency="CNY",
            provider="wechat_pay",
            provider_order_ref="W-rnd416-existing",
            status="pending",
            checkout_url="weixin://wxpay/bizpayurl?pr=rnd416-existing",
            idempotency_key_hash="c" * 64,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        )
        db.add(order)
        db.commit()

    closed = client.post("/api/billing/orders/order-rnd416-wechat/close")

    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert wechat.closed == ["W-rnd416-existing"]
