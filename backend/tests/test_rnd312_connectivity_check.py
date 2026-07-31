"""Offline acceptance coverage for RND-312 tenant connectivity checks."""

from __future__ import annotations

import base64
from collections.abc import Generator
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth import hash_password
from app.db.base import Base
from app.db.models import PlatformAdmin, Tenant, TenantWecomConfig
from app.db.session import get_db

_APP_SECRET = "rnd312-test-credential-not-for-response"


def _basic(password: str = "test-password") -> dict[str, str]:
    token = base64.b64encode(f"platform@example.test:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def connectivity_client(
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
        tables=[Tenant.__table__, TenantWecomConfig.__table__, PlatformAdmin.__table__],
    )
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    db.add(
        PlatformAdmin(
            id="platform-admin-rnd312",
            email="platform@example.test",
            password_hash=hash_password("test-password"),
            role="superadmin",
            status="active",
        )
    )
    tenant = Tenant(id="tenant-rnd312", name="RND-312", slug="rnd-312")
    config = TenantWecomConfig(
        id="config-rnd312",
        tenant_id=tenant.id,
        corp_id="ww-rnd312",
        agent_id="1000312",
        callback_domain="",
        is_active=True,
    )
    config.set_app_secret(_APP_SECRET)
    db.add_all([tenant, config])
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


def test_connectivity_check_returns_ok_for_usable_credentials(
    connectivity_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _db = connectivity_client
    get_token = Mock(return_value="mock-access-token")
    monkeypatch.setattr("app.routers.platform.get_wecom_token", get_token)

    response = client.post(
        "/api/platform/tenants/tenant-rnd312/connectivity-check", headers=_basic()
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    get_token.assert_called_once_with(
        "ww-rnd312",
        _APP_SECRET,
        cache_key="connectivity-check:tenant-rnd312",
    )
    assert _APP_SECRET not in response.text


def test_connectivity_check_returns_safe_failure_reason_without_secret_leakage(
    connectivity_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _db = connectivity_client
    monkeypatch.setattr(
        "app.routers.platform.get_wecom_token",
        Mock(
            side_effect=RuntimeError(
                f"WeCom gettoken failed: errcode=40001; raw diagnostic={_APP_SECRET}"
            )
        ),
    )

    response = client.post(
        "/api/platform/tenants/tenant-rnd312/connectivity-check", headers=_basic()
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "invalid_credentials"}
    assert _APP_SECRET not in response.text
    assert "raw diagnostic" not in response.text
    assert "app_secret" not in response.text
    assert "private_key_encrypted" not in response.text


def test_connectivity_check_returns_404_without_tenant_config(
    connectivity_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _db = connectivity_client
    get_token = Mock()
    monkeypatch.setattr("app.routers.platform.get_wecom_token", get_token)

    response = client.post(
        "/api/platform/tenants/missing-tenant/connectivity-check", headers=_basic()
    )

    assert response.status_code == 404
    get_token.assert_not_called()


@pytest.mark.parametrize("headers", [{}, _basic(password="incorrect")])
def test_connectivity_check_requires_valid_platform_admin_credentials(
    connectivity_client: tuple[TestClient, Session], headers: dict[str, str]
) -> None:
    client, _db = connectivity_client

    response = client.post(
        "/api/platform/tenants/tenant-rnd312/connectivity-check", headers=headers
    )

    assert response.status_code == 401
