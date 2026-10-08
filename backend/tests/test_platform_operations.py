"""Platform-only operations dashboard, summaries, and audited manual actions."""

from __future__ import annotations

import base64
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator
from urllib.parse import urlparse

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
    PaymentEvent,
    PaymentOrder,
    PaymentRecoveryFinding,
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
from app.main import create_app
from app.routers.platform_operations import get_operations_payment_provider
from app.services import platform_operations
from app.services.payment_provider import PaymentQueryResult, TrustedPaymentEvent


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


NOW = datetime.now(timezone.utc).replace(microsecond=0)


class QueryPaymentProvider:
    code = "test-provider"
    app_id = "platform-operations-app"
    merchant_id = "platform-operations-merchant"

    def __init__(self):
        self.query_calls = 0

    def query_payment(self, provider_order_ref):
        self.query_calls += 1
        event = TrustedPaymentEvent(
            provider=self.code,
            provider_event_id="platform-payment-query-event",
            provider_order_ref=provider_order_ref,
            provider_transaction_id="transaction-operations",
            event_type="payment_succeeded",
            app_id=self.app_id,
            merchant_id=self.merchant_id,
            state="SUCCESS",
            amount_cents=10_000,
            currency="CNY",
            succeeded_at=NOW,
            payload_hash="e" * 64,
            source="query",
        )
        return PaymentQueryResult(
            provider=self.code,
            provider_order_ref=provider_order_ref,
            state="SUCCESS",
            status="succeeded",
            success=event,
        )

    def close_payment(self, provider_order_ref):
        raise AssertionError("successful query must not close the order")


