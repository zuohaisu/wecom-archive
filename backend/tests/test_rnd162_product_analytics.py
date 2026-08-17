"""RND-162 product-use analytics: privacy, isolation, degradation and queries."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE, hash_password
from app.db.base import Base
from app.db.models import AppConfigStore, AdminSession, AdminUser, PlatformAdmin, ProductAnalyticsEvent, Tenant
from app.db.session import get_db
from app.schemas.product_analytics import (
    CONVERSATION_OPENED,
    LOGIN_FAILED,
    LOGIN_SUCCEEDED,
    REVIEW_OPENED,
)
from app.services import product_analytics


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)


def _basic() -> dict[str, str]:
    token = base64.b64encode(b"platform@example.test:test-password").decode()
    return {"Authorization": f"Basic {token}"}


def _event(
    *,
    event_name: str,
    tenant_id: str | None,
    admin_user_id: str | None,
    occurred_at: datetime,
    attributes: dict | None = None,
) -> ProductAnalyticsEvent:
    return ProductAnalyticsEvent(
        event_id=str(uuid.uuid4()),
        event_name=event_name,
        schema_version=1,
        event_class=product_analytics.event_class(event_name),
        source="backend" if event_name.startswith("product.auth") else "frontend",
        tenant_id=tenant_id,
        admin_user_id=admin_user_id,
        attributes=attributes or {},
        occurred_at=occurred_at,
        received_at=occurred_at,
    )


@pytest.fixture()
def analytics_client(monkeypatch) -> Generator[tuple[TestClient, sessionmaker], None, None]:
    monkeypatch.setenv("PRODUCT_ANALYTICS_ENABLED", "true")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            AppConfigStore.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            PlatformAdmin.__table__,
            ProductAnalyticsEvent.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                Tenant(id="tenant-a", name="Active Co", slug="active-co"),
                Tenant(id="tenant-b", name="Quiet Co", slug="quiet-co"),
                AdminUser(
                    id="tenant-a-admin",
                    tenant_id="tenant-a",
                    wecom_user_id="internal-a",
                    name="Tenant A Admin",
                    status="active",
                ),
                AdminUser(
                    id="tenant-b-admin",
                    tenant_id="tenant-b",
                    wecom_user_id="internal-b",
                    name="Tenant B Admin",
                    status="active",
                ),
                AdminSession(
                    id="tenant-a-session",
                    admin_user_id="tenant-a-admin",
                    tenant_id="tenant-a",
                    wecom_user_id="internal-a",
                    expires_at=NOW + timedelta(days=1),
                    is_revoked=False,
                ),
                PlatformAdmin(
                    id="platform-admin",
                    email="platform@example.test",
                    password_hash=hash_password("test-password"),
                    status="active",
                ),
            ]
        )
        db.commit()

    from app.main import create_app

    app = create_app()

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            client.cookies.set(SESSION_COOKIE, "tenant-a-session")
            yield client, factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _window() -> dict[str, str]:
    return {
        "starts_at": (NOW - timedelta(days=30)).isoformat(),
        "ends_at": (NOW + timedelta(minutes=1)).isoformat(),
    }


def test_collection_is_session_scoped_idempotent_and_rejects_sensitive_fields(
    analytics_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = analytics_client
    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "event_name": REVIEW_OPENED,
        "occurred_at": NOW.isoformat(),
    }

    assert client.post("/api/product-analytics/events", json=payload).status_code == 202
    assert client.post("/api/product-analytics/events", json=payload).status_code == 202
    forbidden = client.post(
        "/api/product-analytics/events",
        json={**payload, "event_id": str(uuid.uuid4()), "search_keyword": "never-store-this"},
    )
    assert forbidden.status_code == 422
    spoofed_tenant = client.post(
        "/api/product-analytics/events",
        json={**payload, "event_id": str(uuid.uuid4()), "tenant_id": "tenant-b"},
    )
    assert spoofed_tenant.status_code == 422

    with factory() as db:
        rows = db.query(ProductAnalyticsEvent).all()
        assert len(rows) == 1
        assert rows[0].tenant_id == "tenant-a"
        assert rows[0].admin_user_id == "tenant-a-admin"
        assert rows[0].attributes == {}
        assert "never-store-this" not in str(rows)


def test_collection_is_disabled_without_creating_events(
    analytics_client: tuple[TestClient, sessionmaker], monkeypatch
) -> None:
    client, factory = analytics_client
    monkeypatch.setenv("PRODUCT_ANALYTICS_ENABLED", "false")

    response = client.post(
        "/api/product-analytics/events",
        json={
            "event_id": str(uuid.uuid4()),
            "event_name": REVIEW_OPENED,
            "occurred_at": NOW.isoformat(),
        },
    )

    assert response.status_code == 202
    with factory() as db:
        assert db.query(ProductAnalyticsEvent).count() == 0


def test_collection_failure_is_best_effort_and_does_not_change_the_response(
    analytics_client: tuple[TestClient, sessionmaker], monkeypatch
) -> None:
    client, factory = analytics_client

    def broken_record(*_args, **_kwargs):
        raise RuntimeError("analytics storage unavailable")

    monkeypatch.setattr(product_analytics, "record_frontend_event", broken_record)
    response = client.post(
        "/api/product-analytics/events",
        json={
            "event_id": str(uuid.uuid4()),
            "event_name": REVIEW_OPENED,
            "occurred_at": NOW.isoformat(),
        },
    )

    assert response.status_code == 202
    with factory() as db:
        assert db.query(ProductAnalyticsEvent).count() == 0


def test_login_succeeds_when_best_effort_backend_analytics_is_dropped(
    analytics_client: tuple[TestClient, sessionmaker], monkeypatch
) -> None:
    client, factory = analytics_client
    password = "test-password"
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("ADMIN_USERNAME", "default-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(password))
    with factory() as db:
        db.add(Tenant(id="default-tenant", name="Default", slug="default"))
        db.commit()

    monkeypatch.setattr(product_analytics, "record_backend_event_best_effort", lambda *_args, **_kwargs: False)
    response = client.post(
        "/api/auth/password/login",
        json={"username": "default-admin", "password": password},
    )

    assert response.status_code == 200
    assert response.json() == {"logged_in": True}


def test_platform_overview_and_tenant_detail_are_aggregate_only(
    analytics_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = analytics_client
    with factory() as db:
        db.add_all(
            [
                _event(
                    event_name=LOGIN_SUCCEEDED,
                    tenant_id="tenant-a",
                    admin_user_id="tenant-a-admin",
                    occurred_at=NOW - timedelta(days=2),
                ),
                _event(
                    event_name=LOGIN_FAILED,
                    tenant_id="tenant-a",
                    admin_user_id=None,
                    occurred_at=NOW - timedelta(days=2),
                ),
                _event(
                    event_name=REVIEW_OPENED,
                    tenant_id="tenant-a",
                    admin_user_id="tenant-a-admin",
                    occurred_at=NOW - timedelta(days=1),
                ),
                _event(
                    event_name=CONVERSATION_OPENED,
                    tenant_id="tenant-a",
                    admin_user_id="tenant-a-admin",
                    occurred_at=NOW - timedelta(days=1),
                    attributes={},
                ),
                _event(
                    event_name=LOGIN_SUCCEEDED,
                    tenant_id="tenant-b",
                    admin_user_id="tenant-b-admin",
                    occurred_at=NOW - timedelta(days=1),
                ),
            ]
        )
        db.commit()

    overview = client.get("/api/platform/operations/product-analytics/overview", params=_window(), headers=_basic())
    assert overview.status_code == 200
    body = overview.json()
    assert body["provisioned_tenant_count"] == 2
    assert body["active_tenant_count"] == 1
    assert body["inactive_tenant_count"] == 1
    assert body["active_admin_count"] == 1
    assert body["login"] == {"success_count": 2, "failure_count": 1, "failure_rate": pytest.approx(1 / 3)}
    adoption = {item["event_name"]: item for item in body["function_adoption"]}
    assert adoption[REVIEW_OPENED]["tenant_count"] == 1
    assert adoption[CONVERSATION_OPENED]["admin_count"] == 1
    assert "attributes" not in overview.text

    tenants = client.get(
        "/api/platform/operations/product-analytics/tenants",
        params={**_window(), "activity_status": "inactive"},
        headers=_basic(),
    )
    assert tenants.status_code == 200
    assert [item["tenant_id"] for item in tenants.json()["items"]] == ["tenant-b"]

    detail = client.get(
        "/api/platform/operations/product-analytics/tenants/tenant-a",
        params=_window(),
        headers=_basic(),
    )
    assert detail.status_code == 200
    assert detail.json()["last_product_event_at"] is not None
    assert detail.json()["login"]["failure_count"] == 1
    assert "attributes" not in detail.text


def test_platform_analytics_rejects_tenant_sessions_and_bounds_queries(
    analytics_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = analytics_client

    denied = client.get("/api/platform/operations/product-analytics/overview", params=_window())
    assert denied.status_code == 401
    invalid_event = client.get(
        "/api/platform/operations/product-analytics/overview",
        params={**_window(), "event_name": "arbitrary.event.v1"},
        headers=_basic(),
    )
    assert invalid_event.status_code == 422
    too_wide = client.get(
        "/api/platform/operations/product-analytics/overview",
        params={
            "starts_at": (NOW - timedelta(days=94)).isoformat(),
            "ends_at": NOW.isoformat(),
        },
        headers=_basic(),
    )
    assert too_wide.status_code == 422


def test_platform_operations_ui_exposes_only_aggregate_product_analytics() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "app/web/templates/platform_operations.html").read_text(encoding="utf-8")
    script = (root / "app/web/static/platform-operations.js").read_text(encoding="utf-8")

    assert "产品使用分析" in template
    assert "近期未使用" in template
    assert "product-analytics/overview" in script
    assert "product-analytics/tenants" in script
    assert "content_text" not in template
    assert "innerHTML" not in script


def test_retention_cleanup_and_backend_failure_are_safe(
    analytics_client: tuple[TestClient, sessionmaker], monkeypatch
) -> None:
    _client, factory = analytics_client
    with factory() as db:
        db.add_all(
            [
                _event(
                    event_name=REVIEW_OPENED,
                    tenant_id="tenant-a",
                    admin_user_id="tenant-a-admin",
                    occurred_at=NOW - timedelta(days=181),
                ),
                _event(
                    event_name=REVIEW_OPENED,
                    tenant_id="tenant-a",
                    admin_user_id="tenant-a-admin",
                    occurred_at=NOW - timedelta(days=179),
                ),
            ]
        )
        db.commit()
        assert product_analytics.purge_expired_events(db, at=NOW) == 1
        db.commit()
        assert db.query(ProductAnalyticsEvent).count() == 1

    def broken_factory(*_args, **_kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(product_analytics, "sessionmaker", broken_factory)
    with factory() as db:
        assert product_analytics.record_backend_event_best_effort(
            db,
            event_name=LOGIN_SUCCEEDED,
            tenant_id="tenant-a",
            admin_user_id="tenant-a-admin",
        ) is False
