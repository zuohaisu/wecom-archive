"""Persist durable quota-blocked media download facts.

Revision ID: 0044
Revises: 0043
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "media_quota_blocks",
        sa.Column("media_file_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("observed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "observed_bytes > 0",
            name="ck_media_quota_blocks_bytes",
        ),
        sa.CheckConstraint(
            "reason IN ('quota_exceeded', 'subscription_inactive', "
            "'usage_unavailable')",
            name="ck_media_quota_blocks_reason",
        ),
        sa.ForeignKeyConstraint(
            ["media_file_id"],
            ["media_files.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("media_file_id"),
    )
    op.create_index(
        "ix_media_quota_blocks_tenant_blocked",
        "media_quota_blocks",
        ["tenant_id", "blocked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_quota_blocks_tenant_blocked",
        table_name="media_quota_blocks",
    )
    op.drop_table("media_quota_blocks")
