from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0054_rnd403_wechat_refunds.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd403_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_0053_schema(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE refund_orders ("
                "id TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_ref TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE refund_events ("
                "id TEXT PRIMARY KEY, provider_ref TEXT NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO refund_orders VALUES ('legacy-order', 'wechat_pay', 'R1')")
        )
        connection.execute(
            text("INSERT INTO refund_events VALUES ('legacy-event', 'R1')")
        )


def test_migration_roundtrip_preserves_legacy_refund_evidence(monkeypatch) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    _create_0053_schema(engine)
    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        inspector = inspect(connection)
        order_columns = {
            column["name"] for column in inspector.get_columns("refund_orders")
        }
        event_columns = {
            column["name"] for column in inspector.get_columns("refund_events")
        }
        assert "provider_refund_id" in order_columns
        assert {
            "provider_refund_id",
            "provider_order_ref",
            "provider_transaction_id",
        }.issubset(event_columns)
        assert "uq_refund_orders_provider_refund_id" in {
            item["name"]
            for item in inspector.get_unique_constraints("refund_orders")
        }
        assert connection.execute(
            text("SELECT provider_refund_id FROM refund_orders WHERE id='legacy-order'")
        ).scalar_one() is None

        connection.execute(
            text(
                "INSERT INTO refund_orders "
                "(id, provider, provider_ref, provider_refund_id) "
                "VALUES ('new-order', 'wechat_pay', 'R2', 'wechat-refund-1')"
            )
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO refund_orders "
                "(id, provider, provider_ref, provider_refund_id) "
                "VALUES ('duplicate', 'wechat_pay', 'R3', 'wechat-refund-1')"
            )
        )

    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.downgrade()
        assert "provider_refund_id" not in {
            column["name"] for column in inspect(connection).get_columns("refund_orders")
        }
        assert connection.execute(
            text("SELECT count(*) FROM refund_orders WHERE id='legacy-order'")
        ).scalar_one() == 1
        migration.upgrade()
        assert "provider_refund_id" in {
            column["name"] for column in inspect(connection).get_columns("refund_orders")
        }


def test_migration_extends_the_actual_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0054"
    assert migration.down_revision == "0053"
