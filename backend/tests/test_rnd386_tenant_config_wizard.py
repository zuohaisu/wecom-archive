"""Offline acceptance coverage for RND-386 tenant-scoped config wizard."""

from __future__ import annotations

import base64
import os
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import (
    AdminSession,
    AdminUser,
    AuditLog,
    Tenant,
    TenantWecomConfig,
    ThirdPartyOrganizationBinding,
)
from app.db.session import get_db


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"

_ARCHIVE_SECRET = "rnd386-archive-secret-never-in-response"
_CALLBACK_TOKEN = "rnd386-callback-token"
_CALLBACK_AES_KEY = base64.b64encode(os.urandom(32)).decode().rstrip("=")
_RSA_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_RSA_PEM = _RSA_PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


@pytest.fixture()
def wizard_client(
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
            ThirdPartyOrganizationBinding.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            AuditLog.__table__,
        ],
    )
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    tenant = Tenant(
        id="tenant-rnd386",
        name="测试企业",
        slug="rnd-386",
        lifecycle_status="provisioning",
    )
    binding = ThirdPartyOrganizationBinding(
        id="binding-rnd386",
        tenant_id=tenant.id,
        corp_id="ww-rnd386-corp",
        agent_id="1000386",
        permanent_code_encrypted="gAAAAA-not-a-real-code",
        authorization_mode="admin",
    )
    owner = AdminUser(
        id="user-rnd386",
        tenant_id=tenant.id,
        wecom_user_id="owner-rnd386",
        role="owner",
        status="active",
    )
    session = AdminSession(
        id="session-rnd386",
        admin_user_id=owner.id,
        tenant_id=tenant.id,
        wecom_user_id=owner.wecom_user_id,
        session_scope="provisioning",
        is_revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
    )
    db.add_all([tenant, binding, owner, session])
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


def _cookies() -> dict[str, str]:
    return {"session_id": "session-rnd386"}


def test_config_get_reports_all_missing_before_first_save(
    wizard_client: tuple[TestClient, Session],
) -> None:
    client, _db = wizard_client
    response = client.get("/api/provisioning/config", cookies=_cookies())
    assert response.status_code == 200
    data = response.json()
    assert data["org"]["corp_id"] == "ww-rnd386-corp"
    assert data["org"]["agent_id"] == "1000386"
    assert data["org"]["source"] == "third_party_binding"
    assert set(data["missing"]) == {
        "archive_secret",
        "private_key",
        "publickey_version",
        "callback_token",
        "callback_encoding_aes_key",
    }
    for field in data["fields"].values():
        assert field["status"] == "missing"
        assert field["mask"] is None
    assert _ARCHIVE_SECRET not in response.text


def test_config_put_saves_and_get_returns_masks_only(
    wizard_client: tuple[TestClient, Session],
) -> None:
    client, db = wizard_client
    payload = {
        "archive_secret": _ARCHIVE_SECRET,
        "private_key": _RSA_PEM,
        "publickey_version": 1,
        "callback_token": _CALLBACK_TOKEN,
        "callback_encoding_aes_key": _CALLBACK_AES_KEY,
    }
    response = client.put("/api/provisioning/config", json=payload, cookies=_cookies())
    assert response.status_code == 200
    data = response.json()
    assert data["missing"] == []
    assert data["fields"]["archive_secret"]["status"] == "set"
    assert data["fields"]["archive_secret"]["mask"].startswith("****")
    assert data["fields"]["publickey_version"]["value"] == 1
    for secret in (_ARCHIVE_SECRET, _RSA_PEM, _CALLBACK_TOKEN, _CALLBACK_AES_KEY):
        assert secret not in response.text

    config = (
        db.query(TenantWecomConfig)
        .filter(TenantWecomConfig.tenant_id == "tenant-rnd386")
        .first()
    )
    assert config is not None
    assert config.corp_id == "ww-rnd386-corp"
    assert config.agent_id == "1000386"
    assert config.decrypted_app_secret == _ARCHIVE_SECRET
    assert config.decrypted_private_key == _RSA_PEM
    assert config.decrypted_callback_token == _CALLBACK_TOKEN
    assert config.decrypted_callback_encoding_aes_key == _CALLBACK_AES_KEY
    assert config.publickey_version == 1

    audit = db.query(AuditLog).filter(AuditLog.action == "config.changed").first()
    assert audit is not None
    assert audit.tenant_id == "tenant-rnd386"
    assert "archive_secret" in audit.detail["changed_keys"]

    # GET returns the same masks, never plaintext.
    get_response = client.get("/api/provisioning/config", cookies=_cookies())
    assert get_response.status_code == 200
    for secret in (_ARCHIVE_SECRET, _RSA_PEM, _CALLBACK_TOKEN, _CALLBACK_AES_KEY):
        assert secret not in get_response.text


def test_blank_put_never_overwrites_existing_secrets(
    wizard_client: tuple[TestClient, Session],
) -> None:
    client, db = wizard_client
    client.put(
        "/api/provisioning/config",
        json={"archive_secret": _ARCHIVE_SECRET},
        cookies=_cookies(),
    )
    response = client.put(
        "/api/provisioning/config",
        json={"archive_secret": "", "private_key": None, "callback_token": "  "},
        cookies=_cookies(),
    )
    assert response.status_code == 200
    config = (
        db.query(TenantWecomConfig)
        .filter(TenantWecomConfig.tenant_id == "tenant-rnd386")
        .first()
    )
    assert config.decrypted_app_secret == _ARCHIVE_SECRET
    assert config.private_key_encrypted is None
    assert config.callback_token_encrypted is None


