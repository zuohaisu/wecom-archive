"""Revoke association and original-content preservation (RND-201).

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-18

WeCom delivers a message revocation as its own archive_messages row
(msgtype="revoke") whose decrypted payload carries a
revoke.pre_msgid field naming the msgid of the message being revoked
(confirmed against WeCom's official session-archive docs:
https://developer.work.weixin.qq.com/document/path/91774 — example
payload: {"msgid":"...","action":"recall","msgtype":"revoke",
"revoke":{"pre_msgid":"..."}}).

This migration adds:

  - archive_messages.is_revoked / revoked_at -- additive, nullable-safe
    columns recording that an already-archived message was revoked,
    without ever touching content_text/structured_content/decrypted_payload
    or any media_files row. Backfilled false/NULL for every existing row
    (no historical revoke event is fabricated).

  - message_revocations -- one row per revoke event, associating it with
    the original message it targets (possibly not yet archived). This is
    a genuinely new "cross-row" concept (a revoke event's own
    archive_messages row is distinct from the message it targets), so it
    is modeled as its own association table rather than a self-referential
    column pair on archive_messages, following the existing
    archive_message_recipients / media_files precedent of a child table
    FK'd to archive_messages.id.

Both changes are purely additive -- no existing column is altered,
dropped, or backfilled with fabricated data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "archive_messages",
        sa.Column(
            "is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "archive_messages",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "message_revocations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=True
        ),
        sa.Column(
            "revoke_event_message_id",
            sa.BigInteger(),
            sa.ForeignKey("archive_messages.id"),
            nullable=False,
        ),
        sa.Column("revoke_event_msgid", sa.String(64), nullable=False),
        sa.Column("revoke_event_msgtime", sa.BigInteger(), nullable=True),
        sa.Column("target_msgid", sa.String(64), nullable=True),
        sa.Column(
            "original_message_id",
            sa.BigInteger(),
            sa.ForeignKey("archive_messages.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "revoke_event_message_id",
            name="uq_message_revocations_tenant_revoke_event",
        ),
    )
    op.create_index(
        "ix_message_revocations_tenant_id", "message_revocations", ["tenant_id"]
    )
    op.create_index(
        "ix_message_revocations_revoke_event_message_id",
        "message_revocations",
        ["revoke_event_message_id"],
    )
    op.create_index(
        "ix_message_revocations_original_message_id",
        "message_revocations",
        ["original_message_id"],
    )
    op.create_index(
        "ix_message_revocations_tenant_target_msgid",
        "message_revocations",
        ["tenant_id", "target_msgid"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_message_revocations_tenant_target_msgid",
        table_name="message_revocations",
    )
    op.drop_index(
        "ix_message_revocations_original_message_id",
        table_name="message_revocations",
    )
    op.drop_index(
        "ix_message_revocations_revoke_event_message_id",
        table_name="message_revocations",
    )
    op.drop_index("ix_message_revocations_tenant_id", table_name="message_revocations")
    op.drop_table("message_revocations")
    op.drop_column("archive_messages", "revoked_at")
    op.drop_column("archive_messages", "is_revoked")
