from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0072_rnd390_payment_recovery.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd390_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_pre_rnd390_schema(engine) -> None:
    metadata = sa.MetaData()
    sa.Table("tenants", metadata, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table(
        "payment_orders",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Table(
        "billing_notification_intents",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "dedupe_key",
            name="uq_billing_notification_intents_tenant_dedupe",
        ),
        sa.CheckConstraint(
            "subject_type IN ('subscription', 'payment_order', 'refund_order')",
            name="ck_billing_notification_intents_subject_type",
        ),
    )
    sa.Table(
        "billing_notification_attempts",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("intent_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
    )
    metadata.create_all(engine)


def test_migration_upgrade_downgrade_and_reupgrade_on_sqlite(monkeypatch) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    _create_pre_rnd390_schema(engine)

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        inspector = inspect(connection)
        payment_columns = {
            column["name"] for column in inspector.get_columns("payment_orders")
        }
        assert {
            "recovery_state",
            "recovery_reason_code",
            "query_attempt_count",
            "last_query_at",
            "next_query_at",
            "recovery_lease_until",
            "last_reconciled_at",
        }.issubset(payment_columns)
        assert "payment_recovery_findings" in inspector.get_table_names()
        assert "uq_billing_notification_intents_dedupe" in {
            item["name"]
            for item in inspector.get_unique_constraints("billing_notification_intents")
        }
        assert next(
            column
            for column in inspector.get_columns("billing_notification_intents")
            if column["name"] == "tenant_id"
        )["nullable"]

        migration.downgrade()
        inspector = inspect(connection)
        assert "payment_recovery_findings" not in inspector.get_table_names()
        assert "recovery_state" not in {
            column["name"] for column in inspector.get_columns("payment_orders")
        }
        assert not next(
            column
            for column in inspector.get_columns("billing_notification_intents")
            if column["name"] == "tenant_id"
        )["nullable"]

        migration.upgrade()
        assert "payment_recovery_findings" in inspect(connection).get_table_names()


def test_migration_extends_the_actual_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0072"
    assert migration.down_revision == "0071"