def test_put_rejects_client_supplied_identity_keys(
    wizard_client: tuple[TestClient, Session],
) -> None:
    client, _db = wizard_client
    for extra in ({"corp_id": "ww-evil"}, {"tenant_id": "tenant-evil"}, {"is_active": True}):
        response = client.put(
            "/api/provisioning/config",
            json={**extra, "archive_secret": _ARCHIVE_SECRET},
            cookies=_cookies(),
        )
        assert response.status_code == 422


def test_put_validation_failures_return_field_codes(
    wizard_client: tuple[TestClient, Session],
) -> None:
    client, _db = wizard_client
    response = client.put(
        "/api/provisioning/config",
        json={"archive_secret": _ARCHIVE_SECRET, "private_key": "not-a-key"},
        cookies=_cookies(),
    )
    assert response.status_code == 400
    codes = {item["key"]: item["code"] for item in response.json()["errors"]}
    assert codes["private_key"] == "invalid_private_key"

    response = client.put(
        "/api/provisioning/config",
        json={"archive_secret": _ARCHIVE_SECRET, "callback_token": "solo-token"},
        cookies=_cookies(),
    )
    assert response.status_code == 400
    codes = {item["key"]: item["code"] for item in response.json()["errors"]}
    assert codes["callback_encoding_aes_key"] == "callback_pair_required"

    response = client.put(
        "/api/provisioning/config",
        json={
            "archive_secret": _ARCHIVE_SECRET,
            "callback_token": "t",
            "callback_encoding_aes_key": "not-43-chars",
        },
        cookies=_cookies(),
    )
    assert response.status_code == 400
    codes = {item["key"]: item["code"] for item in response.json()["errors"]}
    assert codes["callback_encoding_aes_key"] == "invalid_callback_aes_key"

    response = client.put(
        "/api/provisioning/config",
        json={"archive_secret": _ARCHIVE_SECRET, "publickey_version": 0},
        cookies=_cookies(),
    )
    assert response.status_code == 400
    codes = {item["key"]: item["code"] for item in response.json()["errors"]}
    assert codes["publickey_version"] == "invalid_publickey_version"


def test_first_save_without_secret_is_rejected(
    wizard_client: tuple[TestClient, Session],
) -> None:
    client, db = wizard_client
    response = client.put(
        "/api/provisioning/config",
        json={"private_key": _RSA_PEM},
        cookies=_cookies(),
    )
    assert response.status_code == 400
    codes = {item["key"]: item["code"] for item in response.json()["errors"]}
    assert codes["archive_secret"] == "required_first"
    assert db.query(TenantWecomConfig).count() == 0


def test_config_test_probes_connectivity_and_maps_failures(
    wizard_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _db = wizard_client
    client.put(
        "/api/provisioning/config",
        json={"archive_secret": _ARCHIVE_SECRET},
        cookies=_cookies(),
    )

    get_token = Mock(return_value="mock-access-token")
    monkeypatch.setattr("app.auth.get_wecom_token", get_token)
    response = client.post("/api/provisioning/config/test", cookies=_cookies())
    assert response.status_code == 200
    assert response.json()["fields"]["connectivity"]["ok"] is True

    monkeypatch.setattr(
        "app.auth.get_wecom_token",
        Mock(side_effect=RuntimeError("WeCom gettoken failed: errcode=40001")),
    )
    response = client.post("/api/provisioning/config/test", cookies=_cookies())
    assert response.json()["fields"]["connectivity"] == {
        "ok": False,
        "safe_error_code": "invalid_credentials",
    }

    monkeypatch.setattr(
        "app.auth.get_wecom_token",
        Mock(side_effect=RuntimeError("Failed to fetch WeCom access_token")),
    )
    response = client.post("/api/provisioning/config/test", cookies=_cookies())
    assert response.json()["fields"]["connectivity"] == {
        "ok": False,
        "safe_error_code": "network_error",
    }


def test_config_test_without_row_reports_all_missing(
    wizard_client: tuple[TestClient, Session],
) -> None:
    client, _db = wizard_client
    response = client.post("/api/provisioning/config/test", cookies=_cookies())
    assert response.status_code == 200
    assert response.json()["all_ok"] is False
    assert all(
        field["safe_error_code"] == "missing"
        for key, field in response.json()["fields"].items()
    )


def test_config_routes_require_provisioning_session(
    wizard_client: tuple[TestClient, Session],
) -> None:
    client, _db = wizard_client
    assert client.get("/api/provisioning/config").status_code == 401
    assert (
        client.put(
            "/api/provisioning/config",
            json={"archive_secret": _ARCHIVE_SECRET},
        ).status_code
        == 401
    )
    assert client.post("/api/provisioning/config/test").status_code == 401
    assert (
        client.get("/api/provisioning/config", cookies={"session_id": "bogus"}).status_code
        == 401
    )


def test_admin_scoped_session_is_rejected(
    wizard_client: tuple[TestClient, Session],
) -> None:
    client, db = wizard_client
    admin_session = AdminSession(
        id="session-admin-rnd386",
        admin_user_id="user-rnd386",
        tenant_id="tenant-rnd386",
        wecom_user_id="owner-rnd386",
        session_scope="admin",
        is_revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
    )
    db.add(admin_session)
    db.commit()
    response = client.get(
        "/api/provisioning/config", cookies={"session_id": "session-admin-rnd386"}
    )
    assert response.status_code == 401
