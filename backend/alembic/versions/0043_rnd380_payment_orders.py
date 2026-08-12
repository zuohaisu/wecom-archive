"""Add provider-neutral payment orders and verified payment events.

Revision ID: 0043
Revises: 0042
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("plan_code", sa.String(length=64), nullable=False),
        sa.Column("plan_name", sa.String(length=128), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_order_ref", sa.String(length=64), nullable=False),
        sa.Column("provider_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("provider_state", sa.String(length=32), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default="creating", nullable=False
        ),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("activation_id", sa.String(length=36), nullable=True),
        sa.Column("failure_code", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_payment_orders_amount"),
        sa.CheckConstraint(
            "length(currency) = 3", name="ck_payment_orders_currency"
        ),
        sa.CheckConstraint(
            "status IN ('creating', 'pending', 'paid_activation_pending', "
            "'succeeded', 'closed', 'failed')",
            name="ck_payment_orders_status",
        ),
        sa.CheckConstraint(
            "created_at < expires_at", name="ck_payment_orders_expiry"
        ),
        sa.CheckConstraint(
            "status NOT IN ('paid_activation_pending', 'succeeded') OR ("
            "paid_at IS NOT NULL AND provider_transaction_id IS NOT NULL)",
            name="ck_payment_orders_paid_result",
        ),
        sa.CheckConstraint(
            "status != 'succeeded' OR ("
            "activation_id IS NOT NULL AND activated_at IS NOT NULL)",
            name="ck_payment_orders_activation_result",
        ),
        sa.CheckConstraint(
            "status != 'failed' OR failure_code IS NOT NULL",
            name="ck_payment_orders_failed_code",
        ),
        sa.ForeignKeyConstraint(["activation_id"], ["subscription_activations.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["billing_plans.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "provider_order_ref",
            name="uq_payment_orders_provider_ref",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_transaction_id",
            name="uq_payment_orders_provider_transaction",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_payment_orders_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_payment_orders_tenant_created",
        "payment_orders",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "payment_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_event_id", sa.String(length=128), nullable=False),
        sa.Column("provider_transaction_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('callback', 'query')", name="ck_payment_events_source"
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64", name="ck_payment_events_payload_hash"
        ),
        sa.ForeignKeyConstraint(["order_id"], ["payment_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "provider_event_id", name="uq_payment_events_provider_event"
        ),
    )
    op.create_index(
        "ix_payment_events_order_created",
        "payment_events",
        ["order_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_payment_events_order_created", table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_index("ix_payment_orders_tenant_created", table_name="payment_orders")
    op.drop_table("payment_orders")
