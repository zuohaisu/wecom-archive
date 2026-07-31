"""Acceptance coverage for RND-246 application configuration storage."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, DateTime, String, Text, create_engine, inspect
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import AppConfigStore

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0031_app_config_store.py"
)


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AppConfigStore.__table__])
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def test_model_has_application_level_config_store_contract() -> None:
    table = AppConfigStore.__table__

    assert set(table.columns.keys()) == {
        "key",
        "group",
        "value",
        "is_secret",
        "value_type",
        "requires_restart",
        "updated_at",
        "updated_by",
    }
    assert list(table.primary_key.columns.keys()) == ["key"]
    assert isinstance(table.c.key.type, String)
    assert isinstance(table.c.group.type, String)
    assert isinstance(table.c.value.type, Text)
    assert isinstance(table.c.is_secret.type, Boolean)
    assert isinstance(table.c.value_type.type, String)
    assert isinstance(table.c.requires_restart.type, Boolean)
    assert isinstance(table.c.updated_at.type, DateTime)
    assert table.c.updated_at.type.timezone is True
    assert isinstance(table.c.updated_by.type, String)
    assert table.c.value.nullable is True
    assert table.c.updated_by.nullable is True
    assert table.c.is_secret.nullable is False
    assert table.c.requires_restart.nullable is False
    assert table.c.is_secret.default.arg is False
    assert table.c.requires_restart.default.arg is False
    assert table.c.is_secret.server_default is not None
    assert table.c.requires_restart.server_default is not None
    assert table.c.updated_at.server_default is not None
    assert table.c.updated_at.onupdate is not None
    assert table.foreign_keys == set()


def test_orm_can_insert_update_and_query_config_value(db: Session) -> None:
    db.add(
        AppConfigStore(
            key="wecom_corp_id",
            group="wecom",
            value="ww-initial",
            is_secret=False,
            value_type="string",
            requires_restart=True,
            updated_by="admin-1",
        )
    )
    db.commit()

    stored = db.get(AppConfigStore, "wecom_corp_id")
    assert stored is not None
    assert stored.value == "ww-initial"
    assert stored.updated_at is not None

    stored.value = "ww-updated"
    stored.updated_by = "admin-2"
    db.commit()
    db.expire_all()

    updated = db.get(AppConfigStore, "wecom_corp_id")
    assert updated is not None
    assert updated.value == "ww-updated"
    assert updated.updated_by == "admin-2"
    assert updated.is_secret is False
    assert updated.requires_restart is True


def test_migration_creates_and_drops_app_config_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("rnd246_migration", _MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    engine = create_engine(f"sqlite:///{tmp_path / 'app-config-store.db'}")
    connection = engine.connect()
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))

    migration.upgrade()
    inspector = inspect(connection)
    assert "app_config_store" in inspector.get_table_names()
    columns = {column["name"]: column for column in inspector.get_columns("app_config_store")}
    assert set(columns) == {
        "key",
        "group",
        "value",
        "is_secret",
        "value_type",
        "requires_restart",
        "updated_at",
        "updated_by",
    }
    assert columns["key"]["primary_key"] == 1
    assert isinstance(columns["key"]["type"], String)
    assert isinstance(columns["group"]["type"], String)
    assert isinstance(columns["value"]["type"], Text)
    assert isinstance(columns["is_secret"]["type"], Boolean)
    assert isinstance(columns["value_type"]["type"], String)
    assert isinstance(columns["requires_restart"]["type"], Boolean)
    assert isinstance(columns["updated_at"]["type"], DateTime)
    assert isinstance(columns["updated_by"]["type"], String)
    assert columns["value"]["nullable"] is True
    assert columns["updated_by"]["nullable"] is True
    assert columns["is_secret"]["nullable"] is False
    assert columns["requires_restart"]["nullable"] is False
    assert inspector.get_foreign_keys("app_config_store") == []

    migration.downgrade()
    assert "app_config_store" not in inspect(connection).get_table_names()
    connection.close()
    engine.dispose()
