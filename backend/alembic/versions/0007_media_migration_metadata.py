"""Full storage metadata persistence for migrated media (RND-186 QA fix).

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-13

QA review of the first RND-186 implementation found it relied on
storage_ref (the object key string) as the only migration-time metadata,
and never recomputed file_size/mime_type/checksum at migration time —
file_size was simply left at whatever the original local download recorded.
This migration adds the missing first-class columns so a migrated row's
full storage metadata is queryable without parsing/inferring anything from
storage_ref:

    bucket           -- the object-storage bucket the bytes actually live
                        in (NULL for storage_backend="local", which has no
                        bucket concept; populated from the provider that
                        performed the upload, e.g. QiniuStorageProvider.bucket
                        -- never re-derived independently from config, so it
                        can never drift from what was actually used)
    mime_type        -- content-sniffed (never extension-inferred) MIME
                        type, e.g. "image/jpeg", "video/mp4", "audio/amr"
    checksum_sha256  -- hex-encoded SHA-256 of the exact bytes uploaded

storage_ref (migration 0005) remains the authoritative object key / local
path reference -- deliberately NOT duplicated into a second "object_key"
column: two columns holding the same value would be a drift risk with no
offsetting benefit, since every existing read path (the media route, the
RND-187 signed-URL route, this migration tool) already treats storage_ref
as that authoritative reference.

Migration is additive only -- no backfill, no destructive change, no
rewrite of any existing column. Every existing row (including any row
already migrated under the pre-fix version of
scripts/migrate_local_media_to_qiniu.py) gets bucket/mime_type/
checksum_sha256 = NULL, meaning "not recorded by this tool version" --
compatible with old data; a --retry run only reconsiders rows with
migration_status="failed", so an already-"migrated" row is not
retroactively backfilled by this migration alone (see RND-186 fix report,
"Remaining Limitations").
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("media_files", sa.Column("bucket", sa.String(128), nullable=True))
    op.add_column("media_files", sa.Column("mime_type", sa.String(128), nullable=True))
    op.add_column(
        "media_files", sa.Column("checksum_sha256", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("media_files", "checksum_sha256")
    op.drop_column("media_files", "mime_type")
    op.drop_column("media_files", "bucket")
