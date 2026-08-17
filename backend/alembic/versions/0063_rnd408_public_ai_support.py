"""Add public AI support tables for anonymous visitor pre-sales chat
(RND-408).

These tables mirror the shape of the tenant-scoped ai_chat_sessions/
ai_chat_messages/ai_query_audit_logs/ai_handoff tables but deliberately
have no tenant_id, admin_user_id, or foreign keys into tenant/admin data.
They are keyed only by an anonymous visitor_id cookie, keeping the public
surface isolated from purchased-tenant AI sessions and from archive data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0063"
down_revision: Union[str, None] = "0062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_public_chat_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("visitor_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ai_public_chat_sessions"),
    )
    op.create_index(
        "ix_ai_public_chat_sessions_visitor_created",
        "ai_public_chat_sessions",
        ["visitor_id", "created_at", "id"],
    )

    op.create_table(
        "ai_public_chat_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("visitor_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), nullable=True),
        sa.Column("response_status", sa.String(length=32), nullable=True),
        sa.Column("index_version_id", sa.BigInteger(), nullable=True),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ai_public_chat_sessions.id"], name="fk_ai_public_chat_messages_session_id"
        ),
        sa.ForeignKeyConstraint(
            ["index_version_id"],
            ["kb_index_versions.id"],
            name="fk_ai_public_chat_messages_index_version_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_public_chat_messages"),
    )
    op.create_index(
        "ix_ai_public_chat_messages_session_created",
        "ai_public_chat_messages",
        ["session_id", "created_at", "id"],
    )
    op.create_index(
        "ix_ai_public_chat_messages_visitor", "ai_public_chat_messages", ["visitor_id"]
    )

    op.create_table(
        "ai_public_query_audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("visitor_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("retrieved_chunk_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("index_version_id", sa.BigInteger(), nullable=True),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("response_status", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["ai_public_chat_sessions.id"],
            name="fk_ai_public_query_audit_logs_session_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["index_version_id"],
            ["kb_index_versions.id"],
            name="fk_ai_public_query_audit_logs_index_version_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_public_query_audit_logs"),
    )
    op.create_index(
        "ix_ai_public_query_audit_logs_visitor_created",
        "ai_public_query_audit_logs",
        ["visitor_id", "created_at", "id"],
    )

    op.create_table(
        "ai_public_handoff",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("visitor_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("redacted_summary", sa.Text(), nullable=False),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("resolution_category", sa.String(length=32), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["ai_public_chat_sessions.id"],
            name="fk_ai_public_handoff_session_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_public_handoff"),
    )
    op.create_index(
        "ix_ai_public_handoff_visitor_created", "ai_public_handoff", ["visitor_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_public_handoff_visitor_created", table_name="ai_public_handoff")
    op.drop_table("ai_public_handoff")

    op.drop_index("ix_ai_public_query_audit_logs_visitor_created", table_name="ai_public_query_audit_logs")
    op.drop_table("ai_public_query_audit_logs")

    op.drop_index("ix_ai_public_chat_messages_visitor", table_name="ai_public_chat_messages")
    op.drop_index("ix_ai_public_chat_messages_session_created", table_name="ai_public_chat_messages")
    op.drop_table("ai_public_chat_messages")

    op.drop_index("ix_ai_public_chat_sessions_visitor_created", table_name="ai_public_chat_sessions")
    op.drop_table("ai_public_chat_sessions")
