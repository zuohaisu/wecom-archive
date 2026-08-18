"""RND-404 Owner billing lifecycle experience: frozen/suspended access,
cancel-at-period-end intent, and read-only refund status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    PaymentOrder,
    PlanEntitlement,
    RefundOrder,
    Subscription,
    SubscriptionHistory,
    Tenant,
)
from app.db.session import get_db
from app.main import create_app
from app.routers import billing
from app.services.entitlements import ANNUAL_PLAN_CODE, ARCHIVE_ACCESS, UNLIMITED_SEATS
from app.services.payment_provider import CheckoutArtifact

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PLAN_ID = "rnd404-plan"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


class FakeProvider:
    code = "wechat_pay"
    app_id = "wx-rnd404"
    merchant_id = "1900004041"

    def create_payment(self, request):
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="qr_code",
            value="weixin://wxpay/bizpayurl?pr=rnd404",
        )

    def verify_and_parse_notification(self, headers, raw_body):  # pragma: no cover
        raise AssertionError("not used in this test module")

    def query_payment(self, provider_order_ref):  # pragma: no cover
        raise AssertionError("not used in this test module")

    def close_payment(self, provider_order_ref):  # pragma: no cover
        raise AssertionError("not used in this test module")


def _tables():
    return [
        Tenant.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        SubscriptionHistory.__table__,
        PaymentOrder.__table__,
        RefundOrder.__table__,
        AdminUser.__table__,
        AdminSession.__table__,
        AuditLog.__table__,
    ]


def _subscription(tenant_id: str, *, status: str = "active") -> Subscription:
    return Subscription(
        id=f"sub-{tenant_id}",
        tenant_id=tenant_id,
        plan_id=PLAN_ID,
        status=status,
        starts_at=NOW - timedelta(days=30),
        ends_at=NOW + timedelta(days=335),
        grace_ends_at=NOW + timedelta(days=342),
        cancel_at_period_end=False,
        source="test_seed",
        renewal_count=1,
        revision=1,
    )


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
                Tenant(id="tenant-active", name="Active", slug="tenant-active", lifecycle_status="active"),
                Tenant(id="tenant-frozen", name="Frozen", slug="tenant-frozen", lifecycle_status="frozen"),
                Tenant(
                    id="tenant-suspended",
                    name="Suspended",
                    slug="tenant-suspended",
                    lifecycle_status="suspended",
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
                    id="rnd404-unlimited", plan_id=PLAN_ID, capability=UNLIMITED_SEATS, is_enabled=True
                ),
                PlanEntitlement(
                    id="rnd404-archive", plan_id=PLAN_ID, capability=ARCHIVE_ACCESS, is_enabled=True
                ),
                _subscription("tenant-active", status="active"),
                _subscription("tenant-suspended", status="active"),
                AdminUser(
                    id="owner-active",
                    tenant_id="tenant-active",
                    wecom_user_id="owner-active",
                    role="owner",
                    status="active",
                ),
                AdminUser(
                    id="admin-active",
                    tenant_id="tenant-active",
                    wecom_user_id="admin-active",
                    role="admin",
                    status="active",
                ),
                AdminUser(
                    id="owner-frozen",
                    tenant_id="tenant-frozen",
                    wecom_user_id="owner-frozen",
                    role="owner",
                    status="active",
                ),
                AdminUser(
                    id="owner-suspended",
                    tenant_id="tenant-suspended",
                    wecom_user_id="owner-suspended",
                    role="owner",
                    status="active",
                ),
                AdminSession(
                    id="session-owner-active",
                    admin_user_id="owner-active",
                    tenant_id="tenant-active",
                    wecom_user_id="owner-active",
                    session_scope="admin",
                    expires_at=NOW + timedelta(hours=1),
                    is_revoked=False,
                ),
                AdminSession(
                    id="session-admin-active",
                    admin_user_id="admin-active",
                    tenant_id="tenant-active",
                    wecom_user_id="admin-active",
                    session_scope="admin",
                    expires_at=NOW + timedelta(hours=1),
                    is_revoked=False,
                ),
                AdminSession(
                    id="session-owner-frozen",
                    admin_user_id="owner-frozen",
                    tenant_id="tenant-frozen",
                    wecom_user_id="owner-frozen",
                    session_scope="admin",
                    expires_at=NOW + timedelta(hours=1),
                    is_revoked=False,
                ),
                AdminSession(
                    id="session-owner-suspended",
                    admin_user_id="owner-suspended",
                    tenant_id="tenant-suspended",
                    wecom_user_id="owner-suspended",
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
    return TestClient(app), factory


def _client_with_session(client: TestClient, session_id: str) -> TestClient:
    scoped = TestClient(client.app)
    scoped.cookies.set("session_id", session_id)
    return scoped


# ── AC-1: frozen Owner can still view billing and create a renewal order ──


def test_frozen_owner_can_view_billing_page_and_create_order(monkeypatch) -> None:
    base, _factory = _setup(monkeypatch)
    client = _client_with_session(base, "session-owner-frozen")

    page = client.get("/admin/billing")
    subscription = client.get("/api/billing/subscription")
    created = client.post(
        "/api/billing/orders",
        headers={"Idempotency-Key": "rnd404-frozen-order-key"},
        json={"plan_code": ANNUAL_PLAN_CODE},
    )

    assert page.status_code == 200
    assert subscription.status_code == 200
    assert subscription.json()["tenant_lifecycle_status"] == "frozen"
    assert created.status_code == 201


# ── AC-2: suspended Owner reads status but cannot write ──


def test_suspended_owner_can_read_status_but_not_pay_or_change_intent(monkeypatch) -> None:
    base, _factory = _setup(monkeypatch)
    client = _client_with_session(base, "session-owner-suspended")

    page = client.get("/admin/billing")
    subscription = client.get("/api/billing/subscription")
    refund = client.get("/api/billing/refunds/latest")
    create = client.post(
        "/api/billing/orders",
        headers={"Idempotency-Key": "rnd404-suspended-order-key"},
        json={"plan_code": ANNUAL_PLAN_CODE},
    )
    cancel_intent = client.post(
        "/api/billing/subscription/cancel-intent", json={"enabled": True}
    )

    assert page.status_code == 200
    assert subscription.status_code == 200
    assert subscription.json()["tenant_lifecycle_status"] == "suspended"
    assert refund.status_code == 200
    assert refund.json() is None
    assert create.status_code == 403
    assert cancel_intent.status_code == 403


# ── AC-3: cancel-at-period-end intent is reversible and does not move ends_at ──


def test_owner_can_set_and_restore_cancel_at_period_end(monkeypatch) -> None:
    base, _factory = _setup(monkeypatch)
    client = _client_with_session(base, "session-owner-active")

    before = client.get("/api/billing/subscription").json()
    assert before["cancel_at_period_end"] is False

    set_true = client.post("/api/billing/subscription/cancel-intent", json={"enabled": True})
    assert set_true.status_code == 200
    body = set_true.json()
    assert body["cancel_at_period_end"] is True
    assert body["ends_at"] == before["ends_at"]

    set_false = client.post("/api/billing/subscription/cancel-intent", json={"enabled": False})
    assert set_false.status_code == 200
    restored = set_false.json()
    assert restored["cancel_at_period_end"] is False
    assert restored["ends_at"] == before["ends_at"]


# ── AC-4: only the owner role may change the cancel-at-period-end intent ──


def test_admin_role_cannot_change_cancel_at_period_end(monkeypatch) -> None:
    base, _factory = _setup(monkeypatch)
    client = _client_with_session(base, "session-admin-active")

    response = client.post("/api/billing/subscription/cancel-intent", json={"enabled": True})

    assert response.status_code == 403


# ── AC-5: refund status is read-only and reflects the stored provider state ──


@pytest.mark.parametrize(
    "status,extra",
    [
        ("processing", {}),
        ("succeeded", {"succeeded_at": NOW, "entitlement_reversed_at": NOW}),
        ("abnormal", {"failure_code": "SIGN_ERROR"}),
        ("manual_recovery_required", {"failure_code": "RECONCILE_MISMATCH"}),
    ],
)
def test_owner_can_read_refund_status_without_initiating_one(monkeypatch, status, extra) -> None:
    base, factory = _setup(monkeypatch)
    with factory() as db:
        db.add(
            RefundOrder(
                id=f"refund-{status}",
                tenant_id="tenant-active",
                payment_order_id="payment-order-stub",
                term_grant_id="term-grant-stub",
                amount_cents=9900,
                currency="CNY",
                provider="wechat_pay",
                status=status,
                reason_code="owner_requested",
                approved_by_platform_admin_id="platform-admin-stub",
                idempotency_key_hash="0" * 64,
                requested_at=NOW,
                **extra,
            )
        )
        db.commit()

    client = _client_with_session(base, "session-owner-active")
    response = client.get("/api/billing/refunds/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status
    assert body["amount_cents"] == 9900
    assert "refund_provider_unavailable" not in response.text
    # RefundOut never carries the mutating-endpoint fields owned by
    # app.routers.refunds (submit/query) — this is a display-only surface.
    assert "confirmation" not in body
