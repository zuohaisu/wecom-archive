"""Acceptance coverage for RND-250 first-run initialization, plus
env-bootstrap retirement (RND-386): password login must work with only
per-user accounts and bootstrap must stay closed once real users exist."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Generator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user, hash_password
from app.config import resolver
from app.config.guard import is_initialized
from app.db.base import Base
from app.db.models import AdminSession, AdminUser, AppConfigStore, Tenant
from app.db.session import get_db


@pytest.fixture()
def bootstrap_client(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, sessionmaker], None, None]:
    """A complete password-login database with an initially blank config store."""
    from app.main import app

    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    for name in (
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD_HASH",
        "WECOM_CORP_ID",
        "WECOM_AGENT_ID",
        "WECOM_OAUTH_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            AppConfigStore.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Tenant(id="tenant-default", name="Default", slug="default", is_active=True))
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        request_db = factory()
        try:
            yield request_db
        finally:
            request_db.close()

    resolver.invalidate()
    monkeypatch.setattr("app.routers.auth.write_audit", lambda *args, **kwargs: None)
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, factory
    app.dependency_overrides.clear()
    resolver.invalidate()
    db.close()
    engine.dispose()


def _stored(factory: sessionmaker, key: str) -> AppConfigStore | None:
    db = factory()
    try:
        return db.get(AppConfigStore, key)
    finally:
        db.close()


def test_uninitialized_without_database_or_environment_password(
    bootstrap_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = bootstrap_client
    db = factory()
    try:
        assert is_initialized(db) is False
    finally:
        db.close()
    assert client.get("/api/admin/settings/bootstrap-status").json() == {"initialized": False}


def test_environment_password_remains_initialized_and_cannot_be_overwritten(
    bootstrap_client: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory = bootstrap_client
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("env-password-rnd250"))
    db = factory()
    try:
        assert is_initialized(db) is True
    finally:
        db.close()

    assert client.get("/api/admin/settings/bootstrap-status").json() == {"initialized": True}
    response = client.post(
        "/api/admin/settings/bootstrap",
        json={"admin_username": "attacker", "admin_password": "attacker-password"},
    )
    assert response.status_code == 403
    assert _stored(factory, "admin_username") is None


def test_bootstrap_password_credentials_complete_the_login_flow(
    bootstrap_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = bootstrap_client

    bootstrap = client.post(
        "/api/admin/settings/bootstrap",
        json={"admin_username": "bootstrap-admin", "admin_password": "bootstrap-password"},
    )

    assert bootstrap.status_code == 200
    assert bootstrap.json() == {"ok": True}
    username = _stored(factory, "admin_username")
    password_hash = _stored(factory, "admin_password_hash")
    assert username is not None and username.value == "bootstrap-admin"
    assert password_hash is not None and password_hash.value != "bootstrap-password"
    assert password_hash.is_secret is False

    login = client.post(
        "/api/auth/password/login",
        json={"username": "bootstrap-admin", "password": "bootstrap-password"},
    )
    assert login.status_code == 200
    assert login.json() == {"logged_in": True}
    assert "session_id" in login.headers["set-cookie"]


def test_password_login_falls_back_to_existing_environment_credentials(
    bootstrap_client: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory = bootstrap_client
    monkeypatch.setenv("ADMIN_USERNAME", "env-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("env-password-rnd250"))

    response = client.post(
        "/api/auth/password/login",
        json={"username": "env-admin", "password": "env-password-rnd250"},
    )

    assert response.status_code == 200
    assert response.json() == {"logged_in": True}
    assert _stored(factory, "admin_username") is None
    assert _stored(factory, "admin_password_hash") is None


def test_wecom_bootstrap_uses_registered_validation_and_resolver(
    bootstrap_client: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory = bootstrap_client
    monkeypatch.setenv("AUTH_MODE", "wecom")

    response = client.post(
        "/api/admin/settings/bootstrap",
        json={
            "wecom_corp_id": "corp-rnd250",
            "wecom_agent_id": "1000001",
            "wecom_oauth_secret": "oauth-secret-rnd250",
        },
    )

    assert response.status_code == 200
    db = factory()
    try:
        assert is_initialized(db) is True
    finally:
        db.close()
    assert _stored(factory, "wecom_oauth_secret").is_secret is True


def test_init_page_renders_only_until_initialization_is_complete(
    bootstrap_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = bootstrap_client

    initial = client.get("/admin/settings/init", follow_redirects=False)
    assert initial.status_code == 200
    assert 'id="bootstrap-form"' in initial.text

    assert client.post(
        "/api/admin/settings/bootstrap",
        json={"admin_username": "init-admin", "admin_password": "init-password"},
    ).status_code == 200
    complete = client.get("/admin/settings/init", follow_redirects=False)
    assert complete.status_code == 302
    assert complete.headers["location"] == "/admin/login"


def test_credentials_are_not_returned_by_settings_api(
    bootstrap_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = bootstrap_client
    assert client.post(
        "/api/admin/settings/bootstrap",
        json={"admin_username": "settings-admin", "admin_password": "settings-password"},
    ).status_code == 200

    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: (SimpleNamespace(id="admin", role="admin"), "tenant-default")
    try:
        response = client.get("/api/admin/settings")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    keys = {
        item["key"]
        for group in response.json()["groups"].values()
        for item in group
    }
    assert "admin_username" not in keys
    assert "admin_password_hash" not in keys


def _add_real_user(
    factory: sessionmaker,
    *,
    email: str,
    password: str,
) -> str:
    """Insert an active per-user account with a password hash; return its id."""
    db = factory()
    try:
        user = AdminUser(
            id=str(uuid.uuid4()),
            tenant_id="tenant-default",
            wecom_user_id=f"real-{uuid.uuid4()}",
            name="Real user",
            email=email,
            password_hash=hash_password(password),
            role="compliance",
            status="active",
            last_login_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def test_is_initialized_true_with_real_user_without_env_bootstrap(
    bootstrap_client: tuple[TestClient, sessionmaker],
) -> None:
    """RND-386: after env bootstrap retirement, a real per-user account keeps
    the deployment initialized so bootstrap cannot be re-opened."""
    client, factory = bootstrap_client
    user_id = _add_real_user(factory, email="real@example.com", password="real-secret")
    db = factory()
    try:
        assert is_initialized(db) is True
    finally:
        db.close()

    # Bootstrap must stay locked — re-opening it is a privilege escalation.
    response = client.post(
        "/api/admin/settings/bootstrap",
        json={"admin_username": "attacker", "admin_password": "attacker-password"},
    )
    assert response.status_code == 403

    db = factory()
    try:
        db.query(AdminUser).filter(AdminUser.id == user_id).delete()
        db.commit()
    finally:
        db.close()


def test_password_login_with_real_user_without_env_bootstrap(
    bootstrap_client: tuple[TestClient, sessionmaker],
) -> None:
    """RND-386: password login succeeds for a real per-user account when the
    env bootstrap credential pair has been removed."""
    client, factory = bootstrap_client
    user_id = _add_real_user(factory, email="real@example.com", password="real-secret")

    response = client.post(
        "/api/auth/password/login",
        json={"username": "real@example.com", "password": "real-secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"logged_in": True}
    assert "session_id" in response.headers["set-cookie"]

    db = factory()
    try:
        db.query(AdminSession).filter(AdminSession.admin_user_id == user_id).delete()
        db.query(AdminUser).filter(AdminUser.id == user_id).delete()
        db.commit()
    finally:
        db.close()


def test_password_login_without_env_bootstrap_rejects_unknown_credentials(
    bootstrap_client: tuple[TestClient, sessionmaker],
) -> None:
    """RND-386: without env bootstrap and without a matching per-user account,
    login fails closed with 401 (no account-enumeration oracle)."""
    client, _factory = bootstrap_client

    response = client.post(
        "/api/auth/password/login",
        json={"username": "admin", "password": "123456"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials"}

