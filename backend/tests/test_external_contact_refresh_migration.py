"""Migration contract for durable external-contact refresh tasks."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0036_external_contact_refresh_tasks.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "external_contact_refresh_tasks_migration", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_refresh_task_migration_creates_coalesced_queue_and_downgrades(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'refresh-tasks.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO tenants (id) VALUES ('tenant-a')"))

    connection = engine.connect()
    migration = _load_migration()
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )

    migration.upgrade()
    inspector = inspect(connection)
    assert "external_contact_refresh_tasks" in inspector.get_table_names()
    assert {
        "id",
        "tenant_id",
        "external_userid",
        "source",
        "state",
        "attempt_count",
        "next_attempt_at",
        "last_attempt_at",
        "last_error_class",
        "created_at",
        "updated_at",
    } <= {column["name"] for column in inspector.get_columns("external_contact_refresh_tasks")}

    connection.execute(
        text(
            "INSERT INTO external_contact_refresh_tasks "
            "(tenant_id, external_userid, source, state, next_attempt_at) "
            "VALUES ('tenant-a', 'wm-test', 'callback', 'pending', CURRENT_TIMESTAMP)"
        )
    )
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO external_contact_refresh_tasks "
                "(tenant_id, external_userid, source, state, next_attempt_at) "
                "VALUES ('tenant-a', 'wm-test', 'callback', 'pending', CURRENT_TIMESTAMP)"
            )
        )
    connection.rollback()

    migration.downgrade()
    assert "external_contact_refresh_tasks" not in inspect(connection).get_table_names()
    connection.close()
    engine.dispose()
