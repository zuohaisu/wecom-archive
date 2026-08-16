from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0052_rnd399_refund_authority.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd399_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_0051_schema(engine) -> None:
    statements = [
        "CREATE TABLE tenants (id TEXT PRIMARY KEY)",
        "CREATE TABLE billing_plans (id TEXT PRIMARY KEY)",
        "CREATE TABLE platform_admins (id TEXT PRIMARY KEY)",
        "CREATE TABLE subscriptions (id TEXT PRIMARY KEY, tenant_id TEXT, plan_id TEXT)",
        """CREATE TABLE subscription_activations (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, plan_code TEXT NOT NULL,
            source TEXT NOT NULL, idempotency_key_hash TEXT NOT NULL,
            command_hash TEXT NOT NULL, trusted_at DATETIME NOT NULL,
            status TEXT NOT NULL, failure_code TEXT, subscription_id TEXT,
            subscription_revision INTEGER, applied_renewal_count INTEGER,
            activation_kind TEXT, applied_starts_at DATETIME,
            applied_ends_at DATETIME, applied_at DATETIME,
            created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE payment_orders (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, plan_id TEXT NOT NULL,
            plan_code TEXT NOT NULL, plan_name TEXT NOT NULL,
            amount_cents INTEGER NOT NULL, currency TEXT NOT NULL,
            provider TEXT NOT NULL, provider_order_ref TEXT NOT NULL,
            provider_transaction_id TEXT, provider_state TEXT, status TEXT NOT NULL,
            checkout_url TEXT, idempotency_key_hash TEXT NOT NULL,
            activation_id TEXT, failure_code TEXT, created_at DATETIME,
            expires_at DATETIME, paid_at DATETIME, activated_at DATETIME,
            closed_at DATETIME, updated_at DATETIME
        )""",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _seed_legacy_success(connection) -> None:
    at = datetime(2026, 8, 16, tzinfo=timezone.utc)
    connection.execute(text("INSERT INTO tenants VALUES ('tenant')"))
    connection.execute(text("INSERT INTO billing_plans VALUES ('plan')"))
    connection.execute(text("INSERT INTO platform_admins VALUES ('admin')"))
    connection.execute(
        text("INSERT INTO subscriptions VALUES ('subscription', 'tenant', 'plan')")
    )
    connection.execute(
        text(
            "INSERT INTO subscription_activations "
            "(id, tenant_id, plan_code, source, idempotency_key_hash, command_hash, "
            "trusted_at, status, subscription_id, subscription_revision, "
            "applied_renewal_count, activation_kind, applied_starts_at, "
            "applied_ends_at, applied_at, created_at, updated_at) VALUES "
            "('activation', 'tenant', 'annual', 'wechat_pay', :hash, :hash, :at, "
            "'applied', 'subscription', 1, 0, 'activation', :at, "
            "'2027-08-16 00:00:00+00:00', :at, :at, :at)"
        ),
        {"hash": "a" * 64, "at": at},
    )
    connection.execute(
        text(
            "INSERT INTO payment_orders "
            "(id, tenant_id, plan_id, plan_code, plan_name, amount_cents, currency, "
            "provider, provider_order_ref, provider_transaction_id, status, "
            "idempotency_key_hash, activation_id, created_at, expires_at, paid_at, "
            "activated_at, updated_at) VALUES "
            "('payment', 'tenant', 'plan', 'annual', 'Annual', 9900, 'CNY', "
            "'wechat_pay', 'provider-order', 'provider-transaction', 'succeeded', "
            ":hash, 'activation', :at, '2026-08-16 00:15:00+00:00', :at, :at, :at)"
        ),
        {"hash": "b" * 64, "at": at},
    )


def test_migration_roundtrip_and_legacy_payment_fails_safe(monkeypatch) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    _create_0051_schema(engine)
    with engine.begin() as connection:
        _seed_legacy_success(connection)
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        tables = set(inspect(connection).get_table_names())
        assert {
            "subscription_term_grants",
            "refund_orders",
            "refund_events",
        }.issubset(tables)
        activation_columns = {
            column["name"]
            for column in inspect(connection).get_columns("subscription_activations")
        }
        assert {
            "applied_grace_ends_at",
            "prior_subscription_existed",
            "prior_grace_ends_at",
        }.issubset(activation_columns)
        legacy = connection.execute(
            text(
                "SELECT status, failure_code, applied_grace_ends_at "
                "FROM subscription_term_grants WHERE payment_order_id = 'payment'"
            )
        ).one()
        assert legacy.status == "manual_recovery_required"
        assert legacy.failure_code == "legacy_snapshot_unavailable"
        assert str(legacy.applied_grace_ends_at).startswith("2027-08-23")

        migration.downgrade()
        assert "refund_orders" not in inspect(connection).get_table_names()
        assert "prior_subscription_existed" not in {
            column["name"]
            for column in inspect(connection).get_columns("subscription_activations")
        }
        migration.upgrade()
        assert connection.execute(
            text("SELECT count(*) FROM subscription_term_grants")
        ).scalar_one() == 1


def test_migration_extends_the_actual_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0052"
    assert migration.down_revision == "0051"
