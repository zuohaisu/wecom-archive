from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0051_rnd400_billing_lifecycle.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd400_migration", MIGRATION_PATH)
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
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(128), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("lifecycle_status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle_status IN ('provisioning', 'active', 'suspended')",
            name="ck_tenants_lifecycle_status",
        ),
    )
    sa.Table(
        "billing_plans",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "subscriptions",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("renewal_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('trial', 'active', 'past_due', 'expired', 'canceled')",
            name="ck_subscriptions_status",
        ),
        sa.CheckConstraint(
            "starts_at < ends_at",
            name="ck_subscriptions_date_range",
        ),
        sa.CheckConstraint(
            "renewal_count >= 0",
            name="ck_subscriptions_renewal_count",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_subscriptions_revision"),
        sa.UniqueConstraint("tenant_id", name="uq_subscriptions_tenant"),
    )
    sa.Table(
        "subscription_history",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subscription_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("renewal_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("change_kind", sa.String(32), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('trial', 'active', 'past_due', 'expired', 'canceled')",
            name="ck_subscription_history_status",
        ),
        sa.CheckConstraint(
            "starts_at < ends_at",
            name="ck_subscription_history_date_range",
        ),
        sa.CheckConstraint(
            "renewal_count >= 0",
            name="ck_subscription_history_renewal_count",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_subscription_history_revision",
        ),
        sa.UniqueConstraint(
            "subscription_id",
            "revision",
            name="uq_subscription_history_revision",
        ),
    )
    metadata.create_all(engine)


def _seed_legacy_rows(connection) -> None:
    created_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    starts_at = datetime(2025, 8, 1, tzinfo=timezone.utc)
    ends_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    connection.execute(
        text(
            "INSERT INTO tenants "
            "(id, name, slug, is_active, lifecycle_status, created_at, updated_at) "
            "VALUES "
            "('tenant-active', 'Active', 'active', true, 'active', :created, :created), "
            "('tenant-suspended', 'Suspended', 'suspended', false, 'suspended', :created, :created)"
        ),
        {"created": created_at},
    )
    connection.execute(
        text("INSERT INTO billing_plans (id) VALUES ('plan')")
    )
    connection.execute(
        text(
            "INSERT INTO subscriptions "
            "(id, tenant_id, plan_id, status, starts_at, ends_at, source, "
            "renewal_count, revision, created_at, updated_at) "
            "VALUES ('subscription', 'tenant-active', 'plan', 'past_due', "
            ":starts, :ends, 'legacy', 0, 1, :created, :created)"
        ),
        {"starts": starts_at, "ends": ends_at, "created": created_at},
    )
    connection.execute(
        text(
            "INSERT INTO subscription_history "
            "(id, subscription_id, tenant_id, plan_id, status, starts_at, ends_at, "
            "source, renewal_count, revision, change_kind, recorded_at) "
            "VALUES ('history', 'subscription', 'tenant-active', 'plan', 'past_due', "
            ":starts, :ends, 'legacy', 0, 1, 'assigned', :created)"
        ),
        {"starts": starts_at, "ends": ends_at, "created": created_at},
    )


def test_migration_upgrade_downgrade_and_reupgrade_preserves_safe_semantics(
    monkeypatch,
) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    _create_legacy_schema(engine)
    with engine.begin() as connection:
        _seed_legacy_rows(connection)
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        inspector = inspect(connection)
        subscription_columns = {
            column["name"] for column in inspector.get_columns("subscriptions")
        }
        tenant_columns = {
            column["name"] for column in inspector.get_columns("tenants")
        }
        assert {"grace_ends_at", "cancel_at_period_end"}.issubset(
            subscription_columns
        )
        assert {
            "lifecycle_revision",
            "frozen_at",
            "suspended_at",
            "suspension_reason",
            "suspended_by_platform_admin_id",
            "suspension_previous_status",
        }.issubset(tenant_columns)
        upgraded = connection.execute(
            text(
                "SELECT status, grace_ends_at, cancel_at_period_end "
                "FROM subscriptions WHERE id = 'subscription'"
            )
        ).one()
        assert upgraded.status == "grace"
        assert str(upgraded.grace_ends_at).startswith("2026-08-08")
        assert upgraded.cancel_at_period_end in {False, 0}
        suspended = connection.execute(
            text(
                "SELECT suspension_reason, suspension_previous_status, lifecycle_revision "
                "FROM tenants WHERE id = 'tenant-suspended'"
            )
        ).one()
        assert suspended.suspension_reason == "legacy_suspension"
        assert suspended.suspension_previous_status == "active"
        assert suspended.lifecycle_revision == 1

        connection.execute(
            text(
                "UPDATE tenants SET lifecycle_status = 'frozen', is_active = false, "
                "frozen_at = CURRENT_TIMESTAMP WHERE id = 'tenant-active'"
            )
        )
        migration.downgrade()
        assert "grace_ends_at" not in {
            column["name"] for column in inspect(connection).get_columns("subscriptions")
        }
        assert connection.execute(
            text("SELECT status FROM subscriptions WHERE id = 'subscription'")
        ).scalar_one() == "past_due"
        assert connection.execute(
            text("SELECT lifecycle_status FROM tenants WHERE id = 'tenant-active'")
        ).scalar_one() == "suspended"

        migration.upgrade()
        assert connection.execute(
            text("SELECT status FROM subscriptions WHERE id = 'subscription'")
        ).scalar_one() == "grace"
        assert connection.execute(
            text("SELECT lifecycle_status FROM tenants WHERE id = 'tenant-active'")
        ).scalar_one() == "suspended"


def test_migration_extends_the_actual_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0051"
    assert migration.down_revision == "0050"
