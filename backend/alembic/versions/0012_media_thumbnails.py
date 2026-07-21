"""List/timeline thumbnail metadata for image media (RND-207).

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-20

RND-207 stops loading full-resolution originals in the chat timeline: the
list renders a small server-generated thumbnail while the viewer still opens
the original. This migration adds the columns that make a thumbnail
discoverable and let the list reserve the correct layout box (killing the
image-swap layout shift) without fetching the original.

    thumbnail_ref          -- the thumbnail object key / local path, held in
                              the SAME storage backend as the original
                              (media_files.storage_backend). NULL means "no
                              thumbnail available" -> the reader falls back to
                              serving the original, so this is fully backward
                              compatible with every pre-existing row.
    image_width /
    image_height           -- the ORIGINAL image's pixel dimensions (after
                              EXIF-orientation normalization), used purely to
                              reserve an aspect-ratio box in the list before
                              the thumbnail loads. NULL -> the frontend
                              degrades to its existing no-reservation
                              behavior.
    thumbnail_status       -- backfill/generation bookkeeping, mirroring the
                              RND-186 migration_* pattern (migration 0006):
                              NULL   = never attempted
                              generated = thumbnail_ref is populated & usable
                              failed = attempted, no usable thumbnail (a
                                       --retry backfill run reconsiders these)
                              skipped = deliberately not thumbnailed (e.g. a
                                        non-image row, or thumbnails disabled)
    thumbnail_attempted_at -- UTC timestamp of the last generation attempt
    thumbnail_error        -- short, sanitized diagnostic tag only -- never a
                              raw path, sdkfileid, object key, or exception
                              string / bytes.

Additive only: no backfill, no destructive change, no rewrite of any
existing column. Every existing row gets all six columns = NULL, i.e. "no
thumbnail yet" -- serving is unaffected until the backfill script
(scripts/backfill_thumbnails_once.py) is run.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("media_files", sa.Column("thumbnail_ref", sa.Text(), nullable=True))
    op.add_column("media_files", sa.Column("image_width", sa.Integer(), nullable=True))
    op.add_column("media_files", sa.Column("image_height", sa.Integer(), nullable=True))
    op.add_column(
        "media_files", sa.Column("thumbnail_status", sa.String(16), nullable=True)
    )
    op.add_column(
        "media_files",
        sa.Column("thumbnail_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "media_files", sa.Column("thumbnail_error", sa.Text(), nullable=True)
    )
    op.create_index(
        "ix_media_files_thumbnail_status", "media_files", ["thumbnail_status"]
    )


def downgrade() -> None:
    op.drop_index("ix_media_files_thumbnail_status", table_name="media_files")
    op.drop_column("media_files", "thumbnail_error")
    op.drop_column("media_files", "thumbnail_attempted_at")
    op.drop_column("media_files", "thumbnail_status")
    op.drop_column("media_files", "image_height")
    op.drop_column("media_files", "image_width")
    op.drop_column("media_files", "thumbnail_ref")
