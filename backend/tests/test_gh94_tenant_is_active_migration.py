from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0075_gh94_retire_tenant_is_active.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("gh94_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_schema(engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "tenants",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("lifecycle_status", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    )
    metadata.create_all(engine)


def _run_migration(connection, migration, name: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, name)()


def test_upgrade_drops_only_a_consistent_compatibility_projection() -> None:
    engine = create_engine("sqlite://")
    _create_legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants VALUES ('active', 'active', true), "
                 "('provisioning', 'provisioning', false), "
                 "('frozen', 'frozen', false), ('suspended', 'suspended', false)")
        )
        _run_migration(connection, _load_migration(), "upgrade")

    assert "is_active" not in {column["name"] for column in inspect(engine).get_columns("tenants")}
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT id, lifecycle_status FROM tenants ORDER BY id")
        ).all() == [
            ("active", "active"),
            ("frozen", "frozen"),
            ("provisioning", "provisioning"),
            ("suspended", "suspended"),
        ]
    engine.dispose()


@pytest.mark.parametrize(
    ("tenant_id", "lifecycle_status", "legacy_value"),
    [
        ("active-mismatch", "active", False),
        ("provisioning-mismatch", "provisioning", True),
        ("frozen-mismatch", "frozen", True),
        ("suspended-mismatch", "suspended", True),
    ],
)
def test_upgrade_fails_closed_without_repairing_projection_mismatch(
    tenant_id: str, lifecycle_status: str, legacy_value: bool
) -> None:
    engine = create_engine("sqlite://")
    _create_legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, lifecycle_status, is_active) "
                "VALUES (:id, :status, :legacy_value)"
            ),
            {
                "id": tenant_id,
                "status": lifecycle_status,
                "legacy_value": legacy_value,
            },
        )
        with pytest.raises(RuntimeError, match="refusing to drop"):
            _run_migration(connection, _load_migration(), "upgrade")

    assert "is_active" in {column["name"] for column in inspect(engine).get_columns("tenants")}
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT lifecycle_status, is_active FROM tenants WHERE id = :id"),
            {"id": tenant_id},
        ).one() == (lifecycle_status, legacy_value)
    engine.dispose()


def test_downgrade_recreates_boolean_from_lifecycle_status() -> None:
    engine = create_engine("sqlite://")
    _create_legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants VALUES ('active', 'active', true), "
                 "('frozen', 'frozen', false), ('provisioning', 'provisioning', false), "
                 "('suspended', 'suspended', false)")
        )
        migration = _load_migration()
        _run_migration(connection, migration, "upgrade")
        _run_migration(connection, migration, "downgrade")
        assert connection.execute(
            text("SELECT id, lifecycle_status, is_active FROM tenants ORDER BY id")
        ).all() == [
            ("active", "active", True),
            ("frozen", "frozen", False),
            ("provisioning", "provisioning", False),
            ("suspended", "suspended", False),
        ]
    engine.dispose()
