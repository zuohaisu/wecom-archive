"""Add soft-delete tombstones and deletion hold (RND-362).

Revision ID: 0068
Revises: 0067
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0068"
down_revision: Union[str, None] = "0067"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("deletion_locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("archive_messages", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("archive_messages", sa.Column("deleted_by_admin_user_id", sa.String(length=36), nullable=True))
    op.add_column("archive_messages", sa.Column("delete_reason", sa.String(length=200), nullable=True))
    op.add_column("archive_messages", sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("archive_messages", sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("archive_messages", sa.Column("restored_by_admin_user_id", sa.String(length=36), nullable=True))
    op.add_column("archive_messages", sa.Column("deletion_batch_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_archive_messages_deleted_by_admin_user",
        "archive_messages", "admin_users", ["deleted_by_admin_user_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_archive_messages_restored_by_admin_user",
        "archive_messages", "admin_users", ["restored_by_admin_user_id"], ["id"],
    )
    op.create_index(
        "ix_archive_messages_tenant_deleted_at", "archive_messages", ["tenant_id", "deleted_at"]
    )
    op.create_index("ix_archive_messages_deletion_batch_id", "archive_messages", ["deletion_batch_id"])


def downgrade() -> None:
    op.drop_index("ix_archive_messages_deletion_batch_id", table_name="archive_messages")
    op.drop_index("ix_archive_messages_tenant_deleted_at", table_name="archive_messages")
    op.drop_constraint("fk_archive_messages_restored_by_admin_user", "archive_messages", type_="foreignkey")
    op.drop_constraint("fk_archive_messages_deleted_by_admin_user", "archive_messages", type_="foreignkey")
    for name in (
        "deletion_batch_id", "restored_by_admin_user_id", "restored_at", "purge_after",
        "delete_reason", "deleted_by_admin_user_id", "deleted_at",
    ):
        op.drop_column("archive_messages", name)
    op.drop_column("tenants", "deletion_locked")
