"""Add AI support knowledge-base index, chat, and audit tables (RND-356 / T2).

Revision ID: 0057
Revises: 0056
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kb_index_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("triggered_by", sa.String(length=64), nullable=False),
        sa.Column("plan_summary", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_kb_index_versions"),
    )

    op.create_table(
        "kb_document_chunks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("index_version_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("topic_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("access_level", sa.String(length=16), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("doc_version", sa.String(length=32), nullable=False),
        sa.Column("doc_path", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["index_version_id"], ["kb_index_versions.id"], name="fk_kb_document_chunks_index_version_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kb_document_chunks"),
    )
    op.create_index(
        "ix_kb_document_chunks_source_chunk", "kb_document_chunks", ["source_id", "chunk_index"]
    )
    op.create_index(
        "ix_kb_document_chunks_access_locale_version",
        "kb_document_chunks",
        ["access_level", "locale", "index_version_id"],
    )
    # pg_trgm GIN index for word_similarity-ranked retrieval (see
    # app/services/ai/retriever.py) — NOT to_tsvector: 'simple' FTS cannot
    # match a Chinese query phrase against a sub-phrase of a longer chunk
    # (no CJK segmenter). Mirrors ix_archive_messages_content_text_trgm
    # (alembic 0013), which already established pg_trgm as this repo's
    # working answer for Chinese-heavy search. Extension already enabled
    # repo-wide by alembic 0013.
    op.execute(
        "CREATE INDEX ix_kb_document_chunks_content_text_trgm "
        "ON kb_document_chunks USING gin (content_text gin_trgm_ops)"
    )

    op.create_table(
        "ai_chat_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("admin_user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_chat_sessions_tenant_id"),
        sa.ForeignKeyConstraint(
            ["admin_user_id"], ["admin_users.id"], name="fk_ai_chat_sessions_admin_user_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_chat_sessions"),
    )
    op.create_index(
        "ix_ai_chat_sessions_tenant_admin_user", "ai_chat_sessions", ["tenant_id", "admin_user_id"]
    )

    op.create_table(
        "ai_chat_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), nullable=True),
        sa.Column("response_status", sa.String(length=32), nullable=True),
        sa.Column("index_version_id", sa.BigInteger(), nullable=True),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["session_id"], ["ai_chat_sessions.id"], name="fk_ai_chat_messages_session_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_chat_messages_tenant_id"),
        sa.ForeignKeyConstraint(
            ["index_version_id"], ["kb_index_versions.id"], name="fk_ai_chat_messages_index_version_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_chat_messages"),
    )
    op.create_index(
        "ix_ai_chat_messages_session_created", "ai_chat_messages", ["session_id", "created_at", "id"]
    )
    op.create_index("ix_ai_chat_messages_tenant", "ai_chat_messages", ["tenant_id"])

    op.create_table(
        "ai_query_audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("admin_user_id", sa.String(length=36), nullable=False),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_query_audit_logs_tenant_id"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ai_chat_sessions.id"], name="fk_ai_query_audit_logs_session_id"
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"], ["admin_users.id"], name="fk_ai_query_audit_logs_admin_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["index_version_id"], ["kb_index_versions.id"], name="fk_ai_query_audit_logs_index_version_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_query_audit_logs"),
    )
    op.create_index(
        "ix_ai_query_audit_logs_tenant_created", "ai_query_audit_logs", ["tenant_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_query_audit_logs_tenant_created", table_name="ai_query_audit_logs")
    op.drop_table("ai_query_audit_logs")

    op.drop_index("ix_ai_chat_messages_tenant", table_name="ai_chat_messages")
    op.drop_index("ix_ai_chat_messages_session_created", table_name="ai_chat_messages")
    op.drop_table("ai_chat_messages")

    op.drop_index("ix_ai_chat_sessions_tenant_admin_user", table_name="ai_chat_sessions")
    op.drop_table("ai_chat_sessions")

    op.execute("DROP INDEX ix_kb_document_chunks_content_text_trgm")
    op.drop_index("ix_kb_document_chunks_access_locale_version", table_name="kb_document_chunks")
    op.drop_index("ix_kb_document_chunks_source_chunk", table_name="kb_document_chunks")
    op.drop_table("kb_document_chunks")

    op.drop_table("kb_index_versions")
