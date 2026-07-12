"""Local -> Qiniu media migration bookkeeping (RND-186).

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-12

RND-186 adds a repeatable, resumable scripts/migrate_local_media_to_qiniu.py
tool that copies media_files rows currently served by LocalStorageProvider
(storage_backend="local") to QiniuStorageProvider, flipping
storage_backend/storage_ref (migration 0005's columns) once a row's bytes
are confirmed uploaded.

"Already migrated" is fully answered by storage_backend alone (a migrated
row's storage_backend is "qiniu_kodo", so it is naturally excluded from any
future local->Qiniu candidate scan) -- no new column is needed for that.
What storage_backend alone cannot represent is "this row was attempted and
failed" versus "this row was never attempted", because a failed migration
must leave storage_backend/storage_ref completely untouched (still "local",
still fully servable) rather than flipping to some half-migrated state.
This migration adds three columns purely for that bookkeeping, never read
by any existing serving/timeline code path:

    migration_status       -- NULL (never attempted) | "migrated" | "failed"
    migration_attempted_at -- timestamp of the most recent attempt
    migration_error        -- short, sanitized diagnostic tag (never a raw
                               path, sdkfileid, or exception string)

Migration is additive only -- no backfill, no data rewrite, no destructive
change. Every existing row gets migration_status=NULL (the SQL default for
a new nullable column), which is exactly "never attempted".
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_files", sa.Column("migration_status", sa.String(16), nullable=True)
    )
    op.add_column(
        "media_files",
        sa.Column("migration_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("media_files", sa.Column("migration_error", sa.Text(), nullable=True))
    op.create_index(
        "ix_media_files_migration_status", "media_files", ["migration_status"]
    )


def downgrade() -> None:
    op.drop_index("ix_media_files_migration_status", table_name="media_files")
    op.drop_column("media_files", "migration_error")
    op.drop_column("media_files", "migration_attempted_at")
    op.drop_column("media_files", "migration_status")
