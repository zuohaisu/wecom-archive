"""Browser-playable voice derivative metadata (RND-258).

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-27

Additive only. Existing rows continue serving their original objects until a
new download or manual backfill records a generated MP3/WAV derivative.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("media_files", sa.Column("playback_ref", sa.Text(), nullable=True))
    op.add_column(
        "media_files",
        sa.Column(
            "playback_status",
            sa.String(length=32),
            nullable=True,
            server_default=sa.text("'not_applicable'"),
        ),
    )
    op.create_index("ix_media_files_playback_status", "media_files", ["playback_status"])


def downgrade() -> None:
    op.drop_index("ix_media_files_playback_status", table_name="media_files")
    op.drop_column("media_files", "playback_status")
    op.drop_column("media_files", "playback_ref")
