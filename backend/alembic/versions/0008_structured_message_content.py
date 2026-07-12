"""Structured message content for basic structured message types (RND-197).

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-12

RND-196 built the Message Type Registry but stopped short of writing
structured parsers/renderers. RND-197 adds them for link/location/
markdown/news/miniprogram (full field extraction) and card/docmsg/
audio_doc (raw preservation + generic structured fallback only — see
app.structured_message_parser module docstring for why those three don't
get field extraction).

This column is deliberately separate from the existing decrypted_payload
JSONB column, which the decrypt script has a documented, intentional
constraint against populating (see "# SF-1" in
scripts/decrypt_wecom_messages_once.py) — reversing that constraint was
explicitly ruled out. structured_content stores only the type-specific
sub-payload (decrypted[msgtype], not the full decrypted envelope) plus
the parsed fields, scoped to exactly what RND-197's normalized contract
and raw-preservation requirement need:

    {"fields": {...} | null, "raw": {...}, "parse_warnings": [...]}

Additive only -- nullable, no backfill. Historical rows (and any row for
a msgtype outside RND-197's scope) simply have structured_content = NULL,
which the API/frontend already treat as "render the generic fallback."
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "archive_messages", sa.Column("structured_content", JSONB(), nullable=True)
    )
    op.create_index(
        "ix_archive_messages_structured_content_gin",
        "archive_messages",
        ["structured_content"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_archive_messages_structured_content_gin", table_name="archive_messages"
    )
    op.drop_column("archive_messages", "structured_content")
