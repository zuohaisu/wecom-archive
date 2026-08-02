"""Acceptance coverage for RND-249 Settings GET/PUT endpoints."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Generator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user
from app.config import repository, resolver
from app.config.crypto import encrypt_value, is_encrypted
from app.config.schema import CONFIG_REGISTRY
from app.db.base import Base
from app.db.models import AppConfigStore
from app.db.session import get_db


@pytest.fixture()
def settings_client(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, Session], None, None]:
    """Authenticated client backed by only the deployment config store."""
    from app.main import app

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[AppConfigStore.__table__])
    factory = sessionmaker(bind=engine)
    db = factory()

    def override_db() -> Generator[Session, None, None]:
        yield db

    user = SimpleNamespace(id="rnd249-user", role="admin")
    resolver.invalidate()
    app.dependency_overrides[get_current_user] = lambda: (user, "tenant-rnd249")
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db
    app.dependency_overrides.clear()
    resolver.invalidate()
    db.close()
    engine.dispose()


def _fields(response_json: dict) -> dict[str, dict]:
    return {
        item["key"]
        : item
        for group in response_json["groups"].values()
        for item in group
    }


def test_get_returns_every_registry_field_grouped_and_masks_secrets(
    settings_client: tuple[TestClient, Session],
) -> None:
    client, db = settings_client
    plain_secret = "rnd249-test-secret-never-returned"
    repository.upsert(
        db,
        "smtp_password",
        encrypt_value(plain_secret),
        is_secret=True,
        requires_restart=False,
        updated_by="seed",
    )
    db.commit()
    resolver.invalidate()

    response = client.get("/api/admin/settings")

    assert response.status_code == 200
    fields = _fields(response.json())
    assert set(fields) == set(CONFIG_REGISTRY)
    assert fields["smtp_password"]["value"] == "****rned"
    assert fields["smtp_password"]["source"] == "db"
    assert plain_secret not in json.dumps(response.json())
    for key, spec in CONFIG_REGISTRY.items():
        assert fields[key]["requires_restart"] is spec.restart_required
        assert key in {item["key"] for item in response.json()["groups"][spec.group.value]}


def test_get_reports_default_env_and_database_sources(
    settings_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = settings_client
    monkeypatch.delenv("MEDIA_STORAGE_PROVIDER", raising=False)
    monkeypatch.setenv("SMTP_HOST", "env.smtp.rnd249.test")
    resolver.invalidate()

    first = _fields(client.get("/api/admin/settings").json())
    assert first["media_storage_provider"]["source"] == "default"
    assert first["smtp_host"]["source"] == "env"

    repository.upsert(
        db,
        "smtp_host",
        "db.smtp.rnd249.test",
        is_secret=False,
        requires_restart=False,
        updated_by="seed",
    )
    db.commit()
    resolver.invalidate("smtp_host")

    second = _fields(client.get("/api/admin/settings").json())
    assert second["smtp_host"]["source"] == "db"
    assert second["smtp_host"]["value"] == "db.smtp.rnd249.test"


def test_put_rejects_invalid_batch_without_writing_any_value(
    settings_client: tuple[TestClient, Session],
) -> None:
    client, db = settings_client

    response = client.put(
        "/api/admin/settings",
        json={"updates": {"smtp_host": "valid.smtp.rnd249.test", "smtp_port": "not-an-int"}},
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [
        {"key": "smtp_port", "message": "value must be an integer", "code": "invalid_type"}
    ]
    assert db.get(AppConfigStore, "smtp_host") is None
    assert db.get(AppConfigStore, "smtp_port") is None


def test_put_accepts_numeric_string_and_persists_canonical_integer(
    settings_client: tuple[TestClient, Session],
) -> None:
    client, db = settings_client

    response = client.put("/api/admin/settings", json={"updates": {"smtp_port": "25"}})

    assert response.status_code == 200
    assert db.get(AppConfigStore, "smtp_port").value == "25"


def test_put_reports_required_field_validation(
    settings_client: tuple[TestClient, Session],
) -> None:
    client, _db = settings_client

    response = client.put("/api/admin/settings", json={"updates": {"wecom_corp_id": ""}})

    assert response.status_code == 400
    assert response.json()["errors"] == [
        {"key": "wecom_corp_id", "message": "value is required", "code": "required"}
    ]


def test_put_encrypts_secrets_and_blank_secret_preserves_existing_value(
    settings_client: tuple[TestClient, Session],
) -> None:
    client, db = settings_client
    plain_secret = "rnd249-secret-value"

    saved = client.put("/api/admin/settings", json={"updates": {"smtp_password": plain_secret}})
    assert saved.status_code == 200
    stored = db.get(AppConfigStore, "smtp_password")
    assert stored is not None
    assert stored.value != plain_secret
    assert is_encrypted(stored.value)
    encrypted_value = stored.value

    retained = client.put("/api/admin/settings", json={"updates": {"smtp_password": ""}})
    assert retained.status_code == 200
    db.expire_all()
    assert db.get(AppConfigStore, "smtp_password").value == encrypted_value


def test_secret_setting_activity_detail_recursively_excludes_submitted_value(
    settings_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit identifies a changed key without retaining secret plaintext/ciphertext."""
    from unittest.mock import MagicMock
    import app.routers.settings as settings_router

    client, db = settings_client
    submitted_secret = "rnd335-actual-secret-value"
    audit_writer = MagicMock()
    monkeypatch.setattr(settings_router, "write_audit", audit_writer)

    assert client.put(
        "/api/admin/settings", json={"updates": {"smtp_password": submitted_secret}}
    ).status_code == 200
    stored_ciphertext = db.get(AppConfigStore, "smtp_password").value
    detail = audit_writer.call_args.kwargs["detail"]

    def assert_value_safe(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                assert_value_safe(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                assert_value_safe(item)
        elif isinstance(value, str):
            assert submitted_secret not in value
            assert stored_ciphertext not in value
            assert "****" not in value

    assert detail == {"changed_keys": ["smtp_password"]}
    assert_value_safe(detail)


def test_put_persists_effective_noop_without_security_activity(
    settings_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit config source is persisted even when its value is unchanged."""
    client, db = settings_client
    from unittest.mock import MagicMock
    import app.routers.settings as settings_router

    monkeypatch.setenv("SMTP_HOST", "same.smtp.rnd335.test")
    resolver.invalidate("smtp_host")
    audit_writer = MagicMock()
    monkeypatch.setattr(settings_router, "write_audit", audit_writer)

    response = client.put(
        "/api/admin/settings", json={"updates": {"smtp_host": "same.smtp.rnd335.test"}}
    )

    assert response.status_code == 200
    assert db.get(AppConfigStore, "smtp_host").value == "same.smtp.rnd335.test"
    audit_writer.assert_not_called()


def test_put_blank_nonsecret_clears_value(
    settings_client: tuple[TestClient, Session],
) -> None:
    client, db = settings_client

    assert client.put("/api/admin/settings", json={"updates": {"smtp_host": "smtp.rnd249.test"}}).status_code == 200
    cleared = client.put("/api/admin/settings", json={"updates": {"smtp_host": ""}})

    assert cleared.status_code == 200
    db.expire_all()
    assert db.get(AppConfigStore, "smtp_host").value == ""


def test_put_invalidates_resolver_cache_and_returns_restart_keys(
    settings_client: tuple[TestClient, Session],
) -> None:
    client, db = settings_client
    repository.upsert(
        db,
        "smtp_host",
        "old.smtp.rnd249.test",
        is_secret=False,
        requires_restart=False,
        updated_by="seed",
    )
    db.commit()
    resolver.invalidate()
    assert resolver.resolve(db, "smtp_host") == "old.smtp.rnd249.test"

    response = client.put(
        "/api/admin/settings",
        json={"updates": {"smtp_host": "new.smtp.rnd249.test", "media_storage_provider": "local"}},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "restart_required_keys": ["media_storage_provider"]}
    assert resolver.resolve(db, "smtp_host") == "new.smtp.rnd249.test"
    assert client.put("/api/admin/settings", json={"updates": {"smtp_host": "again.smtp.rnd249.test"}}).json()[
        "restart_required_keys"
    ] == []
