"""Add tenant-scoped current WeCom group-chat metadata.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "group_chat_metadata",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("roomid", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=False,
            server_default="wecom_external_groupchat",
        ),
        sa.Column("sync_status", sa.String(length=32), nullable=False, server_default="unresolved"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "roomid", name="uq_group_chat_metadata_tenant_roomid"),
    )
    op.create_index(
        "ix_group_chat_metadata_tenant_id", "group_chat_metadata", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_group_chat_metadata_tenant_roomid",
        "group_chat_metadata",
        ["tenant_id", "roomid"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_group_chat_metadata_tenant_roomid", table_name="group_chat_metadata")
    op.drop_index("ix_group_chat_metadata_tenant_id", table_name="group_chat_metadata")
    op.drop_table("group_chat_metadata")
