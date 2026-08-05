"""Acceptance coverage for RND-309's audited, deny-by-default access gate."""

from __future__ import annotations

import base64
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth import PLATFORM_TENANT_ACCESS_ACTION, hash_password
from app.db.models import AuditLog, PlatformAdmin, Tenant
from app.db.session import get_db
from tests.test_reachability_audit import configure_sqlite_for_savepoints


def _basic(password: str = "test-password") -> dict[str, str]:
    token = base64.b64encode(f"platform@example.test:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def content_access_client() -> Generator[tuple[TestClient, Session], None, None]:
    from app.main import create_app

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    configure_sqlite_for_savepoints(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "slug TEXT NOT NULL, is_active BOOLEAN NOT NULL DEFAULT 1, "
                "lifecycle_status TEXT NOT NULL DEFAULT 'active', "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, onboarding_completed_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE platform_admins (id TEXT PRIMARY KEY, email TEXT NOT NULL, "
                "password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'superadmin', "
                "status TEXT NOT NULL DEFAULT 'active', "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, last_active_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE audit_logs (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
                "admin_user_id TEXT, action TEXT NOT NULL, object_type TEXT NOT NULL, "
                "object_id TEXT, detail JSON, created_at DATETIME NOT NULL)"
            )
        )
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    db.add(Tenant(id="tenant-rnd309", name="RND-309 Tenant", slug="rnd309"))
    db.add(
        PlatformAdmin(
            id="platform-admin-rnd309",
            email="platform@example.test",
            password_hash=hash_password("test-password"),
            role="superadmin",
            status="active",
        )
    )
    db.commit()

    app = create_app()

    def override_db() -> Generator[Session, None, None]:
        request_db = session_factory()
        try:
            yield request_db
        finally:
            request_db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def test_content_access_request_records_exactly_one_audit_row_and_returns_no_content(
    content_access_client: tuple[TestClient, Session],
) -> None:
    client, db = content_access_client

    response = client.post(
        "/api/platform/content-access-requests?tenant_id=tenant-rnd309", headers=_basic()
    )

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "tenant-rnd309",
        "recorded": True,
        "granted": False,
        "note": "access request recorded; this endpoint does not return message content",
    }
    rows = db.query(AuditLog).filter(AuditLog.tenant_id == "tenant-rnd309").all()
    assert len(rows) == 1
    assert rows[0].action == PLATFORM_TENANT_ACCESS_ACTION
    assert rows[0].object_id == "tenant-rnd309"
    assert rows[0].detail == {"platform_admin_id": "platform-admin-rnd309"}


@pytest.mark.parametrize(
    "headers",
    [None, _basic(password="wrong-password")],
)
def test_content_access_request_rejects_invalid_platform_credentials(
    content_access_client: tuple[TestClient, Session], headers: dict[str, str] | None
) -> None:
    client, db = content_access_client

    response = client.post(
        "/api/platform/content-access-requests?tenant_id=tenant-rnd309", headers=headers
    )

    assert response.status_code == 401
    assert db.query(AuditLog).count() == 0


def test_tenant_admin_session_cannot_request_content_access(
    content_access_client: tuple[TestClient, Session],
) -> None:
    client, db = content_access_client
    client.cookies.set("session_id", "tenant-admin-session")

    response = client.post("/api/platform/content-access-requests?tenant_id=tenant-rnd309")

    assert response.status_code == 401
    assert db.query(AuditLog).count() == 0


def test_content_access_request_requires_explicit_tenant_id(
    content_access_client: tuple[TestClient, Session],
) -> None:
    client, db = content_access_client

    response = client.post("/api/platform/content-access-requests", headers=_basic())

    assert response.status_code == 422
    assert db.query(AuditLog).count() == 0
