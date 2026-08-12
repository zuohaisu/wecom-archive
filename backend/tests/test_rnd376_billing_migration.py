from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.db.base import Base
from app.db.models import Tenant

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0041_rnd376_billing_foundation.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd376_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_upgrade_seed_downgrade_and_reupgrade(monkeypatch) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Tenant.__table__])

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        tables = set(inspect(connection).get_table_names())
        assert {
            "billing_plans",
            "plan_entitlements",
            "subscriptions",
            "subscription_history",
        }.issubset(tables)
        plan = connection.execute(
            text(
                "SELECT id, code, display_name, amount_cents, currency, "
                "billing_period_months, storage_quota_bytes, is_active "
                "FROM billing_plans"
            )
        ).mappings().one()
        assert dict(plan) == {
            "id": migration.ANNUAL_PLAN_ID,
            "code": "annual_base_cny_99",
            "display_name": "年度基础套餐",
            "amount_cents": 9900,
            "currency": "CNY",
            "billing_period_months": 12,
            "storage_quota_bytes": 5 * 1024**3,
            "is_active": 1,
        }
        capabilities = connection.execute(
            text(
                "SELECT capability FROM plan_entitlements "
                "WHERE plan_id = :plan_id ORDER BY capability"
            ),
            {"plan_id": migration.ANNUAL_PLAN_ID},
        ).scalars().all()
        assert capabilities == ["archive_access", "unlimited_seats"]

        migration.downgrade()
        assert "billing_plans" not in inspect(connection).get_table_names()

        migration.upgrade()
        count = connection.execute(
            text(
                "SELECT count(*) FROM billing_plans "
                "WHERE code = 'annual_base_cny_99'"
            )
        ).scalar_one()
        assert count == 1


def test_migration_extends_the_actual_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0041"
    assert migration.down_revision == "0040"
