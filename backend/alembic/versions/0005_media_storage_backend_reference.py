"""Media storage backend/reference per row (RND-174 QA remediation).

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-10

QA found that reusing media_files.local_path both as a local filesystem
path AND as a Qiniu object key was ambiguous and unsafe once Qiniu became a
second possible backend: nothing on the row itself said which provider
actually held its bytes, so serving a row correctly depended entirely on
the deployment-wide MEDIA_STORAGE_PROVIDER setting matching whatever
backend originally wrote it — switching that setting (e.g. enabling Qiniu
for new writes) would silently reinterpret every existing local row as a
Qiniu object key, or vice versa.

This migration adds an explicit, per-row discriminator:

    storage_backend  — "local" or "qiniu_kodo"
    storage_ref      — that provider's own reference (a local path for
                        "local", a Qiniu object key for "qiniu_kodo")

Backfill (part of this migration, not a separate script — unlike the
tenant_id backfill pattern used by 0002/0003): every existing row is
stamped storage_backend='local', storage_ref=local_path. This is safe
because Qiniu did not exist as a selectable provider before RND-174, so
every historical row was necessarily written by LocalStorageProvider.
local_path itself is left untouched (not dropped, not overwritten) — it
remains a legacy/local-only compatibility column; new code must not treat
it as an authoritative Qiniu reference. No historical local media is
migrated to Qiniu by this migration (RND-186 remains a separate,
not-yet-implemented ticket).

Migration is additive plus one data backfill UPDATE — no destructive
changes, no row deletion, no rewrite of local_path.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_files", sa.Column("storage_backend", sa.String(32), nullable=True)
    )
    op.add_column("media_files", sa.Column("storage_ref", sa.Text(), nullable=True))
    op.create_index(
        "ix_media_files_storage_backend", "media_files", ["storage_backend"]
    )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE media_files
            SET storage_backend = 'local', storage_ref = local_path
            WHERE storage_backend IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_media_files_storage_backend", table_name="media_files")
    op.drop_column("media_files", "storage_ref")
    op.drop_column("media_files", "storage_backend")
