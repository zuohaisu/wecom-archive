"""Acceptance coverage for RND-256 read-only .env settings export."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user
from app.config import repository, resolver
from app.config.crypto import encrypt_value
from app.config.schema import CONFIG_REGISTRY
from app.db.base import Base
from app.db.models import AppConfigStore
from app.db.session import get_db


@pytest.fixture()
def settings_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, Session], None, None]:
    """Authenticated client backed only by the deployment config store."""
    from app.main import app

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[AppConfigStore.__table__])
    db = sessionmaker(bind=engine)()

    def override_db() -> Generator[Session, None, None]:
        yield db

    resolver.invalidate()
    app.dependency_overrides[get_current_user] = lambda: (
        SimpleNamespace(id="rnd256-admin", role="admin"),
        "tenant-rnd256",
    )
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db
    app.dependency_overrides.clear()
    resolver.invalidate()
    db.close()
    engine.dispose()


def test_export_covers_registry_with_resolved_values_and_secret_placeholders(
    settings_client: tuple[TestClient, Session],
) -> None:
    client, db = settings_client
    plain_secret = "rnd256-secret-never-exported"
    repository.upsert(
        db,
        "smtp_host",
        "smtp.rnd256.test",
        is_secret=False,
        requires_restart=False,
        updated_by="rnd256",
    )
    repository.upsert(
        db,
        "smtp_password",
        encrypt_value(plain_secret),
        is_secret=True,
        requires_restart=False,
        updated_by="rnd256",
    )
    db.commit()
    resolver.invalidate()

    response = client.get("/api/admin/settings/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    exported = dict(line.split("=", 1) for line in response.text.splitlines())
    assert set(exported) == {key.upper() for key in CONFIG_REGISTRY}
    assert exported["SMTP_HOST"] == "smtp.rnd256.test"
    for key, spec in CONFIG_REGISTRY.items():
        if spec.is_secret:
            assert exported[key.upper()] == "***"
    assert plain_secret not in response.text


def test_settings_page_copies_exported_env_text_to_clipboard() -> None:
    script = (Path(__file__).parent.parent / "app" / "web" / "static" / "settings.js").read_text()

    assert "data-settings-export" in script
    assert "fetch('/api/admin/settings/export', {credentials: 'include'})" in script
    assert "navigator.clipboard.writeText(text)" in script


def test_export_implementation_does_not_parse_or_write_env_files() -> None:
    contents = (Path(__file__).parent.parent / "app" / "routers" / "settings.py").read_text()

    assert "parse_env" not in contents
    assert "write_text" not in contents
