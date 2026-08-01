"""Offline acceptance coverage for RND-252 saved-configuration checks."""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user
from app.config import repository, resolver, validation
from app.config.crypto import encrypt_value
from app.db.base import Base
from app.db.models import AppConfigStore
from app.db.session import get_db

_SECRET = "rnd252-secret-never-in-response"


@pytest.fixture()
def settings_client(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, Session], None, None]:
    """Authenticated client backed by the saved configuration table only."""
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

    resolver.invalidate()
    app.dependency_overrides[get_current_user] = lambda: (
        SimpleNamespace(id="rnd252-admin", role="admin"),
        "tenant-rnd252",
    )
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db
    app.dependency_overrides.clear()
    resolver.invalidate()
    db.close()
    engine.dispose()


def _save(db: Session, key: str, value: str, *, secret: bool = False) -> None:
    repository.upsert(
        db,
        key,
        encrypt_value(value) if secret else value,
        is_secret=secret,
        requires_restart=False,
        updated_by="rnd252",
    )
    db.commit()
    resolver.invalidate(key)


def test_check_qiniu_uses_provider_bucket_manager_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = Mock()
    info.ok.return_value = True
    provider = Mock()
    provider._bucket_manager.list.return_value = ({"items": []}, info, None)
    storage_provider = Mock(return_value=provider)
    monkeypatch.setattr(validation, "QiniuStorageProvider", storage_provider)

    assert validation.check_qiniu("access", _SECRET, "bucket", "https://cdn.example.test") == (True, None)
    storage_provider.assert_called_once_with(
        "access", _SECRET, "bucket", "https://cdn.example.test", region=None
    )
    provider._bucket_manager.list.assert_called_once_with("bucket", limit=1)


def test_check_qiniu_returns_fixed_reason_for_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validation,
        "QiniuStorageProvider",
        Mock(side_effect=RuntimeError(f"SDK diagnostic: {_SECRET}")),
    )

    ok, reason = validation.check_qiniu("access", _SECRET, "bucket", "https://cdn.example.test")

    assert (ok, reason) == (False, "qiniu_connection_failed")
    assert _SECRET not in (reason or "")


def test_check_wecom_uses_isolated_cache_key_and_returns_fixed_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_token = Mock(return_value="mock-token")
    monkeypatch.setattr(validation, "get_wecom_token", get_token)

    assert validation.check_wecom("corp-rnd252", _SECRET) == (True, None)
    get_token.assert_called_once_with(
        "corp-rnd252", _SECRET, cache_key="config-self-check:wecom"
    )

    monkeypatch.setattr(
        validation,
        "get_wecom_token",
        Mock(side_effect=RuntimeError(f"provider diagnostic: {_SECRET}")),
    )
    ok, reason = validation.check_wecom("corp-rnd252", _SECRET)
    assert (ok, reason) == (False, "wecom_connection_failed")
    assert _SECRET not in (reason or "")


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("https://admin.example.test", (True, None)),
        ("http://admin.example.test", (False, "invalid_domain_format")),
        ("https://not a domain", (False, "invalid_domain_format")),
        ("https://admin.example.test/path", (False, "invalid_domain_format")),
    ],
)
def test_check_domain_format_is_local_validation_only(
    domain: str, expected: tuple[bool, str | None]
) -> None:
    assert validation.check_domain_format(domain) == expected


@pytest.mark.parametrize("target", ["qiniu", "wecom", "domain"])
def test_endpoint_checks_only_saved_values_for_each_target(
    settings_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    client, db = settings_client
    from app.routers import settings

    _save(db, "qiniu_access_key", "saved-access", secret=True)
    _save(db, "qiniu_secret_key", _SECRET, secret=True)
    _save(db, "qiniu_bucket", "saved-bucket")
    _save(db, "qiniu_domain", "https://cdn.example.test")
    _save(db, "qiniu_region", "z0")
    _save(db, "wecom_corp_id", "saved-corp")
    _save(db, "wecom_oauth_secret", _SECRET, secret=True)
    _save(db, "admin_domain", "https://admin.example.test")

    qiniu_check = Mock(return_value=(True, None))
    wecom_check = Mock(return_value=(True, None))
    domain_check = Mock(return_value=(True, None))
    monkeypatch.setattr(settings, "check_qiniu", qiniu_check)
    monkeypatch.setattr(settings, "check_wecom", wecom_check)
    monkeypatch.setattr(settings, "check_domain_format", domain_check)

    response = client.post("/api/admin/settings/test-connection", json={"target": target})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "reason": None}
    assert _SECRET not in response.text
    if target == "qiniu":
        qiniu_check.assert_called_once_with(
            "saved-access",
            _SECRET,
            "saved-bucket",
            "https://cdn.example.test",
            region="z0",
        )
    elif target == "wecom":
        wecom_check.assert_called_once_with("saved-corp", _SECRET)
    else:
        domain_check.assert_called_once_with("https://admin.example.test")


def test_endpoint_rejects_temporary_credential_fields(
    settings_client: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _db = settings_client
    from app.routers import settings

    wecom_check = Mock()
    monkeypatch.setattr(settings, "check_wecom", wecom_check)

    response = client.post(
        "/api/admin/settings/test-connection",
        json={"target": "wecom", "oauth_secret": _SECRET},
    )

    assert response.status_code == 422
    assert _SECRET not in response.text
    wecom_check.assert_not_called()
