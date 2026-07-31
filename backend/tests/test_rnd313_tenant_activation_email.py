"""Offline acceptance coverage for RND-313 tenant owner activation invitations."""

from __future__ import annotations

import base64
from collections.abc import Generator
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth import hash_password
from app.db.base import Base
from app.db.models import AdminUser, PlatformAdmin, Tenant, TenantWecomConfig
from app.db.session import get_db

_SECRET = "test-secret-rnd313"
_PRIVATE_KEY_PEM = "-----BEGIN PRIVATE KEY-----\ntest-rnd313\n-----END PRIVATE KEY-----"


def _basic() -> dict[str, str]:
    token = base64.b64encode(b"platform@example.test:test-password").decode()
    return {"Authorization": f"Basic {token}"}


def _payload() -> dict[str, str]:
    return {
        "name": "RND-313 Tenant",
        "slug": "rnd-313-tenant",
        "admin_email": "platform@example.com",
        "owner_email": "owner@example.com",
        "corp_id": "ww-rnd313",
        "agent_id": "1000313",
        "secret": _SECRET,
        "private_key_pem": _PRIVATE_KEY_PEM,
    }


@pytest.fixture()
def provision_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, Session], None, None]:
    from app.main import create_app

    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantWecomConfig.__table__,
            AdminUser.__table__,
            PlatformAdmin.__table__,
        ],
    )
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    db.add(
        PlatformAdmin(
            id="platform-admin-rnd313",
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


def test_create_tenant_creates_owner_pending_invite_and_sends_email(
    provision_client: tuple[TestClient, Session],
) -> None:
    client, db = provision_client

    with patch("app.email.send_invite_email") as send_email:
        response = client.post("/api/platform/tenants", json=_payload(), headers=_basic())

    assert response.status_code == 201
    assert response.json()["owner_invite_sent"] is True
    owner = db.query(AdminUser).one()
    assert owner.email == "owner@example.com"
    assert owner.role == "owner"
    assert owner.invite_status == "pending"
    assert owner.invited_by is None
    send_email.assert_called_once()
    assert send_email.call_args.args[0] == "owner@example.com"


def test_invite_email_failure_does_not_rollback_tenant_provisioning(
    provision_client: tuple[TestClient, Session],
) -> None:
    client, db = provision_client

    with patch("app.email.send_invite_email", side_effect=RuntimeError("SMTP unavailable")):
        response = client.post("/api/platform/tenants", json=_payload(), headers=_basic())

    assert response.status_code == 201
    assert response.json()["owner_invite_sent"] is False
    assert db.query(Tenant).count() == 1
    assert db.query(TenantWecomConfig).count() == 1
    assert db.query(AdminUser).count() == 1


def test_create_tenant_requires_owner_email(
    provision_client: tuple[TestClient, Session],
) -> None:
    client, _db = provision_client
    payload = _payload()
    payload.pop("owner_email")

    response = client.post("/api/platform/tenants", json=payload, headers=_basic())

    assert response.status_code == 422
