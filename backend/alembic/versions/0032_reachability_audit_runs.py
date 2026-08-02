"""Create persistent reachability audit runs (RND-337).

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reachability_audit_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("algorithm_version", sa.String(length=32), nullable=False),
        sa.Column("scope_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope_max_message_id", sa.BigInteger(), nullable=False),
        sa.Column("matching_count", sa.Integer(), nullable=False),
        sa.Column("checked_count", sa.Integer(), nullable=False),
        sa.Column("reachable_count", sa.Integer(), nullable=False),
        sa.Column("unreachable_count", sa.Integer(), nullable=False),
        sa.Column("reason_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("safe_error_code", sa.String(length=48), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('checking', 'completed', 'incomplete', 'error')",
            name="ck_reachability_audit_runs_status_valid",
        ),
        sa.CheckConstraint(
            "source IN ('manual')",
            name="ck_reachability_audit_runs_source_valid",
        ),
        sa.CheckConstraint(
            "matching_count >= 0 AND checked_count >= 0 AND reachable_count >= 0 "
            "AND unreachable_count >= 0",
            name="ck_reachability_audit_runs_counts_nonnegative",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_reachability_audit_runs_public_id"),
    )
    op.create_index(
        "ix_reachability_audit_runs_tenant_created",
        "reachability_audit_runs",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "uq_reachability_audit_runs_tenant_checking",
        "reachability_audit_runs",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'checking'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_reachability_audit_runs_tenant_checking",
        table_name="reachability_audit_runs",
    )
    op.drop_index(
        "ix_reachability_audit_runs_tenant_created",
        table_name="reachability_audit_runs",
    )
    op.drop_table("reachability_audit_runs")
