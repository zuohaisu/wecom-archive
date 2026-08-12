from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
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
    Tenant,
)
from app.db.session import get_db
from app.main import create_app
from app.routers import billing
from app.services.entitlements import ANNUAL_PLAN_CODE, UNLIMITED_SEATS
from app.services.payment_provider import (
    CheckoutArtifact,
    PaymentQueryResult,
    TrustedPaymentEvent,
)
from app.services.wechat_pay import WechatPayConfigurationError

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PLAN_ID = "rnd380-api-plan"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


class FakeProvider:
    code = "wechat_pay"
    app_id = "wx-rnd380"
    merchant_id = "1900003801"

    def __init__(self):
        self.notification = None
        self.queries = []
        self.closed = []

    def create_payment(self, request):
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="qr_code",
            value="weixin://wxpay/bizpayurl?pr=api-rnd380",
        )

    def verify_and_parse_notification(self, headers, raw_body):
        assert raw_body == b'{"encrypted":"opaque"}'
        assert self.notification is not None
        return self.notification

    def query_payment(self, provider_order_ref):
        self.queries.append(provider_order_ref)
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
        AdminUser.__table__,
        AdminSession.__table__,
        AuditLog.__table__,
    ]


def _setup(monkeypatch):
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
                    id="tenant-provisioning",
                    name="Provisioning",
                    slug="tenant-provisioning",
                    lifecycle_status="provisioning",
                    is_active=False,
                ),
                Tenant(
                    id="tenant-other",
                    name="Other",
                    slug="tenant-other",
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
                    id="rnd380-unlimited",
                    plan_id=PLAN_ID,
                    capability=UNLIMITED_SEATS,
                    is_enabled=True,
                ),
                AdminUser(
                    id="owner-provisioning",
                    tenant_id="tenant-provisioning",
                    wecom_user_id="owner-provisioning",
                    name="Owner",
                    role="owner",
                    status="active",
                ),
                AdminUser(
                    id="owner-other",
                    tenant_id="tenant-other",
                    wecom_user_id="owner-other",
                    role="owner",
                    status="active",
                ),
                AdminUser(
                    id="admin-other",
                    tenant_id="tenant-other",
                    wecom_user_id="admin-other",
                    role="admin",
                    status="active",
                ),
                AdminSession(
                    id="session-provisioning",
                    admin_user_id="owner-provisioning",
                    tenant_id="tenant-provisioning",
                    wecom_user_id="owner-provisioning",
                    session_scope="provisioning",
                    expires_at=NOW + timedelta(hours=1),
                    is_revoked=False,
                ),
                AdminSession(
                    id="session-other",
                    admin_user_id="owner-other",
                    tenant_id="tenant-other",
                    wecom_user_id="owner-other",
                    session_scope="admin",
                    expires_at=NOW + timedelta(hours=1),
                    is_revoked=False,
                ),
                AdminSession(
                    id="session-admin",
                    admin_user_id="admin-other",
                    tenant_id="tenant-other",
                    wecom_user_id="admin-other",
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

    provider = FakeProvider()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[billing.get_payment_provider] = lambda: provider
    monkeypatch.setattr(billing, "wechat_pay_is_enabled", lambda: True)
    return TestClient(app), factory, provider


def _client_with_session(client: TestClient, session_id: str) -> TestClient:
    scoped = TestClient(client.app)
    scoped.cookies.set("session_id", session_id)
    return scoped


def _create(client: TestClient):
    return client.post(
        "/api/billing/orders",
        headers={"Idempotency-Key": "browser-api-idempotency-rnd380"},
        json={"plan_code": ANNUAL_PLAN_CODE},
    )


def test_provisioning_owner_can_view_plan_create_order_and_receive_only_qr_image(
    monkeypatch,
) -> None:
    base, factory, _provider = _setup(monkeypatch)
    client = _client_with_session(base, "session-provisioning")

    page = client.get("/admin/billing")
    plan = client.get("/api/billing/plan")
    created = _create(client)

    assert page.status_code == 200
    assert "购买年度套餐" in page.text
    assert "WECHAT_PAY_API_V3_KEY" not in page.text
    assert plan.status_code == 200
    assert plan.json() == {
        "code": ANNUAL_PLAN_CODE,
        "display_name": "年度基础套餐",
        "amount_cents": 9900,
        "currency": "CNY",
        "billing_period_months": 12,
        "storage_quota_bytes": 5 * 1024**3,
        "unlimited_seats": True,
        "tencent_archive_fee_separate": True,
        "payment_enabled": True,
    }
    assert created.status_code == 201
    body = created.json()
    assert body["amount_cents"] == 9900
    assert body["status"] == "pending"
    assert body["qr_available"] is True
    assert "checkout_url" not in body
    assert "weixin://" not in created.text
    qr = client.get(f"/api/billing/orders/{body['order_id']}/qr")
    assert qr.status_code == 200
    assert qr.headers["content-type"] == "image/png"
    assert qr.headers["cache-control"] == "no-store"
    assert qr.content.startswith(b"\x89PNG\r\n\x1a\n")
    with factory() as db:
        order = db.get(PaymentOrder, body["order_id"])
        assert order.tenant_id == "tenant-provisioning"
        assert order.amount_cents == 9900


def test_signed_provider_callback_needs_no_browser_session_and_activates_once(
    monkeypatch,
) -> None:
    base, factory, provider = _setup(monkeypatch)
    owner = _client_with_session(base, "session-provisioning")
    created = _create(owner).json()
    with factory() as db:
        order = db.get(PaymentOrder, created["order_id"])
        provider.notification = TrustedPaymentEvent(
            provider="wechat_pay",
            provider_event_id="callback-api-rnd380",
            provider_order_ref=order.provider_order_ref,
            provider_transaction_id="42000000000000000000000380",
            event_type="payment_succeeded",
            app_id=provider.app_id,
            merchant_id=provider.merchant_id,
            state="SUCCESS",
            amount_cents=9900,
            currency="CNY",
            succeeded_at=NOW,
            payload_hash="a" * 64,
            source="callback",
        )

    callback = base.post(
        "/api/payments/wechat/notify",
        content=b'{"encrypted":"opaque"}',
        headers={"Content-Type": "application/json"},
    )
    duplicate = base.post(
        "/api/payments/wechat/notify",
        content=b'{"encrypted":"opaque"}',
        headers={"Content-Type": "application/json"},
    )
    status = owner.get(f"/api/billing/orders/{created['order_id']}")

    assert callback.status_code == duplicate.status_code == 204
    assert status.json()["status"] == "succeeded"
    assert status.json()["subscription_ends_at"] is not None
    with factory() as db:
        assert db.query(PaymentEvent).count() == 1
        assert db.query(SubscriptionHistory).count() == 1
        assert db.query(AuditLog).count() == 1


def test_order_status_and_qr_are_tenant_scoped_and_owner_only(monkeypatch) -> None:
    base, _factory, _provider = _setup(monkeypatch)
    provisioning = _client_with_session(base, "session-provisioning")
    other = _client_with_session(base, "session-other")
    admin = _client_with_session(base, "session-admin")
    order_id = _create(provisioning).json()["order_id"]

    assert other.get(f"/api/billing/orders/{order_id}").status_code == 404
    assert other.get(f"/api/billing/orders/{order_id}/qr").status_code == 404
    assert admin.get("/admin/billing").status_code == 403
    assert admin.get("/api/billing/plan").status_code == 403
    assert base.get("/admin/billing").status_code == 401


def test_page_and_javascript_expose_precise_states_without_raw_checkout_material(
    monkeypatch,
) -> None:
    base, _factory, _provider = _setup(monkeypatch)
    client = _client_with_session(base, "session-provisioning")
    page = client.get("/admin/billing").text
    script = (
        Path(billing.__file__).parents[1] / "web/static/billing.js"
    ).read_text()

    for state in (
        "pending",
        "succeeded",
        "closed",
        "failed",
        "paid_activation_pending",
    ):
        assert f"billing.status.{state}" in page or state in script
    assert "/api/billing/orders/" in script
    assert "/refresh" in script
    assert "weixin://" not in page
    assert "checkout_url" not in script
    assert "银行卡" in page
    assert "不是免密自动扣款" in page


def test_enabled_but_incomplete_configuration_stops_app_startup(monkeypatch) -> None:
    monkeypatch.setenv("WECHAT_PAY_ENABLED", "true")
    for name in (
        "WECHAT_PAY_APP_ID",
        "WECHAT_PAY_MCH_ID",
        "WECHAT_PAY_MERCHANT_SERIAL_NO",
        "WECHAT_PAY_MERCHANT_PRIVATE_KEY",
        "WECHAT_PAY_API_V3_KEY",
        "WECHAT_PAY_PUBLIC_KEY_ID",
        "WECHAT_PAY_PUBLIC_KEY",
        "WECHAT_PAY_NOTIFY_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(WechatPayConfigurationError):
        create_app()
