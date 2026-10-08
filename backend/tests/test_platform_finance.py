"""Platform finance 订单明细 page + read-only order listing API.

Covers: platform-admin gating (page redirect, API 401), the unified
receipt/refund listing order, confirmed-money summary semantics (mirrors
platform_operations._financial_summary), kind/status filters and their
validation, tenant search, and pagination.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db.base import Base
from app.db.models import (
    BillingPlan,
    PaymentOrder,
    PlatformAdmin,
    RefundOrder,
    Tenant,
)
from app.db.session import get_db
from app.main import create_app

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _tables():
    return [
        PlatformAdmin.__table__,
        Tenant.__table__,
        BillingPlan.__table__,
        PaymentOrder.__table__,
        RefundOrder.__table__,
    ]


def _basic() -> dict[str, str]:
    token = base64.b64encode(b"finance@example.test:test-password").decode()
    return {"Authorization": f"Basic {token}"}


def _payment_order(order_id: str, tenant_id: str, *, status: str, created_at, paid_at=None) -> PaymentOrder:
    paid_status = status in ("paid_activation_pending", "succeeded")
    succeeded = status == "succeeded"
    return PaymentOrder(
        id=order_id,
        tenant_id=tenant_id,
        plan_id="finance-plan",
        plan_code="annual_base_cny_99",
        plan_name="年度基础套餐",
        amount_cents=9_900,
        currency="CNY",
        provider="wechat_pay",
        provider_order_ref=f"ref-{order_id}",
        provider_transaction_id=f"txn-{order_id}" if paid_status else None,
        status=status,
        idempotency_key_hash=order_id.ljust(64, "0"),
        created_at=created_at,
        expires_at=created_at + timedelta(days=1),
        paid_at=paid_at,
        # ck_payment_orders_activation_result: succeeded requires activation
        # evidence; the FK itself is unenforced in the SQLite table subset.
        activation_id=f"activation-{order_id}" if succeeded else None,
        activated_at=paid_at if succeeded else None,
        failure_code="provider_rejected" if status == "failed" else None,
    )


def _refund_order(order_id: str, tenant_id: str, payment_order_id: str, *, status: str, requested_at, succeeded_at=None) -> RefundOrder:
    succeeded = status == "succeeded"
    return RefundOrder(
        id=order_id,
        tenant_id=tenant_id,
        payment_order_id=payment_order_id,
        term_grant_id=f"grant-{order_id}",
        amount_cents=9_900,
        currency="CNY",
        provider="wechat_pay",
        provider_ref=f"refund-ref-{order_id}",
        provider_refund_id=f"refund-{order_id}" if succeeded else None,
        provider_state="SUCCESS" if succeeded else None,
        status=status,
        reason_code="tenant_request",
        approved_by_platform_admin_id="finance-admin",
        idempotency_key_hash=order_id.ljust(64, "0"),
        requested_at=requested_at,
        succeeded_at=succeeded_at,
        entitlement_reversed_at=succeeded_at,
    )


@pytest.fixture()
def finance_client() -> Generator[tuple[TestClient, sessionmaker], None, None]:
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
                PlatformAdmin(
                    id="finance-admin",
                    email="finance@example.test",
                    password_hash=hash_password("test-password"),
                    status="active",
                ),
                Tenant(id="tenant-alpha", name="Alpha 客户", slug="alpha"),
                Tenant(id="tenant-beta", name="Beta 客户", slug="beta"),
                BillingPlan(
                    id="finance-plan",
                    code="annual_base_cny_99",
                    display_name="年度基础套餐",
                    amount_cents=9_900,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=5_368_709_120,
                    is_active=True,
                ),
                # Newest confirmed receipt first when sorting desc.
                _payment_order("receipt-old", "tenant-alpha", status="succeeded", created_at=NOW - timedelta(days=10), paid_at=NOW - timedelta(days=9)),
                _payment_order("receipt-new", "tenant-beta", status="succeeded", created_at=NOW - timedelta(days=2), paid_at=NOW - timedelta(days=1)),
                _payment_order("receipt-pending", "tenant-alpha", status="pending", created_at=NOW - timedelta(hours=5)),
                _payment_order("receipt-failed", "tenant-beta", status="failed", created_at=NOW - timedelta(hours=4)),
                _refund_order("refund-succeeded", "tenant-alpha", "receipt-old", status="succeeded", requested_at=NOW - timedelta(hours=3), succeeded_at=NOW - timedelta(hours=2)),
                _refund_order("refund-processing", "tenant-beta", "receipt-new", status="processing", requested_at=NOW - timedelta(hours=1)),
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


def test_orders_api_requires_platform_admin(finance_client) -> None:
    client, _factory = finance_client
    response = client.get("/api/platform/finance/orders")
    assert response.status_code == 401


def test_orders_page_redirects_anonymous_to_platform_login(finance_client) -> None:
    client, _factory = finance_client
    response = client.get("/platform/finance/orders", follow_redirects=False)
    assert response.status_code in (301, 302, 307)
    assert response.headers["location"] == "/platform/login"


def test_orders_page_renders_finance_nav_group(finance_client) -> None:
    client, _factory = finance_client
    response = client.get("/platform/finance/orders", headers=_basic())
    assert response.status_code == 200
    body = response.text
    assert "订单明细" in body
    assert "/platform/finance/orders" in body
    assert 'data-group="财务"' in body or ">财务<" in body


def test_orders_listing_sorts_and_summarizes_confirmed_money(finance_client) -> None:
    client, _factory = finance_client
    response = client.get("/api/platform/finance/orders", headers=_basic())
    assert response.status_code == 200
    data = response.json()
    assert data["kind"] == "all"
    assert data["total"] == 6
    # Descending by money-movement time: refund succeeded most recently.
    assert [item["order_id"] for item in data["items"]] == [
        "refund-processing",
        "refund-succeeded",
        "receipt-failed",
        "receipt-pending",
        "receipt-new",
        "receipt-old",
    ]
    first_refund = data["items"][0]
    assert first_refund["kind"] == "refund"
    assert first_refund["reason_code"] == "tenant_request"
    assert first_refund["plan_name"] is None
    first_receipt = next(item for item in data["items"] if item["order_id"] == "receipt-new")
    assert first_receipt["kind"] == "receipt"
    assert first_receipt["plan_name"] == "年度基础套餐"
    assert first_receipt["status"] == "succeeded"
    # Confirmed money only: 2 succeeded receipts, 1 succeeded refund.
    summary = data["summary"]
    assert summary["receipt_count"] == 2
    assert summary["receipt_cents"] == 19_800
    assert summary["refund_count"] == 1
    assert summary["refund_cents"] == 9_900
    assert summary["net_cents"] == 9_900
    assert summary["currency"] == "CNY"


def test_orders_kind_and_status_filters(finance_client) -> None:
    client, _factory = finance_client
    refunds = client.get("/api/platform/finance/orders?kind=refund", headers=_basic()).json()
    assert refunds["total"] == 2
    assert all(item["kind"] == "refund" for item in refunds["items"])
    # Filter-scoped summary: receipts drop out entirely.
    assert refunds["summary"]["receipt_count"] == 0
    assert refunds["summary"]["refund_count"] == 1

    pending = client.get(
        "/api/platform/finance/orders?kind=receipt&status=pending", headers=_basic()
    ).json()
    assert pending["total"] == 1
    assert pending["items"][0]["order_id"] == "receipt-pending"
    # Unconfirmed statuses never enter the money summary.
    assert pending["summary"]["receipt_cents"] == 0


def test_orders_filter_validation_is_fail_closed(finance_client) -> None:
    client, _factory = finance_client
    assert client.get("/api/platform/finance/orders?kind=bogus", headers=_basic()).status_code == 422
    # A refund-only status is invalid while kind=receipt.
    assert (
        client.get(
            "/api/platform/finance/orders?kind=receipt&status=processing", headers=_basic()
        ).status_code
        == 422
    )
    assert (
        client.get("/api/platform/finance/orders?status=not-a-status", headers=_basic()).status_code
        == 422
    )


def test_orders_tenant_search_and_pagination(finance_client) -> None:
    client, _factory = finance_client
    alpha = client.get("/api/platform/finance/orders?q=alpha", headers=_basic()).json()
    assert alpha["total"] == 3
    assert {item["tenant_name"] for item in alpha["items"]} == {"Alpha 客户"}

    slug = client.get("/api/platform/finance/orders?q=beta", headers=_basic()).json()
    assert slug["total"] == 3

    page = client.get(
        "/api/platform/finance/orders?page=2&page_size=4", headers=_basic()
    ).json()
    assert page["total"] == 6
    assert page["page"] == 2
    assert len(page["items"]) == 2
