"""Add permanent-purge retry state and purge audit actions (RND-364).

Revision ID: 0069
Revises: 0068
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: Union[str, None] = "0068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "media_purge_retries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_file_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("storage_backend", sa.String(length=32), nullable=True),
        sa.Column("storage_ref", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_media_purge_retries_next_retry_at", "media_purge_retries", ["next_retry_at"]
    )
    op.create_index("ix_media_purge_retries_tenant", "media_purge_retries", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_media_purge_retries_tenant", table_name="media_purge_retries")
    op.drop_index("ix_media_purge_retries_next_retry_at", table_name="media_purge_retries")
    op.drop_table("media_purge_retries")
