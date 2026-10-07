"""Integration coverage for RND-276 per-user password authentication.

The database-backed cases run only when ``DATABASE_URL`` is explicitly set,
following the repository's live-schema test convention.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import PASSWORD_MODE_WECOM_PREFIX, hash_password
from app.db.models import AdminSession, AdminUser, Tenant


_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def db() -> Session:
    engine = create_engine(os.environ["DATABASE_URL"])
    session = Session(engine)
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("ADMIN_USERNAME", "rnd276-env-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("rnd276-env-password"))


@pytest.fixture()
def default_tenant(db: Session) -> Tenant:
    tenant = (
        db.query(Tenant)
        .filter(Tenant.slug == "default", Tenant.lifecycle_status == "active")
        .first()
    )
    if tenant is None:
        pytest.skip("active default tenant is required for password-login integration tests")
    return tenant


def _add_user(
    db: Session,
    tenant: Tenant,
    *,
    email: str,
    password: str = "secret",
    role: str = "compliance",
    status: str = "active",
) -> AdminUser:
    user = AdminUser(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        wecom_user_id=f"rnd276-{uuid.uuid4()}",
        name="RND-276 test user",
        email=email,
        password_hash=hash_password(password),
        role=role,
        status=status,
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    return user


def _remove_user(db: Session, user: AdminUser) -> None:
    db.query(AdminSession).filter(AdminSession.admin_user_id == user.id).delete()
    db.query(AdminUser).filter(AdminUser.id == user.id).delete()
    db.commit()


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_per_user_email_login_succeeds_case_insensitively(
    client, db, default_tenant, password_env
) -> None:
    user = _add_user(db, default_tenant, email=f"alice-{uuid.uuid4()}@example.com")
    try:
        response = client.post(
            "/api/auth/password/login",
            json={"username": f"  {user.email.upper()}  ", "password": "secret"},
        )
        assert response.status_code == 200
        assert response.json() == {"logged_in": True}
        assert "session_id" in response.headers["set-cookie"]
    finally:
        _remove_user(db, user)


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_wrong_password_and_unknown_email_have_identical_responses(
    client, db, default_tenant, password_env
) -> None:
    user = _add_user(db, default_tenant, email=f"alice-{uuid.uuid4()}@example.com")
    try:
        wrong_password = client.post(
            "/api/auth/password/login",
            json={"username": user.email, "password": "wrong-password"},
        )
        unknown_email = client.post(
            "/api/auth/password/login",
            json={"username": f"unknown-{uuid.uuid4()}@example.com", "password": "secret"},
        )
        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.content == unknown_email.content
        assert wrong_password.json() == {"detail": "Invalid credentials"}
    finally:
        _remove_user(db, user)


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_auth_me_returns_per_user_role_without_password_data(
    client, db, default_tenant, password_env
) -> None:
    user = _add_user(db, default_tenant, email=f"alice-{uuid.uuid4()}@example.com")
    try:
        login = client.post(
            "/api/auth/password/login",
            json={"username": user.email, "password": "secret"},
        )
        assert login.status_code == 200

        response = client.get("/api/auth/me")
        assert response.status_code == 200
        assert response.json()["role"] == "compliance"
        assert "pbkdf2" not in response.text
        assert PASSWORD_MODE_WECOM_PREFIX not in response.text
    finally:
        _remove_user(db, user)


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_disabled_per_user_account_is_rejected(
    client, db, default_tenant, password_env
) -> None:
    user = _add_user(
        db,
        default_tenant,
        email=f"disabled-{uuid.uuid4()}@example.com",
        status="disabled",
    )
    try:
        response = client.post(
            "/api/auth/password/login",
            json={"username": user.email, "password": "secret"},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid credentials"}
    finally:
        _remove_user(db, user)


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_env_bootstrap_account_remains_usable(
    client, db, default_tenant, password_env
) -> None:
    username = f"rnd276-bootstrap-{uuid.uuid4()}"
    password = "bootstrap-secret"
    sentinel = f"{PASSWORD_MODE_WECOM_PREFIX}{username}__"
    try:
        with patch.dict(
            os.environ,
            {
                "AUTH_MODE": "password",
                "ADMIN_USERNAME": username,
                "ADMIN_PASSWORD_HASH": hash_password(password),
            },
        ):
            response = client.post(
                "/api/auth/password/login",
                json={"username": username, "password": password},
            )
        assert response.status_code == 200
        assert response.json() == {"logged_in": True}
    finally:
        db.query(AdminSession).filter(AdminSession.wecom_user_id == sentinel).delete()
        db.query(AdminUser).filter(
            AdminUser.tenant_id == default_tenant.id,
            AdminUser.wecom_user_id == sentinel,
        ).delete()
        db.commit()


def test_failed_login_always_runs_env_password_verification() -> None:
    """Unknown-email failures still do one PBKDF2 verification for timing parity."""
    from app.db.session import get_db
    from app.main import app
    from fastapi.testclient import TestClient

    tenant = MagicMock(id="default-tenant")
    mock_db = MagicMock()

    def query(model):
        query_mock = MagicMock()
        query_mock.filter.return_value = query_mock
        query_mock.first.return_value = tenant if model is Tenant else None
        return query_mock

    mock_db.query.side_effect = query

    def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client, patch.dict(
            os.environ,
            {
                "AUTH_MODE": "password",
                "ADMIN_USERNAME": "admin",
                "ADMIN_PASSWORD_HASH": hash_password("secret"),
            },
        ), patch("app.routers.auth.verify_password", return_value=False) as verify:
            response = test_client.post(
                "/api/auth/password/login",
                json={"username": "unknown@example.com", "password": "secret"},
            )
        assert response.status_code == 401
        assert verify.call_count == 1
    finally:
        app.dependency_overrides.clear()
