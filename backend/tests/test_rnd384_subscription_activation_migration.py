from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.db.base import Base
from app.db.models import BillingPlan, Subscription, Tenant

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0042_rnd384_subscription_activation.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd384_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_upgrade_contract_downgrade_and_reupgrade(monkeypatch) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, BillingPlan.__table__, Subscription.__table__],
    )

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        inspector = inspect(connection)
        assert "subscription_activations" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns(
            "subscription_activations"
        )} >= {
            "tenant_id",
            "plan_code",
            "source",
            "idempotency_key_hash",
            "command_hash",
            "status",
            "failure_code",
            "subscription_id",
            "subscription_revision",
            "applied_renewal_count",
            "activation_kind",
            "applied_starts_at",
            "applied_ends_at",
            "applied_at",
        }
        unique_names = {
            item["name"]
            for item in inspector.get_unique_constraints("subscription_activations")
        }
        assert "uq_subscription_activations_source_key" in unique_names

        migration.downgrade()
        assert "subscription_activations" not in inspect(connection).get_table_names()

        migration.upgrade()
        assert "subscription_activations" in inspect(connection).get_table_names()


def test_migration_extends_the_actual_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0042"
    assert migration.down_revision == "0041"
