"""Add authoritative refunds and reversible payment term grants.

Revision ID: 0052
Revises: 0051
"""

from datetime import datetime, timedelta
from typing import Sequence, Union
import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _add_activation_snapshots() -> None:
    with op.batch_alter_table("subscription_activations") as batch_op:
        batch_op.add_column(
            sa.Column("applied_grace_ends_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(sa.Column("prior_subscription_existed", sa.Boolean()))
        batch_op.add_column(sa.Column("prior_plan_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("prior_status", sa.String(length=16)))
        batch_op.add_column(
            sa.Column("prior_starts_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(sa.Column("prior_ends_at", sa.DateTime(timezone=True)))
        batch_op.add_column(
            sa.Column("prior_grace_ends_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(sa.Column("prior_cancel_at_period_end", sa.Boolean()))
        batch_op.add_column(sa.Column("prior_source", sa.String(length=32)))
        batch_op.add_column(sa.Column("prior_renewal_count", sa.Integer()))
        batch_op.add_column(sa.Column("prior_revision", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_subscription_activations_prior_plan_id",
            "billing_plans",
            ["prior_plan_id"],
            ["id"],
        )


def _create_term_grants() -> None:
    op.create_table(
        "subscription_term_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("payment_order_id", sa.String(length=36), nullable=False),
        sa.Column("activation_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_code", sa.String(length=64)),
        sa.Column("activation_kind", sa.String(length=16), nullable=False),
        sa.Column("prior_subscription_existed", sa.Boolean()),
        sa.Column("prior_plan_id", sa.String(length=36)),
        sa.Column("prior_status", sa.String(length=16)),
        sa.Column("prior_starts_at", sa.DateTime(timezone=True)),
        sa.Column("prior_ends_at", sa.DateTime(timezone=True)),
        sa.Column("prior_grace_ends_at", sa.DateTime(timezone=True)),
        sa.Column("prior_cancel_at_period_end", sa.Boolean()),
        sa.Column("prior_source", sa.String(length=32)),
        sa.Column("prior_renewal_count", sa.Integer()),
        sa.Column("prior_revision", sa.Integer()),
        sa.Column("applied_plan_id", sa.String(length=36), nullable=False),
        sa.Column("applied_status", sa.String(length=16), nullable=False),
        sa.Column("applied_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "applied_grace_ends_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("applied_cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("applied_source", sa.String(length=32), nullable=False),
        sa.Column("applied_renewal_count", sa.Integer(), nullable=False),
        sa.Column("applied_revision", sa.Integer(), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'reversed', 'manual_recovery_required')",
            name="ck_term_grants_status",
        ),
        sa.CheckConstraint(
            "activation_kind IN ('activation', 'renewal')",
            name="ck_term_grants_activation_kind",
        ),
        sa.CheckConstraint(
            "applied_starts_at < applied_ends_at AND "
            "applied_ends_at < applied_grace_ends_at",
            name="ck_term_grants_applied_range",
        ),
        sa.CheckConstraint(
            "applied_renewal_count >= 0 AND applied_revision >= 1",
            name="ck_term_grants_applied_counters",
        ),
        sa.CheckConstraint(
            "status != 'manual_recovery_required' OR failure_code IS NOT NULL",
            name="ck_term_grants_manual_failure",
        ),
        sa.ForeignKeyConstraint(["payment_order_id"], ["payment_orders.id"]),
        sa.ForeignKeyConstraint(
            ["activation_id"], ["subscription_activations.id"]
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
        sa.ForeignKeyConstraint(["prior_plan_id"], ["billing_plans.id"]),
        sa.ForeignKeyConstraint(["applied_plan_id"], ["billing_plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "payment_order_id", name="uq_term_grants_payment_order"
        ),
        sa.UniqueConstraint("activation_id", name="uq_term_grants_activation"),
    )
    op.create_index(
        "ix_term_grants_tenant_created",
        "subscription_term_grants",
        ["tenant_id", "created_at"],
    )


def _create_refunds() -> None:
    op.create_table(
        "refund_orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("payment_order_id", sa.String(length=36), nullable=False),
        sa.Column("term_grant_id", sa.String(length=36), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_ref", sa.String(length=64)),
        sa.Column("provider_state", sa.String(length=32)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column(
            "approved_by_platform_admin_id", sa.String(length=36), nullable=False
        ),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("failure_code", sa.String(length=64)),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_accepted_at", sa.DateTime(timezone=True)),
        sa.Column("succeeded_at", sa.DateTime(timezone=True)),
        sa.Column("entitlement_reversed_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_refund_orders_amount"),
        sa.CheckConstraint(
            "length(currency) = 3", name="ck_refund_orders_currency"
        ),
        sa.CheckConstraint(
            "status IN ('created', 'processing', 'succeeded', 'closed', "
            "'abnormal', 'manual_recovery_required')",
            name="ck_refund_orders_status",
        ),
        sa.CheckConstraint(
            "status != 'succeeded' OR "
            "(succeeded_at IS NOT NULL AND entitlement_reversed_at IS NOT NULL)",
            name="ck_refund_orders_succeeded_result",
        ),
        sa.CheckConstraint(
            "status NOT IN ('abnormal', 'manual_recovery_required') OR "
            "failure_code IS NOT NULL",
            name="ck_refund_orders_failure_code",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["payment_order_id"], ["payment_orders.id"]),
        sa.ForeignKeyConstraint(
            ["term_grant_id"], ["subscription_term_grants.id"]
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_platform_admin_id"], ["platform_admins.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "payment_order_id", name="uq_refund_orders_payment_order"
        ),
        sa.UniqueConstraint("term_grant_id", name="uq_refund_orders_term_grant"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_refund_orders_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "provider", "provider_ref", name="uq_refund_orders_provider_ref"
        ),
    )
    op.create_index(
        "ix_refund_orders_tenant_created",
        "refund_orders",
        ["tenant_id", "requested_at"],
    )
    op.create_table(
        "refund_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("refund_order_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_event_id", sa.String(length=128), nullable=False),
        sa.Column("provider_ref", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('callback', 'query')", name="ck_refund_events_source"
        ),
        sa.CheckConstraint(
            "state IN ('PROCESSING', 'SUCCESS', 'CLOSED', 'ABNORMAL')",
            name="ck_refund_events_state",
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_refund_events_amount"),
        sa.CheckConstraint(
            "length(currency) = 3", name="ck_refund_events_currency"
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64", name="ck_refund_events_payload_hash"
        ),
        sa.ForeignKeyConstraint(["refund_order_id"], ["refund_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "provider_event_id", name="uq_refund_events_provider_event"
        ),
    )
    op.create_index(
        "ix_refund_events_refund_created",
        "refund_events",
        ["refund_order_id", "created_at"],
    )


def _backfill_legacy_grants() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT po.id AS payment_order_id, po.activation_id, po.tenant_id, "
            "po.plan_id, sa.subscription_id, sa.activation_kind, "
            "sa.applied_starts_at, sa.applied_ends_at, "
            "sa.applied_renewal_count, sa.subscription_revision, sa.source "
            "FROM payment_orders po JOIN subscription_activations sa "
            "ON sa.id = po.activation_id "
            "WHERE po.status = 'succeeded' AND po.activation_id IS NOT NULL"
        )
    ).mappings()
    for row in rows:
        ends_at = _as_datetime(row["applied_ends_at"])
        bind.execute(
            sa.text(
                "INSERT INTO subscription_term_grants "
                "(id, payment_order_id, activation_id, tenant_id, subscription_id, "
                "status, failure_code, activation_kind, applied_plan_id, "
                "applied_status, applied_starts_at, applied_ends_at, "
                "applied_grace_ends_at, applied_cancel_at_period_end, "
                "applied_source, applied_renewal_count, applied_revision) VALUES "
                "(:id, :payment_order_id, :activation_id, :tenant_id, "
                ":subscription_id, 'manual_recovery_required', "
                "'legacy_snapshot_unavailable', :activation_kind, :plan_id, "
                "'active', :starts_at, :ends_at, :grace_ends_at, false, :source, "
                ":renewal_count, :revision)"
            ),
            {
                "id": str(uuid.uuid4()),
                "payment_order_id": row["payment_order_id"],
                "activation_id": row["activation_id"],
                "tenant_id": row["tenant_id"],
                "subscription_id": row["subscription_id"],
                "activation_kind": row["activation_kind"],
                "plan_id": row["plan_id"],
                "starts_at": _as_datetime(row["applied_starts_at"]),
                "ends_at": ends_at,
                "grace_ends_at": ends_at + timedelta(days=7),
                "source": row["source"],
                "renewal_count": row["applied_renewal_count"],
                "revision": row["subscription_revision"],
            },
        )


def upgrade() -> None:
    _add_activation_snapshots()
    _create_term_grants()
    _create_refunds()
    _backfill_legacy_grants()


def downgrade() -> None:
    op.drop_index("ix_refund_events_refund_created", table_name="refund_events")
    op.drop_table("refund_events")
    op.drop_index("ix_refund_orders_tenant_created", table_name="refund_orders")
    op.drop_table("refund_orders")
    op.drop_index(
        "ix_term_grants_tenant_created", table_name="subscription_term_grants"
    )
    op.drop_table("subscription_term_grants")
    with op.batch_alter_table("subscription_activations") as batch_op:
        batch_op.drop_constraint(
            "fk_subscription_activations_prior_plan_id", type_="foreignkey"
        )
        for column in (
            "prior_revision",
            "prior_renewal_count",
            "prior_source",
            "prior_cancel_at_period_end",
            "prior_grace_ends_at",
            "prior_ends_at",
            "prior_starts_at",
            "prior_status",
            "prior_plan_id",
            "prior_subscription_existed",
            "applied_grace_ends_at",
        ):
            batch_op.drop_column(column)
