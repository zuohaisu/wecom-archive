from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.db.base import Base
from app.db.models import (
    BillingPlan,
    Subscription,
    SubscriptionActivation,
    Tenant,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0043_rnd380_payment_orders.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd380_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_upgrade_contract_downgrade_and_reupgrade(monkeypatch) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            BillingPlan.__table__,
            Subscription.__table__,
            SubscriptionActivation.__table__,
        ],
    )
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()
        inspector = inspect(connection)
        assert {"payment_orders", "payment_events"}.issubset(
            inspector.get_table_names()
        )
        order_columns = {
            column["name"] for column in inspector.get_columns("payment_orders")
        }
        assert {
            "tenant_id",
            "plan_id",
            "plan_code",
            "amount_cents",
            "currency",
            "provider",
            "provider_order_ref",
            "provider_transaction_id",
            "status",
            "checkout_url",
            "idempotency_key_hash",
            "expires_at",
            "paid_at",
            "activation_id",
            "failure_code",
        }.issubset(order_columns)
        unique_names = {
            item["name"]
            for item in inspector.get_unique_constraints("payment_orders")
        }
        assert {
            "uq_payment_orders_provider_ref",
            "uq_payment_orders_provider_transaction",
            "uq_payment_orders_tenant_idempotency",
        }.issubset(unique_names)
        migration.downgrade()
        assert "payment_orders" not in inspect(connection).get_table_names()
        migration.upgrade()
        assert "payment_orders" in inspect(connection).get_table_names()


def test_migration_extends_the_actual_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0043"
    assert migration.down_revision == "0042"
