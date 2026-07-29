"""Enforce downloaded-media sizes and add daily tenant storage rollups (RND-331).

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK_NAME = "ck_media_files_downloaded_requires_file_size"
_CHECK_SQL = "download_status != 'downloaded' OR file_size IS NOT NULL"


def _fail_if_downloaded_sizes_missing() -> None:
    missing = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM media_files "
            "WHERE download_status = 'downloaded' AND file_size IS NULL"
        )
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"RND-331 migration blocked: {missing} downloaded media_files rows have "
            "NULL file_size. Run scripts/backfill_media_file_size_once.py and retry."
        )


def _create_downloaded_size_check() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot ADD CONSTRAINT; batch mode rebuilds the table while
        # preserving its existing rows and is also what Alembic recommends.
        with op.batch_alter_table("media_files", recreate="always") as batch_op:
            batch_op.create_check_constraint(_CHECK_NAME, _CHECK_SQL)
    else:
        op.create_check_constraint(_CHECK_NAME, "media_files", _CHECK_SQL)


def _drop_downloaded_size_check() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("media_files", recreate="always") as batch_op:
            batch_op.drop_constraint(_CHECK_NAME, type_="check")
    else:
        op.drop_constraint(_CHECK_NAME, "media_files", type_="check")


def upgrade() -> None:
    _fail_if_downloaded_sizes_missing()
    _create_downloaded_size_check()
    op.create_table(
        "tenant_storage_daily",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "usage_date", name="uq_tenant_storage_daily_tenant_date"),
    )
    op.create_index("ix_tenant_storage_daily_tenant_id", "tenant_storage_daily", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_storage_daily_tenant_id", table_name="tenant_storage_daily")
    op.drop_table("tenant_storage_daily")
    _drop_downloaded_size_check()
