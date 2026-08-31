from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0073_gh111_payment_recovery_finding_resolution.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("gh111_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_pre_gh111_schema(engine) -> None:
    metadata = sa.MetaData()
    sa.Table("platform_admins", metadata, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table(
        "payment_recovery_findings",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(36)),
        sa.Column("payment_order_id", sa.String(36)),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    metadata.create_all(engine)


def test_migration_adds_and_removes_auditable_finding_resolution_fields(monkeypatch) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    _create_pre_gh111_schema(engine)

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()
        inspector = inspect(connection)
        columns = {
            column["name"]
            for column in inspector.get_columns("payment_recovery_findings")
        }
        assert {
            "resolution_classification",
            "resolution_failure_class",
            "resolution_reason_code",
            "resolved_by_platform_admin_id",
        }.issubset(columns)
        assert any(
            foreign_key["constrained_columns"] == ["resolved_by_platform_admin_id"]
            for foreign_key in inspector.get_foreign_keys("payment_recovery_findings")
        )

        migration.downgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("payment_recovery_findings")
        }
        assert "resolution_classification" not in columns
        assert "resolved_by_platform_admin_id" not in columns

        migration.upgrade()
        assert "resolution_reason_code" in {
            column["name"]
            for column in inspect(connection).get_columns("payment_recovery_findings")
        }


def test_migration_extends_the_actual_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0073"
    assert migration.down_revision == "0072"
