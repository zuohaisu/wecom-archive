"""Add AI chat message feedback columns and ai_handoff table (RND-357 / T3).

Revision ID: 0059
Revises: 0058
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: Union[str, None] = "0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("ai_chat_messages") as batch_op:
        batch_op.add_column(sa.Column("helpful", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("feedback_note", sa.Text(), nullable=True))

    op.create_table(
        "ai_handoff",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("admin_user_id", sa.String(length=36), nullable=False),
        sa.Column("redacted_summary", sa.Text(), nullable=False),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_handoff_tenant_id"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ai_chat_sessions.id"], name="fk_ai_handoff_session_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_users.id"], name="fk_ai_handoff_admin_user_id"),
        sa.PrimaryKeyConstraint("id", name="pk_ai_handoff"),
    )
    op.create_index("ix_ai_handoff_tenant_created", "ai_handoff", ["tenant_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_ai_handoff_tenant_created", table_name="ai_handoff")
    op.drop_table("ai_handoff")

    with op.batch_alter_table("ai_chat_messages") as batch_op:
        batch_op.drop_column("feedback_note")
        batch_op.drop_column("helpful")
