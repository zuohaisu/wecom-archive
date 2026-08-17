"""Make ai_query_audit_logs.session_id ON DELETE SET NULL (RND-357 / T3).

Revision ID: 0060
Revises: 0059

RND-355's original FK (migration 0055) had no ON DELETE clause, which made
RND-357's user-initiated session deletion endpoint fail with a foreign-key
violation the moment any audit log row referenced that session — audit
rows are retained under RND-359's own policy and must survive the session
they were generated for being deleted, exactly like AiHandoff.session_id
(already ON DELETE SET NULL since its introduction in migration 0057).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0060"
down_revision: Union[str, None] = "0059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("fk_ai_query_audit_logs_session_id", "ai_query_audit_logs", type_="foreignkey")
    op.create_foreign_key(
        "fk_ai_query_audit_logs_session_id",
        "ai_query_audit_logs",
        "ai_chat_sessions",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_ai_query_audit_logs_session_id", "ai_query_audit_logs", type_="foreignkey")
    op.create_foreign_key(
        "fk_ai_query_audit_logs_session_id",
        "ai_query_audit_logs",
        "ai_chat_sessions",
        ["session_id"],
        ["id"],
    )
