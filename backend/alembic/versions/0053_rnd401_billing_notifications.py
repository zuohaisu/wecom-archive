"""Add durable billing notification intents and delivery attempts.

Revision ID: 0053
Revises: 0052
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "billing_notification_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("audience", sa.String(length=16), nullable=False),
        sa.Column("context_key", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("source_revision", sa.Integer()),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancellation_code", sa.String(length=64)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("canceled_at", sa.DateTime(timezone=True)),
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
            "subject_type IN ('subscription', 'payment_order', 'refund_order')",
            name="ck_billing_notification_intents_subject_type",
        ),
        sa.CheckConstraint(
            "audience IN ('owner', 'operations')",
            name="ck_billing_notification_intents_audience",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'canceled', 'failed')",
            name="ck_billing_notification_intents_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_billing_notification_intents_attempt_count",
        ),
        sa.CheckConstraint(
            "length(context_key) = 64 AND length(dedupe_key) = 64",
            name="ck_billing_notification_intents_hashes",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "dedupe_key",
            name="uq_billing_notification_intents_tenant_dedupe",
        ),
    )
    op.create_index(
        "ix_billing_notification_intents_due",
        "billing_notification_intents",
        ["status", "next_attempt_at", "scheduled_at"],
    )
    op.create_index(
        "ix_billing_notification_intents_tenant_subject",
        "billing_notification_intents",
        ["tenant_id", "subject_type", "subject_id"],
    )
    op.create_table(
        "billing_notification_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=64)),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('sent', 'failed')",
            name="ck_billing_notification_attempts_outcome",
        ),
        sa.CheckConstraint(
            "attempt_no >= 1",
            name="ck_billing_notification_attempts_number",
        ),
        sa.CheckConstraint(
            "outcome != 'failed' OR failure_code IS NOT NULL",
            name="ck_billing_notification_attempts_failure_code",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["billing_notification_intents.id"]
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "intent_id",
            "attempt_no",
            name="uq_billing_notification_attempts_intent_number",
        ),
    )
    op.create_index(
        "ix_billing_notification_attempts_tenant_attempted",
        "billing_notification_attempts",
        ["tenant_id", "attempted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_notification_attempts_tenant_attempted",
        table_name="billing_notification_attempts",
    )
    op.drop_table("billing_notification_attempts")
    op.drop_index(
        "ix_billing_notification_intents_tenant_subject",
        table_name="billing_notification_intents",
    )
    op.drop_index(
        "ix_billing_notification_intents_due",
        table_name="billing_notification_intents",
    )
    op.drop_table("billing_notification_intents")
