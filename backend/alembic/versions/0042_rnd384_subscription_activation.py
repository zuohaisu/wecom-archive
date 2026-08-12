"""Add durable, provider-neutral paid subscription activation attempts.

Revision ID: 0042
Revises: 0041
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subscription_activations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("plan_code", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("trusted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=32), nullable=True),
        sa.Column("subscription_id", sa.String(length=36), nullable=True),
        sa.Column("subscription_revision", sa.Integer(), nullable=True),
        sa.Column("applied_renewal_count", sa.Integer(), nullable=True),
        sa.Column("activation_kind", sa.String(length=16), nullable=True),
        sa.Column("applied_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending', 'applied', 'failed')",
            name="ck_subscription_activations_status",
        ),
        sa.CheckConstraint(
            "activation_kind IS NULL OR activation_kind IN ('activation', 'renewal')",
            name="ck_subscription_activations_kind",
        ),
        sa.CheckConstraint(
            "subscription_revision IS NULL OR subscription_revision >= 1",
            name="ck_subscription_activations_revision",
        ),
        sa.CheckConstraint(
            "applied_renewal_count IS NULL OR applied_renewal_count >= 0",
            name="ck_subscription_activations_renewal_count",
        ),
        sa.CheckConstraint(
            "status != 'failed' OR failure_code IS NOT NULL",
            name="ck_subscription_activations_failed_code",
        ),
        sa.CheckConstraint(
            "status != 'applied' OR ("
            "subscription_id IS NOT NULL AND subscription_revision IS NOT NULL AND "
            "applied_renewal_count IS NOT NULL AND "
            "activation_kind IS NOT NULL AND applied_starts_at IS NOT NULL AND "
            "applied_ends_at IS NOT NULL AND applied_at IS NOT NULL)",
            name="ck_subscription_activations_applied_result",
        ),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "idempotency_key_hash",
            name="uq_subscription_activations_source_key",
        ),
    )
    op.create_index(
        "ix_subscription_activations_tenant_created",
        "subscription_activations",
        ["tenant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscription_activations_tenant_created",
        table_name="subscription_activations",
    )
    op.drop_table("subscription_activations")
