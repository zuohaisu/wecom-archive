"""Offline acceptance coverage for RND-311 tenant provisioning."""

from __future__ import annotations

import base64
from collections.abc import Generator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth import hash_password
from app.db.base import Base
from app.db.models import PlatformAdmin, Tenant, TenantWecomConfig
from app.db.session import get_db

_SECRET = "test-secret-rnd311"
_PRIVATE_KEY_PEM = "-----BEGIN PRIVATE KEY-----\ntest-rnd311\n-----END PRIVATE KEY-----"


def _basic(email: str = "platform@example.test", password: str = "test-password") -> dict[str, str]:
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _payload(slug: str = "acme", corp_id: str = "ww-rnd311") -> dict[str, str]:
    return {
        "name": "Acme Corporation",
        "slug": slug,
        "admin_email": "first-admin@example.com",
        "corp_id": corp_id,
        "agent_id": "1000001",
        "secret": _SECRET,
        "private_key_pem": _PRIVATE_KEY_PEM,
        "callback_domain": "callback.example.test",
    }


@pytest.fixture()
def provision_client(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, Session], None, None]:
    from app.main import create_app

    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, TenantWecomConfig.__table__, PlatformAdmin.__table__],
    )
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    db.add(
        PlatformAdmin(
            id="platform-admin-rnd311",
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


def _assert_no_secret_keys(value: object) -> None:
    forbidden = ("app_secret", "private_key", "secret", "decrypted")
    if isinstance(value, dict):
        for key, nested in value.items():
            assert not any(term in key.lower() for term in forbidden), key
            _assert_no_secret_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_secret_keys(nested)


def test_create_tenant_encrypts_credentials_at_rest(
    provision_client: tuple[TestClient, Session],
) -> None:
    client, db = provision_client

    response = client.post("/api/platform/tenants", json=_payload(), headers=_basic())

    assert response.status_code == 201
    config = db.query(TenantWecomConfig).one()
    stored_secret, stored_private_key = db.execute(
        text(
            "SELECT app_secret, private_key_encrypted FROM tenant_wecom_configs "
            "WHERE id = :config_id"
        ),
        {"config_id": config.id},
    ).one()
    assert stored_secret != _SECRET
    assert stored_private_key != _PRIVATE_KEY_PEM
    assert config.decrypted_app_secret == _SECRET
    assert config.decrypted_private_key == _PRIVATE_KEY_PEM

    body = response.json()
    assert body["tenant_id"] == config.tenant_id
    assert body["config_id"] == config.id
    assert body["corp_id"] == "ww-rnd311"
    assert body["agent_id"] == "1000001"
    _assert_no_secret_keys(body)


def test_create_tenant_requires_valid_platform_admin_credentials(
    provision_client: tuple[TestClient, Session],
) -> None:
    client, _db = provision_client

    assert client.post("/api/platform/tenants", json=_payload()).status_code == 401
    assert (
        client.post(
            "/api/platform/tenants", json=_payload(), headers=_basic(password="incorrect")
        ).status_code
        == 401
    )


def test_create_tenant_rejects_duplicate_slug_and_active_corp_id(
    provision_client: tuple[TestClient, Session],
) -> None:
    client, _db = provision_client

    assert client.post("/api/platform/tenants", json=_payload(), headers=_basic()).status_code == 201
    assert (
        client.post(
            "/api/platform/tenants", json=_payload(), headers=_basic()).status_code == 409
    )
    assert (
        client.post(
            "/api/platform/tenants",
            json=_payload(slug="other-tenant"),
            headers=_basic(),
        ).status_code
        == 409
    )
