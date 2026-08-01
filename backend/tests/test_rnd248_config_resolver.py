"""Acceptance coverage for RND-248 configuration repository and resolver."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import repository, resolver
from app.config.crypto import encrypt_value
from app.config.schema import CONFIG_REGISTRY
from app.db.base import Base
from app.db.models import AppConfigStore


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AppConfigStore.__table__])
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def clear_resolver_cache() -> None:
    resolver.invalidate()
    yield
    resolver.invalidate()


def test_repository_upsert_get_raw_and_list_all(db: Session) -> None:
    created = repository.upsert(
        db,
        "smtp_host",
        "smtp.example.test",
        is_secret=False,
        requires_restart=False,
        updated_by="admin-1",
    )

    assert created.group == "third_party"
    assert created.value_type == "string"
    assert repository.get_raw(db, "smtp_host") is created
    assert repository.list_all(db) == [created]

    updated = repository.upsert(
        db,
        "smtp_host",
        "mail.example.test",
        is_secret=False,
        requires_restart=True,
        updated_by="admin-2",
    )

    assert updated is created
    assert updated.value == "mail.example.test"
    assert updated.requires_restart is True
    assert updated.updated_by == "admin-2"


def test_resolve_prefers_database_value_over_environment(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_HOST", "env.example.test")
    repository.upsert(
        db,
        "smtp_host",
        "database.example.test",
        is_secret=False,
        requires_restart=False,
        updated_by="admin",
    )

    assert resolver.resolve(db, "smtp_host") == "database.example.test"


def test_resolve_uses_environment_when_database_has_no_value(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_HOST", "env.example.test")

    assert resolver.resolve(db, "smtp_host") == "env.example.test"


def test_resolve_uses_registry_default_when_database_and_environment_are_unset(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MEDIA_STORAGE_PROVIDER", raising=False)

    assert resolver.resolve(db, "media_storage_provider") == CONFIG_REGISTRY[
        "media_storage_provider"
    ].default


def test_resolve_decrypts_secret_database_value(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    plain = "test-secret-value"
    repository.upsert(
        db,
        "smtp_password",
        encrypt_value(plain),
        is_secret=True,
        requires_restart=False,
        updated_by="admin",
    )

    assert resolver.resolve(db, "smtp_password") == plain


def test_resolve_caches_values_and_invalidate_forces_database_lookup(db: Session) -> None:
    repository.upsert(
        db,
        "smtp_host",
        "database.example.test",
        is_secret=False,
        requires_restart=False,
        updated_by="admin",
    )

    with patch.object(resolver.repository, "get_raw", wraps=repository.get_raw) as get_raw:
        assert resolver.resolve(db, "smtp_host") == "database.example.test"
        assert resolver.resolve(db, "smtp_host") == "database.example.test"
        assert get_raw.call_count == 1

        resolver.invalidate("smtp_host")

        assert resolver.resolve(db, "smtp_host") == "database.example.test"
        assert get_raw.call_count == 2


def test_resolve_reads_monkeypatched_environment_on_cache_miss(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_HOST", "first.example.test")
    assert resolver.resolve(db, "smtp_host") == "first.example.test"

    resolver.invalidate("smtp_host")
    monkeypatch.setenv("SMTP_HOST", "second.example.test")

    assert resolver.resolve(db, "smtp_host") == "second.example.test"


def test_environment_accessor_mapping_covers_the_registry() -> None:
    assert set(resolver._ENV_ACCESSORS) == set(CONFIG_REGISTRY)


def test_get_config_resolver_returns_module_singleton() -> None:
    assert resolver.get_config_resolver() is resolver
