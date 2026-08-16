"""Platform-only operations dashboard, summaries, and audited manual actions."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit import AuditAction
from app.auth import hash_password
from app.db.base import Base
from app.db.models import (
    AdminUser,
    ArchiveMessage,
    AuditLog,
    BillingPlan,
    Contact,
    ManualFinancialTransaction,
    MediaFile,
    PaymentOrder,
    PlatformAdmin,
    Subscription,
    SubscriptionActivation,
    SubscriptionHistory,
    Tenant,
)
from app.db.session import get_db
from app.main import create_app


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _tables():
    return [
        Tenant.__table__,
        PlatformAdmin.__table__,
        AdminUser.__table__,
        Contact.__table__,
        BillingPlan.__table__,
        Subscription.__table__,
        SubscriptionActivation.__table__,
        SubscriptionHistory.__table__,
        ArchiveMessage.__table__,
        MediaFile.__table__,
        PaymentOrder.__table__,
        ManualFinancialTransaction.__table__,
        AuditLog.__table__,
    ]


def _basic() -> dict[str, str]:
    token = base64.b64encode(b"operations@example.test:test-password").decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def operations_client() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    fts_index = next(
        index
        for index in ArchiveMessage.__table__.indexes
        if index.name == "ix_archive_messages_content_text_fts"
    )
    ArchiveMessage.__table__.indexes.remove(fts_index)
    try:
        Base.metadata.create_all(engine, tables=_tables())
    finally:
        ArchiveMessage.__table__.indexes.add(fts_index)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                PlatformAdmin(
                    id="platform-operator",
                    email="operations@example.test",
                    password_hash=hash_password("test-password"),
                    status="active",
                ),
                Tenant(id="tenant-active", name="Active Co", slug="active-co"),
                Tenant(id="tenant-trial", name="Trial Co", slug="trial-co"),
                Tenant(id="tenant-expired", name="Expired Co", slug="expired-co"),
                Tenant(id="tenant-canceled", name="Canceled Co", slug="canceled-co"),
                BillingPlan(
                    id="operations-plan",
                    code="operations-annual",
                    display_name="运营年度套餐",
                    amount_cents=10_000,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=100,
                    is_active=True,
                ),
                Subscription(
                    id="subscription-active",
                    tenant_id="tenant-active",
                    plan_id="operations-plan",
                    status="active",
                    starts_at=NOW - timedelta(days=2),
                    ends_at=NOW + timedelta(days=365),
                    source="test",
                    renewal_count=0,
                    revision=1,
                ),
                Subscription(
                    id="subscription-trial",
                    tenant_id="tenant-trial",
                    plan_id="operations-plan",
                    status="trial",
                    starts_at=NOW - timedelta(days=2),
                    ends_at=NOW + timedelta(days=7),
                    source="test",
                    renewal_count=0,
                    revision=1,
                ),
                Subscription(
                    id="subscription-expired",
                    tenant_id="tenant-expired",
                    plan_id="operations-plan",
                    status="active",
                    starts_at=NOW - timedelta(days=30),
                    ends_at=NOW - timedelta(days=8),
                    source="test",
                    renewal_count=0,
                    revision=1,
                ),
                Subscription(
                    id="subscription-canceled",
                    tenant_id="tenant-canceled",
                    plan_id="operations-plan",
                    status="canceled",
                    starts_at=NOW - timedelta(days=30),
                    ends_at=NOW + timedelta(days=30),
                    source="test",
                    renewal_count=0,
                    revision=1,
                ),
                AdminUser(
                    id="active-admin",
                    tenant_id="tenant-active",
                    wecom_user_id="active-admin",
                    role="owner",
                    status="active",
                    last_active_at=NOW,
                ),
                AdminUser(
                    id="disabled-admin",
                    tenant_id="tenant-active",
                    wecom_user_id="disabled-admin",
                    role="admin",
                    status="disabled",
                ),
                Contact(wecom_userid="observed-user", tenant_id="tenant-active"),
                ArchiveMessage(
                    id=1,
                    msgid="operations-message",
                    seq=1,
                    publickey_ver=1,
                    encrypt_random_key="key",
                    encrypt_chat_msg="message",
                    tenant_id="tenant-active",
                    content_text="must never appear in platform operations output",
                ),
                MediaFile(
                    sdkfileid="stored-media",
                    archive_message_id=1,
                    tenant_id="tenant-active",
                    download_status="downloaded",
                    file_size=90,
                ),
                MediaFile(
                    sdkfileid="pending-media",
                    archive_message_id=1,
                    tenant_id="tenant-active",
                    download_status="pending",
                ),
                PaymentOrder(
                    id="paid-order",
                    tenant_id="tenant-active",
                    plan_id="operations-plan",
                    plan_code="operations-annual",
                    plan_name="运营年度套餐",
                    amount_cents=10_000,
                    currency="CNY",
                    provider="test-provider",
                    provider_order_ref="order-operations",
                    provider_transaction_id="transaction-operations",
                    status="paid_activation_pending",
                    idempotency_key_hash="a" * 64,
                    created_at=NOW - timedelta(days=1),
                    expires_at=NOW + timedelta(days=1),
                    paid_at=NOW,
                ),
                ManualFinancialTransaction(
                    id="manual-refund",
                    tenant_id="tenant-active",
                    kind="refund",
                    amount_cents=500,
                    currency="CNY",
                    occurred_at=NOW,
                    recorded_by_platform_admin_id="platform-operator",
                ),
            ]
        )
        db.commit()

    app = create_app()

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_operations_dashboard_is_platform_only_and_contains_only_summary_metrics(
    operations_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = operations_client

    client.cookies.set("session_id", "tenant-owner-session-must-not-grant-platform-access")
    assert client.get("/api/platform/operations/dashboard").status_code == 401
    response = client.get("/api/platform/operations/dashboard?months=3", headers=_basic())

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_counts"] == {
        "total": 4,
        "trial": 1,
        "active": 1,
        "grace": 0,
        "expired": 1,
        "canceled": 1,
        "frozen": 0,
        "suspended": 0,
    }
    assert body["account_counts"] == {
        "admin_accounts": 2,
        "active_admin_accounts": 1,
        "observed_users": 1,
    }
    assert body["usage_totals"] == {
        "message_count": 1,
        "media_file_count": 2,
        "storage_bytes": 90,
    }
    assert body["revenue"]["receipt_cents"] == 10_000
    assert body["revenue"]["refund_cents"] == 500
    assert body["revenue"]["net_revenue_cents"] == 9_500
    assert body["storage_quota_risks"][0]["tenant_id"] == "tenant-active"
    assert "must never appear in platform operations output" not in response.text
    assert "provider_transaction_id" not in response.text


def test_paginated_tenant_detail_is_audited_and_manual_operations_are_audited(
    operations_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = operations_client

    listed = client.get(
        "/api/platform/operations/tenants?page=1&page_size=2", headers=_basic()
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 4
    assert len(listed.json()["items"]) == 2

    detail = client.get("/api/platform/operations/tenants/tenant-active", headers=_basic())
    assert detail.status_code == 200
    assert detail.json()["storage_bytes"] == 90
    assert detail.json()["receipt_cents"] == 10_000
    assert "content_text" not in detail.json()

    service = client.patch(
        "/api/platform/operations/tenants/tenant-active/service",
        headers=_basic(),
        json={"lifecycle_status": "suspended"},
    )
    assert service.status_code == 200
    assert service.json()["is_active"] is False

    subscription = client.put(
        "/api/platform/operations/tenants/tenant-active/subscription",
        headers=_basic(),
        json={
            "plan_code": "operations-annual",
            "status": "active",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=730)).isoformat(),
        },
    )
    assert subscription.status_code == 200
    assert subscription.json()["revision"] == 2

    manual = client.post(
        "/api/platform/operations/tenants/tenant-active/financial-transactions",
        headers=_basic(),
        json={
            "kind": "receipt",
            "amount_cents": 250,
            "occurred_at": NOW.isoformat(),
            "reference": "manual-reference",
            "note": "internal-only note",
        },
    )
    assert manual.status_code == 201
    assert manual.json()["reference"] == "manual-reference"

    with factory() as db:
        tenant = db.get(Tenant, "tenant-active")
        assert tenant is not None and tenant.lifecycle_status == "suspended"
        assert db.query(SubscriptionHistory).count() == 1
        actions = {row.action: row.detail for row in db.query(AuditLog).all()}
        assert AuditLog.__tablename__ == "audit_logs"
        assert "platform.tenant_accessed" in actions
        assert AuditAction.PLATFORM_TENANT_SUSPENDED in actions
        assert "platform.subscription_updated" in actions
        financial = actions["platform.manual_financial_transaction_recorded"]
        assert financial["platform_admin_id"] == "platform-operator"
        assert financial["amount_cents"] == 250
        assert "manual-reference" not in str(financial)
        assert "internal-only note" not in str(financial)


def test_operations_html_is_not_a_tenant_admin_surface(
    operations_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = operations_client

    denied = client.get("/platform/operations")
    allowed = client.get("/platform/operations", headers=_basic())

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert "平台运营" in allowed.text
    assert "不展示聊天内容" in allowed.text
