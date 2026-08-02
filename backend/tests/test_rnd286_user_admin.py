"""Offline API coverage for RND-286 admin user lifecycle endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user, hash_password
from app.db.base import Base
from app.db.models import AdminSession, AdminUser, PasswordResetToken, Tenant
from app.db.session import get_db


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            PasswordResetToken.__table__,
        ],
    )
    session = Session(engine)
    for tenant_id, slug in (("tenant-a", "default"), ("tenant-b", "tenant-b")):
        session.add(Tenant(id=tenant_id, slug=slug, name=tenant_id, is_active=True))
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def admin(db: Session) -> AdminUser:
    user = AdminUser(
        id="admin-a",
        tenant_id="tenant-a",
        wecom_user_id="admin-a",
        name="Admin A",
        email="admin-a@example.test",
        role="admin",
        status="active",
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def client(db: Session, admin: AdminUser):
    from app.main import app

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: (admin, admin.tenant_id)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _add_user(
    db: Session,
    *,
    user_id: str | None = None,
    tenant_id: str = "tenant-a",
    email: str | None = "user@example.test",
    role: str = "admin",
    status: str = "active",
    password: str | None = None,
) -> AdminUser:
    user = AdminUser(
        id=user_id or str(uuid.uuid4()),
        tenant_id=tenant_id,
        wecom_user_id=f"wecom-{uuid.uuid4()}",
        name="Target User",
        email=email,
        password_hash=hash_password(password) if password else None,
        role=role,
        status=status,
    )
    db.add(user)
    db.commit()
    return user


def test_patch_status_toggles_user_and_disabled_user_cannot_password_login(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _add_user(db, email="target@example.test", password="correct-password")

    disabled = client.patch(f"/api/admin/users/{target.id}", json={"status": "disabled"})
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("ADMIN_USERNAME", "bootstrap-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("bootstrap-password"))
    rejected_login = client.post(
        "/api/auth/password/login",
        json={"username": target.email, "password": "correct-password"},
    )
    assert rejected_login.status_code == 401

    enabled = client.patch(f"/api/admin/users/{target.id}", json={"status": "active"})
    assert enabled.status_code == 200
    assert enabled.json() == {
        "id": target.id,
        "email": target.email,
        "name": target.name,
        "role": "admin",
        "status": "active",
    }


def test_status_transition_audits_actor_target_and_skips_noop(
    client: TestClient, db: Session, admin: AdminUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock
    import app.routers.users as users_module

    target = _add_user(db)
    audit_writer = MagicMock()
    monkeypatch.setattr(users_module, "write_audit", audit_writer)

    assert client.patch(f"/api/admin/users/{target.id}", json={"status": "disabled"}).status_code == 200
    call = audit_writer.call_args.kwargs
    assert call["action"] == "user.disabled"
    assert call["tenant_id"] == admin.tenant_id
    assert call["admin_user_id"] == admin.id
    assert call["object_id"] == target.id

    audit_writer.reset_mock()
    assert client.patch(f"/api/admin/users/{target.id}", json={"status": "disabled"}).status_code == 200
    audit_writer.assert_not_called()

    assert client.patch(f"/api/admin/users/{target.id}", json={"status": "active"}).status_code == 200
    assert audit_writer.call_args.kwargs["action"] == "user.enabled"


def test_patch_rejects_invalid_status_cross_tenant_missing_and_self_disable(
    client: TestClient, db: Session, admin: AdminUser
) -> None:
    other_tenant = _add_user(db, tenant_id="tenant-b")

    invalid = client.patch(f"/api/admin/users/{admin.id}", json={"status": "foo"})
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "invalid_status"}

    cross_tenant = client.patch(
        f"/api/admin/users/{other_tenant.id}", json={"status": "disabled"}
    )
    missing = client.patch("/api/admin/users/missing", json={"status": "disabled"})
    assert cross_tenant.status_code == missing.status_code == 404
    assert cross_tenant.json() == missing.json() == {"detail": "User not found"}

    self_disable = client.patch(f"/api/admin/users/{admin.id}", json={"status": "disabled"})
    self_enable = client.patch(f"/api/admin/users/{admin.id}", json={"status": "active"})
    assert self_disable.status_code == 400
    assert self_disable.json() == {"detail": "cannot_disable_self"}
    assert self_enable.status_code == 200


def test_reset_password_creates_token_and_sends_email(client: TestClient, db: Session) -> None:
    target = _add_user(db, email="reset@example.test")

    with patch("app.routers.users.send_password_reset_email", return_value=True) as send_email:
        response = client.post(f"/api/admin/users/{target.id}/reset-password")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    token = db.query(PasswordResetToken).filter_by(admin_user_id=target.id).one()
    assert token.used is False
    assert token.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    send_email.assert_called_once()
    assert target.email == send_email.call_args.args[0]
    assert "token=" in send_email.call_args.args[1]
    assert token.token not in response.text


def test_reset_password_rejects_ineligible_or_cross_tenant_user(
    client: TestClient, db: Session
) -> None:
    disabled = _add_user(db, status="disabled")
    no_email = _add_user(db, email=None)
    other_tenant = _add_user(db, tenant_id="tenant-b")

    disabled_response = client.post(f"/api/admin/users/{disabled.id}/reset-password")
    no_email_response = client.post(f"/api/admin/users/{no_email.id}/reset-password")
    cross_tenant_response = client.post(
        f"/api/admin/users/{other_tenant.id}/reset-password"
    )

    assert disabled_response.status_code == 409
    assert disabled_response.json() == {"detail": "user_not_active"}
    assert no_email_response.status_code == 400
    assert no_email_response.json() == {"detail": "user_has_no_email"}
    assert cross_tenant_response.status_code == 404
    assert cross_tenant_response.json() == {"detail": "User not found"}


@pytest.mark.parametrize("role", ["readonlyaudit", "compliance", "legal"])
def test_lifecycle_routes_require_admin_or_owner(
    client: TestClient, admin: AdminUser, db: Session, role: str
) -> None:
    target = _add_user(db)
    admin.role = role
    db.commit()

    for method, path in (
        ("patch", f"/api/admin/users/{target.id}"),
        ("post", f"/api/admin/users/{target.id}/reset-password"),
    ):
        response = getattr(client, method)(path, json={"status": "active"} if method == "patch" else None)
        assert response.status_code == 403
        assert response.json() == {"detail": "Insufficient role for this operation"}


def test_lifecycle_routes_require_authentication(db: Session, admin: AdminUser) -> None:
    from app.main import app

    target = _add_user(db)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            for method, path in (
                ("patch", f"/api/admin/users/{target.id}"),
                ("post", f"/api/admin/users/{target.id}/reset-password"),
            ):
                response = getattr(test_client, method)(
                    path, json={"status": "active"} if method == "patch" else None
                )
                assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()
