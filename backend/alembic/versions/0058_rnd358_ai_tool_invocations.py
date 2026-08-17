"""Add AI read-only diagnostic tool invocation audit table (RND-358 / T4).

Revision ID: 0058
Revises: 0057
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_tool_invocations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("admin_user_id", sa.String(length=36), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("consent_given", sa.Boolean(), nullable=False),
        sa.Column("fields_returned", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("result_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_tool_invocations_tenant_id"),
        sa.ForeignKeyConstraint(
            ["admin_user_id"], ["admin_users.id"], name="fk_ai_tool_invocations_admin_user_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_tool_invocations"),
    )
    op.create_index(
        "ix_ai_tool_invocations_tenant_created", "ai_tool_invocations", ["tenant_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_tool_invocations_tenant_created", table_name="ai_tool_invocations")
    op.drop_table("ai_tool_invocations")
