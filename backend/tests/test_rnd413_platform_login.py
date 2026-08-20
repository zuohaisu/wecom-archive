"""RND-413 platform-admin login page, session cookie, and WWW-Authenticate.

Covers the ticket's regression contract:
- both 401 branches of require_platform_admin carry
  ``WWW-Authenticate: Basic realm="platform admin"``;
- the /platform/login page, session issuance/expiry/revocation;
- /platform/operations redirects unauthenticated browsers to the login
  page while API clients keep HTTP Basic.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit import AuditAction
from app.auth import hash_password, require_platform_admin
from app.db.base import Base
from app.db.models import AuditLog, PlatformAdmin, PlatformAdminSession, Tenant
from app.db.session import get_db
from app.main import create_app

PLATFORM_COOKIE = "platform_session_id"
WWW_AUTHENTICATE = 'Basic realm="platform admin"'


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _tables():
    return [
        Tenant.__table__,
        PlatformAdmin.__table__,
        PlatformAdminSession.__table__,
        AuditLog.__table__,
    ]


def _basic(email: str = "platform@example.test", password: str = "secret123") -> dict[str, str]:
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def platform_client() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=_tables())
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id="tenant-default", name="Default", slug="default"))
        db.add(
            PlatformAdmin(
                id="platform-admin",
                email="platform@example.test",
                password_hash=hash_password("secret123"),
                status="active",
            )
        )
        db.commit()

    app = create_app()

    @app.get("/platform-auth-probe")
    def _probe(_admin: PlatformAdmin = Depends(require_platform_admin)) -> dict:
        return {"ok": True}

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


def test_unauthenticated_api_401_carries_www_authenticate(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = platform_client
    response = client.get("/platform-auth-probe")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE


def test_invalid_basic_401_carries_www_authenticate(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = platform_client
    response = client.get("/platform-auth-probe", headers=_basic(password="wrong-password"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE


def test_operations_page_redirects_unauthenticated_browser_to_login(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = platform_client
    response = client.get("/platform/operations", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/platform/login"


def test_operations_page_still_accepts_http_basic(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = platform_client
    response = client.get("/platform/operations", headers=_basic())
    assert response.status_code == 200
    assert "平台运营" in response.text
    # RND-414: the admin-bar logout affordance is now a plain "退出" button
    # (approved design's admin-bar copy), not "退出登录".
    assert "退出" in response.text


def test_login_page_renders(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = platform_client
    response = client.get("/platform/login")
    assert response.status_code == 200
    assert "平台运营控制台" in response.text
    assert 'id="login-form"' in response.text
    assert "'/platform/login'" in response.text
    assert 'name="email"' in response.text
    assert 'name="password"' in response.text


def test_login_page_redirects_when_already_authenticated(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = platform_client
    client.post(
        "/platform/login",
        json={"email": "platform@example.test", "password": "secret123"},
    )
    response = client.get("/platform/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/platform/operations"


def test_login_success_issues_session_and_grants_access(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = platform_client
    response = client.post(
        "/platform/login",
        json={"email": "platform@example.test", "password": "secret123"},
    )
    assert response.status_code == 200
    assert response.json() == {"logged_in": True}
    cookie = response.cookies.get(PLATFORM_COOKIE)
    assert cookie

    with factory() as db:
        row = db.get(PlatformAdminSession, cookie)
        assert row is not None
        assert row.is_revoked is False
        assert row.platform_admin_id == "platform-admin"
        logins = db.query(AuditLog).filter_by(action=AuditAction.LOGIN).all()
        assert len(logins) == 1
        assert logins[0].detail == {"platform_admin_id": "platform-admin"}

    # The session cookie now satisfies both the HTML page and the API.
    assert client.get("/platform/operations").status_code == 200
    assert client.get("/platform-auth-probe").status_code == 200


def test_login_failure_rerenders_with_error_and_no_session(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = platform_client
    response = client.post(
        "/platform/login",
        json={"email": "platform@example.test", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_credentials"}
    assert PLATFORM_COOKIE not in response.cookies

    with factory() as db:
        assert db.query(PlatformAdminSession).count() == 0
        failed = db.query(AuditLog).filter_by(action=AuditAction.LOGIN_FAILED).all()
        assert len(failed) == 1
        assert failed[0].detail == {"platform_admin_id": None}


def test_logout_revokes_session_and_clears_cookie(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = platform_client
    client.post(
        "/platform/login",
        json={"email": "platform@example.test", "password": "secret123"},
    )
    cookie = client.cookies.get(PLATFORM_COOKIE)
    assert cookie

    response = client.post("/platform/logout", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/platform/login"

    with factory() as db:
        row = db.get(PlatformAdminSession, cookie)
        assert row is not None and row.is_revoked is True
        logouts = db.query(AuditLog).filter_by(action=AuditAction.LOGOUT).all()
        assert len(logouts) == 1
        assert logouts[0].detail == {"platform_admin_id": "platform-admin"}

    # The revoked cookie no longer grants access anywhere.
    assert client.get("/platform/operations", follow_redirects=False).status_code == 302
    assert client.get("/platform-auth-probe").status_code == 401


def test_expired_session_is_rejected(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = platform_client
    with factory() as db:
        db.add(
            PlatformAdminSession(
                id="expired-session",
                platform_admin_id="platform-admin",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
                is_revoked=False,
            )
        )
        db.commit()

    client.cookies.set(PLATFORM_COOKIE, "expired-session")
    api = client.get("/platform-auth-probe")
    assert api.status_code == 401
    assert api.headers["www-authenticate"] == WWW_AUTHENTICATE
    page = client.get("/platform/operations", follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"] == "/platform/login"


def test_tenant_admin_session_cookie_never_grants_platform_access(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    """A tenant admin session_id cookie must not satisfy platform auth."""
    client, _factory = platform_client
    client.cookies.set("session_id", "tenant-session")
    response = client.get("/platform-auth-probe")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == WWW_AUTHENTICATE