def _tables():
    return [
        Tenant.__table__,
        PlatformAdmin.__table__,
        AdminUser.__table__,
        Contact.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        SubscriptionActivation.__table__,
        SubscriptionHistory.__table__,
        ArchiveMessage.__table__,
        MediaFile.__table__,
        PaymentOrder.__table__,
        PaymentRecoveryFinding.__table__,
        PaymentEvent.__table__,
        SubscriptionTermGrant.__table__,
        RefundOrder.__table__,
        RefundEvent.__table__,
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
                PlanEntitlement(
                    id="operations-entitlement",
                    plan_id="operations-plan",
                    capability="unlimited_seats",
                    is_enabled=True,
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
                RefundOrder(
                    id="successful-refund",
                    tenant_id="tenant-active",
                    payment_order_id="paid-order",
                    term_grant_id="term-grant-operations",
                    amount_cents=1_000,
                    currency="CNY",
                    provider="test-provider",
                    provider_ref="refund-order-operations",
                    provider_refund_id="refund-id-operations",
                    provider_state="SUCCESS",
                    status="succeeded",
                    reason_code="approved_refund",
                    approved_by_platform_admin_id="platform-operator",
                    idempotency_key_hash="c" * 64,
                    requested_at=NOW - timedelta(hours=1),
                    succeeded_at=NOW,
                    entitlement_reversed_at=NOW,
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
    client, factory = operations_client
    with factory() as db:
        db.add(
            PaymentRecoveryFinding(
                id="platform-finding-rnd390",
                provider="wechat_pay",
                tenant_id=None,
                payment_order_id=None,
                kind="payment_callback_signature_failure",
                severity="critical",
                status="open",
                dedupe_key="d" * 64,
                occurrence_count=2,
                first_detected_at=NOW,
                last_detected_at=NOW,
            )
        )
        db.commit()

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
    assert body["revenue"]["refund_cents"] == 1_000
    assert body["revenue"]["net_revenue_cents"] == 9_000
    assert body["manual_financial"] == {
        "currency": "CNY",
        "receipt_cents": 0,
        "refund_cents": 500,
        "net_cents": -500,
    }
    assert body["exceptions"]["payment_activation_pending"] == 1
    assert body["exceptions"]["payment_callback_signature_failure"] == 1
    assert body["recent_payment_findings"] == [
        {
            "finding_id": "platform-finding-rnd390",
            "tenant_id": None,
            "payment_order_id": None,
            "kind": "payment_callback_signature_failure",
            "severity": "critical",
            "status": "open",
            "occurrence_count": 2,
            "first_detected_at": NOW.isoformat().replace("+00:00", "Z"),
            "last_detected_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    ]
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
    assert detail.json()["refund_cents"] == 1_000
    assert detail.json()["manual_financial"]["refund_cents"] == 500
    assert detail.json()["payment_orders"][0]["order_id"] == "paid-order"
    assert detail.json()["payment_orders"][0]["provider_order_ref_masked"].endswith(
        "ations"
    )
    assert "order-operations" not in detail.text
    assert "transaction-operations" not in detail.text
    assert "refund-order-operations" not in detail.text
    assert detail.json()["refund_orders"][0]["provider_ref_masked"].endswith(
        "ations"
    )
    assert "content_text" not in detail.json()

    service_headers = {
        **_basic(),
        "Idempotency-Key": "platform-service-rnd405-0001",
    }
    service = client.patch(
        "/api/platform/operations/tenants/tenant-active/service",
        headers=service_headers,
        json={
            "lifecycle_status": "suspended",
            "reason_code": "risk_review",
            "confirmation": "active-co",
        },
    )
    assert service.status_code == 200
    assert service.json()["is_active"] is False
    replay = client.patch(
        "/api/platform/operations/tenants/tenant-active/service",
        headers=service_headers,
        json={
            "lifecycle_status": "suspended",
            "reason_code": "risk_review",
            "confirmation": "active-co",
        },
    )
    assert replay.status_code == 200
    conflict = client.patch(
        "/api/platform/operations/tenants/tenant-active/service",
        headers=service_headers,
        json={
            "lifecycle_status": "active",
            "reason_code": "risk_review",
            "confirmation": "active-co",
        },
    )
    assert conflict.status_code == 409

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
        assert AuditAction.PLATFORM_CONTROL_AUTHORIZED in actions
        assert "platform.subscription_updated" in actions
        financial = actions["platform.manual_financial_transaction_recorded"]
        assert financial["platform_admin_id"] == "platform-operator"
        assert financial["amount_cents"] == 250
        assert "manual-reference" not in str(financial)
        assert "internal-only note" not in str(financial)


def test_gh94_platform_service_suspend_resume_projects_legacy_boolean(
    operations_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = operations_client
    path = "/api/platform/operations/tenants/tenant-active/service"
    common = {
        "reason_code": "risk_review",
        "confirmation": "active-co",
    }

    suspended = client.patch(
        path,
        headers={**_basic(), "Idempotency-Key": "issue-94-suspend"},
        json={**common, "lifecycle_status": "suspended"},
    )
    assert suspended.status_code == 200
    assert suspended.json()["lifecycle_status"] == "suspended"
    assert suspended.json()["is_active"] is False

    resumed = client.patch(
        path,
        headers={**_basic(), "Idempotency-Key": "issue-94-service-resume"},
        json={**common, "lifecycle_status": "active"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["lifecycle_status"] == "active"
    assert resumed.json()["is_active"] is True

    with factory() as db:
        tenant = db.get(Tenant, "tenant-active")
        assert tenant is not None
        assert tenant.lifecycle_status == "active"


def test_rnd405_commercial_filters_sort_and_control_confirmation(
    operations_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = operations_client

    expired = client.get(
        "/api/platform/operations/tenants?subscription_status=expired&sort=ends_asc",
        headers=_basic(),
    )
    assert expired.status_code == 200
    assert [item["tenant_id"] for item in expired.json()["items"]] == [
        "tenant-expired"
    ]
    refund = client.get(
        "/api/platform/operations/tenants?refund_status=succeeded",
        headers=_basic(),
    )
    assert refund.status_code == 200
    assert [item["tenant_id"] for item in refund.json()["items"]] == [
        "tenant-active"
    ]
    payment_exception = client.get(
        "/api/platform/operations/tenants?exception_type=payment",
        headers=_basic(),
    )
    assert payment_exception.status_code == 200
    assert [item["tenant_id"] for item in payment_exception.json()["items"]] == [
        "tenant-active"
    ]
    invalid = client.get(
        "/api/platform/operations/tenants?exception_type=secret",
        headers=_basic(),
    )
    assert invalid.status_code == 422

    wrong_confirmation = client.patch(
        "/api/platform/operations/tenants/tenant-active/service",
        headers={
            **_basic(),
            "Idempotency-Key": "platform-service-rnd405-wrong",
        },
        json={
            "lifecycle_status": "suspended",
            "reason_code": "risk_review",
            "confirmation": "wrong-tenant",
        },
    )
    assert wrong_confirmation.status_code == 422
    with factory() as db:
        tenant = db.get(Tenant, "tenant-active")
        assert tenant is not None and tenant.lifecycle_status == "active"
        rendered = str([row.detail for row in db.query(AuditLog).all()])
        assert "platform-service-rnd405-wrong" not in rendered


def test_rnd405_platform_payment_recovery_is_scoped_audited_and_idempotent(
    operations_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = operations_client
    provider = QueryPaymentProvider()
    client.app.dependency_overrides[get_operations_payment_provider] = lambda: provider
    path = (
        "/api/platform/operations/tenants/tenant-active/"
        "payments/paid-order/query"
    )
    headers = {
        **_basic(),
        "Idempotency-Key": "platform-payment-query-rnd405",
    }
    payload = {
        "reason_code": "activation_recovery",
        "confirmation": "active-co",
    }

    denied = client.post(path, json=payload)
    assert denied.status_code == 401
    first = client.post(path, headers=headers, json=payload)
    replay = client.post(path, headers=headers, json=payload)

    assert first.status_code == 200
    assert first.json()["status"] == "succeeded"
    assert replay.status_code == 200
    assert provider.query_calls == 1
    cross_tenant = client.post(
        path.replace("tenant-active", "tenant-trial"),
        headers={
            **_basic(),
            "Idempotency-Key": "platform-payment-query-cross-tenant",
        },
        json={
            "reason_code": "activation_recovery",
            "confirmation": "trial-co",
        },
    )
    assert cross_tenant.status_code == 404
    assert provider.query_calls == 1

    with factory() as db:
        order = db.get(PaymentOrder, "paid-order")
        assert order is not None and order.status == "succeeded"
        controls = db.query(AuditLog).filter_by(
            action=AuditAction.PLATFORM_CONTROL_AUTHORIZED,
            tenant_id="tenant-active",
        ).all()
        assert len(controls) == 1
        assert controls[0].detail["operation"] == "payment.query"
        assert "platform-payment-query-rnd405" not in str(controls[0].detail)


@pytest.mark.parametrize(
    "classification",
    ["HISTORICAL_VERIFICATION_READINESS_PROBING", "UNKNOWN"],
)
def test_gh111_callback_finding_resolution_is_platform_only_auditable_and_idempotent(
    operations_client: tuple[TestClient, sessionmaker], classification: str
) -> None:
    client, factory = operations_client
    finding_id = "gh111-historical-signature-finding"
    with factory() as db:
        db.add(
            PaymentRecoveryFinding(
                id=finding_id,
                provider="wechat_pay",
                tenant_id=None,
                payment_order_id=None,
                kind="payment_callback_signature_failure",
                severity="critical",
                status="open",
                dedupe_key="e" * 64,
                occurrence_count=6,
                first_detected_at=NOW - timedelta(days=2),
                last_detected_at=NOW - timedelta(days=1),
                created_at=NOW - timedelta(days=2),
            )
        )
        db.commit()
        order_before = [
            (row.id, row.status, row.provider_state, row.recovery_state)
            for row in db.query(PaymentOrder).order_by(PaymentOrder.id)
        ]
        event_count_before = db.query(PaymentEvent).count()
        subscriptions_before = [
            (row.id, row.status, row.ends_at, row.revision)
            for row in db.query(Subscription).order_by(Subscription.id)
        ]
        entitlements_before = [
            (row.id, row.plan_id, row.capability, row.is_enabled)
            for row in db.query(PlanEntitlement).order_by(PlanEntitlement.id)
        ]

    dashboard = client.get("/api/platform/operations/dashboard", headers=_basic())
    assert dashboard.status_code == 200
    assert next(
        item
        for item in dashboard.json()["recent_payment_findings"]
        if item["finding_id"] == finding_id
    )["status"] == "open"
    path = f"/api/platform/operations/payment-findings/{finding_id}/resolve"
    payload = {
        "classification": classification,
        "failure_class": "UNKNOWN",
        "reason_code": "historical_investigation_complete",
    }

    assert client.post(path, json=payload).status_code == 401
    resolved = client.post(path, headers=_basic(), json=payload)
    repeated = client.post(path, headers=_basic(), json=payload)

    assert resolved.status_code == repeated.status_code == 200
    assert repeated.json() == resolved.json()
    assert resolved.json() == {
        "finding_id": finding_id,
        "kind": "payment_callback_signature_failure",
        "status": "resolved",
        "occurrence_count": 6,
        "created_at": (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        "first_detected_at": (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        "last_detected_at": (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "resolved_at": resolved.json()["resolved_at"],
        "classification": classification,
        "failure_class": "UNKNOWN",
        "reason_code": "historical_investigation_complete",
        "resolved_by_platform_admin_id": "platform-operator",
    }
    with factory() as db:
        finding = db.get(PaymentRecoveryFinding, finding_id)
        assert finding is not None
        assert db.query(PaymentRecoveryFinding).count() == 1
        assert finding.status == "resolved"
        assert finding.occurrence_count == 6
        assert finding.created_at.replace(tzinfo=timezone.utc) == NOW - timedelta(days=2)
        assert finding.first_detected_at.replace(tzinfo=timezone.utc) == NOW - timedelta(days=2)
        assert finding.last_detected_at.replace(tzinfo=timezone.utc) == NOW - timedelta(days=1)
        assert finding.resolution_classification == classification
        assert finding.resolution_failure_class == "UNKNOWN"
        assert finding.resolution_reason_code == "historical_investigation_complete"
        assert finding.resolved_by_platform_admin_id == "platform-operator"
        assert finding.resolved_at is not None
        assert order_before == [
            (row.id, row.status, row.provider_state, row.recovery_state)
            for row in db.query(PaymentOrder).order_by(PaymentOrder.id)
        ]
        assert db.query(PaymentEvent).count() == event_count_before
        assert subscriptions_before == [
            (row.id, row.status, row.ends_at, row.revision)
            for row in db.query(Subscription).order_by(Subscription.id)
        ]
        assert entitlements_before == [
            (row.id, row.plan_id, row.capability, row.is_enabled)
            for row in db.query(PlanEntitlement).order_by(PlanEntitlement.id)
        ]


def test_operations_html_is_not_a_tenant_admin_surface(
    operations_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = operations_client

    # RND-414: /platform is the canonical dashboard URL under the new
    # 9-item side nav; /platform/operations (RND-413's original route)
    # stays a first-class alias to the exact same handler rather than a
    # redirect, so unauthenticated browsers still go straight to
    # /platform/login from either URL (see test_rnd413_platform_login.py).
    for dashboard_path in ("/platform", "/platform/operations"):
        denied = client.get(dashboard_path, follow_redirects=False)
        assert denied.status_code == 302, dashboard_path
        assert denied.headers["location"] == "/platform/login", dashboard_path

    allowed = client.get("/platform", headers=_basic())
    assert allowed.status_code == 200
    assert "平台运营" in allowed.text
    assert "不展示聊天内容" in allowed.text
    assert "运营看板" in allowed.text
    assert "渠道确认收款" in allowed.text
    assert "手工账本单独统计" in allowed.text
    assert 'data-plane="platform"' in allowed.text

    static_dir = Path(__file__).resolve().parents[1] / "app/web/static"
    modals = (static_dir / "platform-operations-modals.js").read_text()
    tenant_detail_script = (static_dir / "platform-tenant-detail.js").read_text()
    styles = (static_dir / "platform-operations.css").read_text()
    theme = (static_dir / "platform-console-orange.css").read_text()
    assert "Idempotency-Key" in modals
    assert "provider_order_ref_masked" in tenant_detail_script
    assert '[data-plane="platform"]' in theme
    for path in sorted(static_dir.glob("platform-*.js")):
        # Match actual usage (".innerHTML"), not the bare word — several of
        # these files document the "no innerHTML" discipline in a comment.
        assert ".innerHTML" not in path.read_text(), path
    assert "@media(max-width:1024px)" in styles
    assert "@media(max-width:720px)" in styles


def test_all_platform_console_html_routes_reachable(
    operations_client: tuple[TestClient, sessionmaker],
) -> None:
    """RND-414: every side-nav route serves its shell when authenticated and
    redirects to /platform/login otherwise — a route missing from either
    list would silently 404 for operators."""
    client, _factory = operations_client
    routes = [
        "/platform",
        "/platform/tenants",
        "/platform/tenants/tenant-active",
        "/platform/tenants/new",
        "/platform/audit",
    ]
    for route in routes:
        denied = client.get(route, follow_redirects=False)
        assert denied.status_code == 302, route
        assert denied.headers["location"] == "/platform/login", route

        allowed = client.get(route, headers=_basic())
        assert allowed.status_code == 200, route
        assert 'data-plane="platform"' in allowed.text, route
        assert "平台运营" in allowed.text, route


def _rnd405_postgres_url() -> str | None:
    url = os.getenv("RND405_TEST_DATABASE_URL", "").strip()
    if not url:
        return None
    if not urlparse(url).path.lstrip("/").endswith("test"):
        raise RuntimeError("RND405_TEST_DATABASE_URL must name a database ending in test")
    return url


@pytest.mark.skipif(
    _rnd405_postgres_url() is None,
    reason="RND405_TEST_DATABASE_URL is not configured for isolated PostgreSQL proof",
)
def test_rnd405_concurrent_service_control_replays_once_on_postgresql() -> None:
    engine = create_engine(_rnd405_postgres_url(), pool_size=4)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    with factory() as db:
        db.add_all(
            [
                Tenant(
                    id=tenant_id,
                    name="RND-405 concurrency",
                    slug=f"rnd405-{suffix}",
                ),
                PlatformAdmin(
                    id=admin_id,
                    email=f"rnd405-{suffix}@example.test",
                    password_hash="unused",
                    status="active",
                ),
            ]
        )
        db.commit()

    def apply_once(_index):
        with factory() as db:
            tenant = platform_operations.set_service_status(
                db,
                tenant_id,
                "suspended",
                platform_admin_id=admin_id,
                reason_code="concurrency_review",
                confirmation=f"rnd405-{suffix}",
                idempotency_key=f"rnd405-service-{suffix}",
            )
            db.commit()
            return tenant.lifecycle_status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert set(executor.map(apply_once, range(2))) == {"suspended"}
        with factory() as db:
            assert db.query(AuditLog).filter_by(
                tenant_id=tenant_id,
                action=AuditAction.PLATFORM_CONTROL_AUTHORIZED,
            ).count() == 1
            assert db.query(AuditLog).filter_by(
                tenant_id=tenant_id,
                action=AuditAction.PLATFORM_TENANT_SUSPENDED,
            ).count() == 1
    finally:
        with factory() as db:
            db.query(AuditLog).filter_by(tenant_id=tenant_id).delete()
            db.query(Tenant).filter_by(id=tenant_id).delete()
            db.query(PlatformAdmin).filter_by(id=admin_id).delete()
            db.commit()
        engine.dispose()
