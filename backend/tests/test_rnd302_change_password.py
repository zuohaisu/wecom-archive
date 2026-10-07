"""Focused offline coverage for RND-302 change password."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE, hash_password, verify_password
from app.db.base import Base
from app.db.models import AdminSession, AdminUser, Tenant
from app.db.session import get_db


def test_change_password_module_imports_without_database() -> None:
    from app.routers.settings import _ChangePasswordBody, change_password

    assert callable(change_password)
    assert _ChangePasswordBody(old_password="old-password", new_password="new-password")


@pytest.fixture()
def password_client() -> Generator[tuple[TestClient, Session, AdminUser], None, None]:
    from app.main import app

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, AdminUser.__table__, AdminSession.__table__],
    )
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    tenant = Tenant(id="tenant-rnd302", slug="rnd302", name="RND-302")
    user = AdminUser(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        wecom_user_id="rnd302-user",
        password_hash=hash_password("old-pass-123"),
        role="readonlyaudit",
        status="active",
    )
    auth_session = AdminSession(
        id="session-rnd302",
        admin_user_id=user.id,
        tenant_id=tenant.id,
        wecom_user_id=user.wecom_user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        is_revoked=False,
    )
    db.add_all([tenant, user, auth_session])
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        request_db = session_factory()
        try:
            yield request_db
        finally:
            request_db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        client.cookies.set(SESSION_COOKIE, auth_session.id)
        yield client, db, user
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def test_change_password_updates_only_current_user(
    password_client: tuple[TestClient, Session, AdminUser],
) -> None:
    client, db, user = password_client

    response = client.post(
        "/api/admin/settings/password",
        json={"old_password": "old-pass-123", "new_password": "new-pass-123"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    db.expire_all()
    updated_user = db.get(AdminUser, user.id)
    assert updated_user is not None
    assert verify_password("new-pass-123", updated_user.password_hash)
    assert not verify_password("old-pass-123", updated_user.password_hash)
    assert "pbkdf2" not in response.text


@pytest.mark.parametrize(
    ("old_password", "new_password", "status_code", "detail"),
    [
        ("wrong-password", "new-pass-123", 401, "invalid_old_password"),
        ("old-pass-123", "short", 400, "weak_password"),
        ("old-pass-123", "old-pass-123", 400, "same_as_old"),
    ],
)
def test_change_password_rejects_invalid_input(
    password_client: tuple[TestClient, Session, AdminUser],
    old_password: str,
    new_password: str,
    status_code: int,
    detail: str,
) -> None:
    client, _db, _user = password_client

    response = client.post(
        "/api/admin/settings/password",
        json={"old_password": old_password, "new_password": new_password},
    )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}


def test_change_password_rejects_user_without_password(
    password_client: tuple[TestClient, Session, AdminUser],
) -> None:
    client, db, user = password_client
    user.password_hash = None
    db.commit()

    response = client.post(
        "/api/admin/settings/password",
        json={"old_password": "old-pass-123", "new_password": "new-pass-123"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "no_password_set"}


def test_settings_page_requires_a_valid_session_and_renders_the_form(
    password_client: tuple[TestClient, Session, AdminUser],
) -> None:
    client, _db, _user = password_client

    authenticated = client.get("/admin/settings", follow_redirects=False)
    assert authenticated.status_code == 200
    assert 'id="change-password-form"' in authenticated.text
    assert "/web/static/design-system.css" in authenticated.text
    assert "/web/static/styles.css" not in authenticated.text

    client.cookies.clear()
    unauthenticated = client.get("/admin/settings", follow_redirects=False)
    assert unauthenticated.status_code == 302
    assert unauthenticated.headers["location"] == "/admin/login"
