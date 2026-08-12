"""Add the authoritative plan, subscription and entitlement foundation.

Revision ID: 0041
Revises: 0040
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ANNUAL_PLAN_ID = "00000000-0000-0000-0000-000000000099"
ARCHIVE_ACCESS_ID = "00000000-0000-0000-0001-000000000099"
UNLIMITED_SEATS_ID = "00000000-0000-0000-0002-000000000099"


def upgrade() -> None:
    billing_plans = op.create_table(
        "billing_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("billing_period_months", sa.Integer(), nullable=False),
        sa.Column("storage_quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount_cents >= 0", name="ck_billing_plans_amount"),
        sa.CheckConstraint("length(currency) = 3", name="ck_billing_plans_currency"),
        sa.CheckConstraint(
            "billing_period_months > 0", name="ck_billing_plans_period_months"
        ),
        sa.CheckConstraint(
            "storage_quota_bytes >= 0", name="ck_billing_plans_storage_quota"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_billing_plans_code"),
    )
    plan_entitlements = op.create_table(
        "plan_entitlements",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["billing_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id",
            "capability",
            name="uq_plan_entitlements_plan_capability",
        ),
    )
    op.create_index(
        "ix_plan_entitlements_plan_id",
        "plan_entitlements",
        ["plan_id"],
        unique=False,
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("renewal_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('trial', 'active', 'past_due', 'expired', 'canceled')",
            name="ck_subscriptions_status",
        ),
        sa.CheckConstraint("starts_at < ends_at", name="ck_subscriptions_date_range"),
        sa.CheckConstraint(
            "renewal_count >= 0", name="ck_subscriptions_renewal_count"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_subscriptions_revision"),
        sa.ForeignKeyConstraint(["plan_id"], ["billing_plans.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_subscriptions_tenant"),
    )
    op.create_table(
        "subscription_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("renewal_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("change_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('trial', 'active', 'past_due', 'expired', 'canceled')",
            name="ck_subscription_history_status",
        ),
        sa.CheckConstraint(
            "starts_at < ends_at", name="ck_subscription_history_date_range"
        ),
        sa.CheckConstraint(
            "renewal_count >= 0",
            name="ck_subscription_history_renewal_count",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_subscription_history_revision"),
        sa.ForeignKeyConstraint(["plan_id"], ["billing_plans.id"]),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "revision",
            name="uq_subscription_history_revision",
        ),
    )
    op.create_index(
        "ix_subscription_history_tenant_id",
        "subscription_history",
        ["tenant_id"],
        unique=False,
    )

    op.bulk_insert(
        billing_plans,
        [
            {
                "id": ANNUAL_PLAN_ID,
                "code": "annual_base_cny_99",
                "display_name": "年度基础套餐",
                "is_active": True,
                "amount_cents": 9900,
                "currency": "CNY",
                "billing_period_months": 12,
                "storage_quota_bytes": 5 * 1024**3,
            }
        ],
    )
    op.bulk_insert(
        plan_entitlements,
        [
            {
                "id": ARCHIVE_ACCESS_ID,
                "plan_id": ANNUAL_PLAN_ID,
                "capability": "archive_access",
                "is_enabled": True,
            },
            {
                "id": UNLIMITED_SEATS_ID,
                "plan_id": ANNUAL_PLAN_ID,
                "capability": "unlimited_seats",
                "is_enabled": True,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscription_history_tenant_id", table_name="subscription_history"
    )
    op.drop_table("subscription_history")
    op.drop_table("subscriptions")
    op.drop_index("ix_plan_entitlements_plan_id", table_name="plan_entitlements")
    op.drop_table("plan_entitlements")
    op.drop_table("billing_plans")
