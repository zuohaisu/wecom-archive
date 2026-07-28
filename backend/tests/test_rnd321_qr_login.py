"""RND-321 — PC WeCom QR login uses the existing OAuth session flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import AdminSession, AdminUser, Base, Tenant, TenantWecomConfig


class _Response:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self):
        return self.payload


class _WeComClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, url, params=None):
        if "getuserinfo" in url:
            return _Response({"errcode": 0, "UserId": "qr-user"})
        if "user/get" in url:
            return _Response({"errcode": 0, "userid": "qr-user", "status": 1, "name": "QR User"})
        raise AssertionError(f"unexpected WeCom URL: {url}")


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, TenantWecomConfig.__table__, AdminUser.__table__, AdminSession.__table__],
    )
    yield engine
    engine.dispose()


@pytest.fixture()
def client(db_engine):
    from app.db.session import get_db
    from app.main import app

    def override_db():
        with Session(db_engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _seed_config(engine, corp_id: str = "corp-qr") -> None:
    with Session(engine) as db:
        db.add(Tenant(id="tenant-qr", name="QR tenant", slug="qr", is_active=True))
        db.add(
            TenantWecomConfig(
                id="config-qr",
                tenant_id="tenant-qr",
                corp_id=corp_id,
                agent_id="100001",
                app_secret="test-only",
                is_active=True,
            )
        )
        db.commit()


def _set_wecom_env(monkeypatch) -> None:
    monkeypatch.setenv("WECOM_CORP_ID", "corp-qr")
    monkeypatch.setenv("WECOM_AGENT_ID", "100001")
    monkeypatch.setenv("WECOM_OAUTH_SECRET", "test-only")
    monkeypatch.setenv("ADMIN_DOMAIN", "admin.example.test")


def test_qr_login_redirects_to_qrconnect_and_login_page_has_i18n(client, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    response = client.get("/api/auth/wecom/qr/login", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert "open.work.weixin.qq.com/wwopen/sso/qrConnect" in location
    assert "state=" in location
    assert "self_redirect=true" in location

    page = client.get("/admin/login")
    assert 'data-i18n="login.qrTitle"' in page.text
    assert 'data-i18n="login.qrScanHint"' in page.text


def test_qr_login_missing_config_fails_closed(client, monkeypatch) -> None:
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.delenv("WECOM_AGENT_ID", raising=False)
    response = client.get("/api/auth/wecom/qr/login", follow_redirects=False)
    assert response.headers["location"] == "/admin/login?error=config_error"


def test_qr_callback_rejects_invalid_and_reused_state_without_cookie(client, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    invalid = client.get(
        "/api/auth/wecom/qr/callback?code=x&state=forged",
        follow_redirects=False,
    )
    assert invalid.headers["location"] == "/admin/login?error=invalid_state"
    assert "session_id" not in invalid.headers.get("set-cookie", "")

    started = client.get("/api/auth/wecom/qr/login", follow_redirects=False)
    state = started.headers["location"].split("state=")[1].split("&")[0]
    monkeypatch.delenv("WECOM_OAUTH_SECRET")
    first = client.get(
        f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
    )
    assert first.headers["location"] == "/admin/login?error=config_error"
    reused = client.get(
        f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
    )
    assert reused.headers["location"] == "/admin/login?error=invalid_state"
    assert "session_id" not in reused.headers.get("set-cookie", "")


def test_qr_callback_upserts_user_and_creates_tenant_bound_session(client, db_engine, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    _seed_config(db_engine)
    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _WeComClient
    ):
        started = client.get("/api/auth/wecom/qr/login", follow_redirects=False)
        state = started.headers["location"].split("state=")[1].split("&")[0]
        response = client.get(
            f"/api/auth/wecom/qr/callback?code=one-time-code&state={state}",
            follow_redirects=False,
        )
        repeat_state = client.get(
            "/api/auth/wecom/qr/login", follow_redirects=False
        ).headers["location"].split("state=")[1].split("&")[0]
        repeated = client.get(
            f"/api/auth/wecom/qr/callback?code=another-code&state={repeat_state}",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert repeated.status_code == 302
    assert response.headers["location"] == "/admin/conversations"
    assert "session_id" in response.headers.get("set-cookie", "")
    with Session(db_engine) as db:
        assert db.query(AdminUser).filter_by(tenant_id="tenant-qr", wecom_user_id="qr-user").count() == 1
        sessions = db.query(AdminSession).all()
        assert len(sessions) == 2
        assert all(session.tenant_id == "tenant-qr" for session in sessions)
        assert all(session.wecom_user_id == "qr-user" for session in sessions)


def test_qr_callback_fails_closed_without_tenant_config_or_for_inactive_user(client, db_engine, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _WeComClient
    ):
        state = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
        missing_tenant = client.get(
            f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
        )
    assert missing_tenant.headers["location"] == "/admin/login?error=config_error"

    _seed_config(db_engine)

    class _InactiveClient(_WeComClient):
        def get(self, url, params=None):
            if "getuserinfo" in url:
                return _Response({"errcode": 0, "UserId": "qr-user"})
            return _Response({"errcode": 0, "userid": "qr-user", "status": 2})

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _InactiveClient
    ):
        state = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
        inactive = client.get(
            f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
        )
    assert inactive.headers["location"] == "/admin/login?error=user_inactive"
    assert "session_id" not in inactive.headers.get("set-cookie", "")
