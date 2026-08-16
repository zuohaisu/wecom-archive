"""Add authoritative subscription grace and tenant service lifecycle state.

Revision ID: 0051
Revises: 0050
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_grace_ends_at(table_name: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            sa.text(
                f"UPDATE {table_name} "
                "SET grace_ends_at = datetime(ends_at, '+7 days') "
                "WHERE grace_ends_at IS NULL"
            )
        )
        return
    op.execute(
        sa.text(
            f"UPDATE {table_name} "
            "SET grace_ends_at = ends_at + INTERVAL '7 days' "
            "WHERE grace_ends_at IS NULL"
        )
    )


def upgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.add_column(
            sa.Column("grace_ends_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "cancel_at_period_end",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.drop_constraint("ck_subscriptions_status", type_="check")

    with op.batch_alter_table("subscription_history") as batch_op:
        batch_op.add_column(
            sa.Column("grace_ends_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "cancel_at_period_end",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.drop_constraint("ck_subscription_history_status", type_="check")

    _backfill_grace_ends_at("subscriptions")
    _backfill_grace_ends_at("subscription_history")
    op.execute(sa.text("UPDATE subscriptions SET status = 'grace' WHERE status = 'past_due'"))
    op.execute(
        sa.text(
            "UPDATE subscription_history SET status = 'grace' "
            "WHERE status = 'past_due'"
        )
    )

    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.alter_column("grace_ends_at", nullable=False)
        batch_op.create_check_constraint(
            "ck_subscriptions_status",
            "status IN ('trial', 'active', 'grace', 'expired', 'canceled')",
        )
        batch_op.create_check_constraint(
            "ck_subscriptions_grace_date_range",
            "ends_at < grace_ends_at",
        )

    with op.batch_alter_table("subscription_history") as batch_op:
        batch_op.alter_column("grace_ends_at", nullable=False)
        batch_op.create_check_constraint(
            "ck_subscription_history_status",
            "status IN ('trial', 'active', 'grace', 'expired', 'canceled')",
        )
        batch_op.create_check_constraint(
            "ck_subscription_history_grace_date_range",
            "ends_at < grace_ends_at",
        )

    with op.batch_alter_table("tenants") as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_revision",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("suspension_reason", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "suspended_by_platform_admin_id",
                sa.String(length=36),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "suspension_previous_status",
                sa.String(length=16),
                nullable=True,
            )
        )
        batch_op.drop_constraint("ck_tenants_lifecycle_status", type_="check")

    op.execute(
        sa.text(
            "UPDATE tenants "
            "SET suspended_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP), "
            "suspension_reason = 'legacy_suspension', "
            "suspension_previous_status = 'active' "
            "WHERE lifecycle_status = 'suspended'"
        )
    )

    with op.batch_alter_table("tenants") as batch_op:
        batch_op.create_check_constraint(
            "ck_tenants_lifecycle_status",
            "lifecycle_status IN ('provisioning', 'active', 'frozen', 'suspended')",
        )
        batch_op.create_check_constraint(
            "ck_tenants_lifecycle_revision",
            "lifecycle_revision >= 1",
        )
        batch_op.create_check_constraint(
            "ck_tenants_suspension_previous_status",
            "suspension_previous_status IS NULL OR "
            "suspension_previous_status IN ('provisioning', 'active', 'frozen')",
        )


def downgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_constraint(
            "ck_subscriptions_grace_date_range", type_="check"
        )
        batch_op.drop_constraint("ck_subscriptions_status", type_="check")
    with op.batch_alter_table("subscription_history") as batch_op:
        batch_op.drop_constraint(
            "ck_subscription_history_grace_date_range", type_="check"
        )
        batch_op.drop_constraint("ck_subscription_history_status", type_="check")

    op.execute(sa.text("UPDATE subscriptions SET status = 'past_due' WHERE status = 'grace'"))
    op.execute(
        sa.text(
            "UPDATE subscription_history SET status = 'past_due' "
            "WHERE status = 'grace'"
        )
    )

    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("cancel_at_period_end")
        batch_op.drop_column("grace_ends_at")
        batch_op.create_check_constraint(
            "ck_subscriptions_status",
            "status IN ('trial', 'active', 'past_due', 'expired', 'canceled')",
        )

    with op.batch_alter_table("subscription_history") as batch_op:
        batch_op.drop_column("cancel_at_period_end")
        batch_op.drop_column("grace_ends_at")
        batch_op.create_check_constraint(
            "ck_subscription_history_status",
            "status IN ('trial', 'active', 'past_due', 'expired', 'canceled')",
        )

    with op.batch_alter_table("tenants") as batch_op:
        batch_op.drop_constraint(
            "ck_tenants_suspension_previous_status", type_="check"
        )
        batch_op.drop_constraint("ck_tenants_lifecycle_revision", type_="check")
        batch_op.drop_constraint("ck_tenants_lifecycle_status", type_="check")

    op.execute(
        sa.text(
            "UPDATE tenants SET lifecycle_status = 'suspended', is_active = false "
            "WHERE lifecycle_status = 'frozen'"
        )
    )

    with op.batch_alter_table("tenants") as batch_op:
        batch_op.drop_column("suspension_previous_status")
        batch_op.drop_column("suspended_by_platform_admin_id")
        batch_op.drop_column("suspension_reason")
        batch_op.drop_column("suspended_at")
        batch_op.drop_column("frozen_at")
        batch_op.drop_column("lifecycle_revision")
        batch_op.create_check_constraint(
            "ck_tenants_lifecycle_status",
            "lifecycle_status IN ('provisioning', 'active', 'suspended')",
        )
