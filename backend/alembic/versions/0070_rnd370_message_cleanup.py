"""Add asynchronous bulk message-cleanup tasks (RND-370).

Revision ID: 0070
Revises: 0069
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0070"
down_revision: Union[str, None] = "0069"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_cleanup_previews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("matched", sa.Integer(), nullable=False),
        sa.Column("filter_snapshot", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_message_cleanup_previews_expires_at", "message_cleanup_previews", ["expires_at"]
    )
    op.create_table(
        "message_cleanup_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_admin_user_id", sa.String(length=36), nullable=False),
        sa.Column("filter_snapshot", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("filter_summary", sa.Text(), nullable=False),
        sa.Column("preview_version", sa.String(length=36), nullable=False),
        sa.Column("preview_matched", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("total_matched", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("succeeded", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("locked", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("moved_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("releasable_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_summary", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'partial', 'completed', 'failed', 'canceled')",
            name="ck_message_cleanup_tasks_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_message_cleanup_tasks_revision"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_message_cleanup_tasks_tenant_status", "message_cleanup_tasks", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_message_cleanup_tasks_created_at", "message_cleanup_tasks", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_message_cleanup_tasks_created_at", table_name="message_cleanup_tasks")
    op.drop_index("ix_message_cleanup_tasks_tenant_status", table_name="message_cleanup_tasks")
    op.drop_table("message_cleanup_tasks")
    op.drop_index("ix_message_cleanup_previews_expires_at", table_name="message_cleanup_previews")
    op.drop_table("message_cleanup_previews")
