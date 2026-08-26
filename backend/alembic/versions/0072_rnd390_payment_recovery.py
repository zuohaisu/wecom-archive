"""Add bounded payment recovery, reconciliation findings, and global ops alerts.

Revision ID: 0072
Revises: 0071
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: Union[str, None] = "0071"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FINDING_KIND_CHECK = (
    "kind IN ('payment_pending_timeout', 'payment_channel_paid_local_pending', "
    "'payment_activation_pending', 'payment_query_failed', "
    "'payment_reconciliation_mismatch', 'payment_callback_signature_failure', "
    "'payment_callback_decrypt_failure')"
)
_NOTIFICATION_SUBJECT_CHECK = (
    "subject_type IN ('subscription', 'payment_order', 'refund_order', "
    "'payment_recovery_finding')"
)


def upgrade() -> None:
    # Batch mode keeps this migration executable in the SQLite-compatible CI
    # path while emitting ordinary ALTER statements for PostgreSQL production.
    with op.batch_alter_table("payment_orders") as batch:
        batch.add_column(
            sa.Column(
                "recovery_state",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'automatic'"),
            )
        )
        batch.add_column(sa.Column("recovery_reason_code", sa.String(length=64)))
        batch.add_column(
            sa.Column(
                "query_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        for name in (
            "last_query_at",
            "next_query_at",
            "recovery_lease_until",
            "last_reconciled_at",
        ):
            batch.add_column(
                sa.Column(name, sa.DateTime(timezone=True), nullable=True)
            )
        batch.create_check_constraint(
            "ck_payment_orders_recovery_state",
            "recovery_state IN ('automatic', 'manual_recovery', 'not_required')",
        )
        batch.create_check_constraint(
            "ck_payment_orders_query_attempt_count",
            "query_attempt_count >= 0",
        )
        batch.create_index(
            "ix_payment_orders_recovery_due",
            ["provider", "recovery_state", "next_query_at"],
            unique=False,
        )
    op.execute(
        "UPDATE payment_orders SET recovery_state = CASE "
        "WHEN status IN ('creating', 'pending', 'paid_activation_pending') "
        "THEN 'automatic' ELSE 'not_required' END, "
        "next_query_at = CASE "
        "WHEN status IN ('creating', 'pending', 'paid_activation_pending') "
        "THEN created_at ELSE NULL END"
    )

    op.create_table(
        "payment_recovery_findings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("payment_order_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column(
            "dedupe_key", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "first_detected_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "last_detected_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            _FINDING_KIND_CHECK,
            name="ck_payment_recovery_findings_kind",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_payment_recovery_findings_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_payment_recovery_findings_status",
        ),
        sa.CheckConstraint(
            "occurrence_count >= 1",
            name="ck_payment_recovery_findings_occurrence_count",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["payment_order_id"], ["payment_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dedupe_key", name="uq_payment_recovery_findings_dedupe"
        ),
    )
    op.create_index(
        "ix_payment_recovery_findings_status_last_detected",
        "payment_recovery_findings",
        ["status", "last_detected_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_recovery_findings_tenant_status",
        "payment_recovery_findings",
        ["tenant_id", "status"],
        unique=False,
    )

    # Reuse the durable notification outbox for a global, operations-only
    # finding that cannot carry a trusted tenant foreign key.
    with op.batch_alter_table("billing_notification_intents") as batch:
        batch.drop_constraint(
            "uq_billing_notification_intents_tenant_dedupe", type_="unique"
        )
        batch.drop_constraint(
            "ck_billing_notification_intents_subject_type", type_="check"
        )
        batch.alter_column(
            "tenant_id", existing_type=sa.String(length=36), nullable=True
        )
        batch.create_unique_constraint(
            "uq_billing_notification_intents_dedupe", ["dedupe_key"]
        )
        batch.create_check_constraint(
            "ck_billing_notification_intents_subject_type",
            _NOTIFICATION_SUBJECT_CHECK,
        )
    with op.batch_alter_table("billing_notification_attempts") as batch:
        batch.alter_column(
            "tenant_id", existing_type=sa.String(length=36), nullable=True
        )


def downgrade() -> None:
    # A downgrade is not used by production deployment. Remove global alert
    # rows first because the pre-RND-390 outbox requires a tenant foreign key.
    op.execute("DELETE FROM billing_notification_attempts WHERE tenant_id IS NULL")
    op.execute("DELETE FROM billing_notification_intents WHERE tenant_id IS NULL")
    with op.batch_alter_table("billing_notification_attempts") as batch:
        batch.alter_column(
            "tenant_id", existing_type=sa.String(length=36), nullable=False
        )
    with op.batch_alter_table("billing_notification_intents") as batch:
        batch.drop_constraint(
            "uq_billing_notification_intents_dedupe", type_="unique"
        )
        batch.drop_constraint(
            "ck_billing_notification_intents_subject_type", type_="check"
        )
        batch.alter_column(
            "tenant_id", existing_type=sa.String(length=36), nullable=False
        )
        batch.create_unique_constraint(
            "uq_billing_notification_intents_tenant_dedupe",
            ["tenant_id", "dedupe_key"],
        )
        batch.create_check_constraint(
            "ck_billing_notification_intents_subject_type",
            "subject_type IN ('subscription', 'payment_order', 'refund_order')",
        )

    op.drop_index(
        "ix_payment_recovery_findings_tenant_status",
        table_name="payment_recovery_findings",
    )
    op.drop_index(
        "ix_payment_recovery_findings_status_last_detected",
        table_name="payment_recovery_findings",
    )
    op.drop_table("payment_recovery_findings")
    with op.batch_alter_table("payment_orders") as batch:
        batch.drop_index("ix_payment_orders_recovery_due")
        batch.drop_constraint(
            "ck_payment_orders_query_attempt_count", type_="check"
        )
        batch.drop_constraint(
            "ck_payment_orders_recovery_state", type_="check"
        )
        for name in (
            "last_reconciled_at",
            "recovery_lease_until",
            "next_query_at",
            "last_query_at",
            "query_attempt_count",
            "recovery_reason_code",
            "recovery_state",
        ):
            batch.drop_column(name)
