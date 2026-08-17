"""Add ai_feedback table (RND-161 / T6).

Revision ID: 0061
Revises: 0060
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: Union[str, None] = "0060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("admin_user_id", sa.String(length=36), nullable=False),
        sa.Column("feedback_type", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column("product_version", sa.String(length=64), nullable=True),
        sa.Column("page_id", sa.String(length=64), nullable=True),
        sa.Column("browser_info", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_feedback_tenant_id"),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_users.id"], name="fk_ai_feedback_admin_user_id"),
        sa.PrimaryKeyConstraint("id", name="pk_ai_feedback"),
    )
    op.create_index("ix_ai_feedback_tenant_created", "ai_feedback", ["tenant_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_ai_feedback_tenant_created", table_name="ai_feedback")
    op.drop_table("ai_feedback")
