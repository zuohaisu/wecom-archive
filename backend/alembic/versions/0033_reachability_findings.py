"""Create persistent reachability findings and automation run sources (RND-339).

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # RND-337's durable run is reused. These two sources distinguish local
    # evidence from a full reconciliation without creating a second state
    # machine or a second run table.
    op.drop_constraint(
        "ck_reachability_audit_runs_source_valid",
        "reachability_audit_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_reachability_audit_runs_source_valid",
        "reachability_audit_runs",
        "source IN ('manual', 'incremental', 'reconcile')",
    )
    op.create_table(
        "reachability_findings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("archive_message_id", sa.BigInteger(), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_run_id", sa.BigInteger(), nullable=False),
        sa.Column("last_run_id", sa.BigInteger(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'resolved')",
            name="ck_reachability_findings_status_valid",
        ),
        sa.CheckConstraint(
            "occurrence_count >= 0",
            name="ck_reachability_findings_occurrences_nonnegative",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "archive_message_id"],
            ["archive_messages.tenant_id", "archive_messages.id"],
            name="fk_reachability_findings_tenant_message",
        ),
        sa.ForeignKeyConstraint(["first_run_id"], ["reachability_audit_runs.id"]),
        sa.ForeignKeyConstraint(["last_run_id"], ["reachability_audit_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_reachability_findings_public_id"),
        sa.UniqueConstraint(
            "tenant_id", "archive_message_id", "reason_code", "algorithm_version",
            name="uq_reachability_findings_identity",
        ),
    )
    op.create_index(
        "ix_reachability_findings_tenant_status_last_seen",
        "reachability_findings",
        ["tenant_id", "status", "last_seen", "id"],
    )
    op.create_index(
        "ix_reachability_findings_tenant_message_active",
        "reachability_findings",
        ["tenant_id", "archive_message_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reachability_findings_tenant_message_active",
        table_name="reachability_findings",
    )
    op.drop_index(
        "ix_reachability_findings_tenant_status_last_seen",
        table_name="reachability_findings",
    )
    op.drop_table("reachability_findings")
    # Feature-owned automation history cannot satisfy RND-337's old enum.
    op.execute("DELETE FROM reachability_audit_runs WHERE source IN ('incremental', 'reconcile')")
    op.drop_constraint(
        "ck_reachability_audit_runs_source_valid",
        "reachability_audit_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_reachability_audit_runs_source_valid",
        "reachability_audit_runs",
        "source IN ('manual')",
    )
