"""Add tenant-scoped message/media favorites (RND-366).

Revision ID: 0074
Revises: 0073
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: Union[str, None] = "0073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The composite favorite FK proves that the selected MediaFile belongs to
    # the declared tenant. ``id`` is already globally unique, so this adds no
    # new data-level restriction beyond making that tenant proof referencable.
    with op.batch_alter_table("media_files") as batch:
        batch.create_unique_constraint(
            "uq_media_files_tenant_id_id", ["tenant_id", "id"]
        )

    op.create_table(
        "archive_favorites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("object_type", sa.String(length=16), nullable=False),
        sa.Column("archive_message_id", sa.BigInteger(), nullable=True),
        sa.Column("media_file_id", sa.Integer(), nullable=True),
        sa.Column("favorited_by_admin_user_id", sa.String(length=36), nullable=True),
        sa.Column(
            "favorited_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_by_admin_user_id", sa.String(length=36), nullable=True),
        sa.Column("source_page", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_archive_favorites_tenant", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "archive_message_id"],
            ["archive_messages.tenant_id", "archive_messages.id"],
            name="fk_archive_favorites_tenant_message",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "media_file_id"],
            ["media_files.tenant_id", "media_files.id"],
            name="fk_archive_favorites_tenant_media",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["favorited_by_admin_user_id"], ["admin_users.id"],
            name="fk_archive_favorites_favorited_by_admin_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["canceled_by_admin_user_id"], ["admin_users.id"],
            name="fk_archive_favorites_canceled_by_admin_user",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "archive_message_id",
            name="uq_archive_favorites_tenant_message",
        ),
        sa.UniqueConstraint(
            "tenant_id", "media_file_id",
            name="uq_archive_favorites_tenant_media",
        ),
        sa.CheckConstraint(
            "(object_type = 'message' AND archive_message_id IS NOT NULL AND media_file_id IS NULL) "
            "OR (object_type = 'media' AND archive_message_id IS NULL AND media_file_id IS NOT NULL)",
            name="ck_archive_favorites_object_target",
        ),
        sa.CheckConstraint(
            "source_page IS NULL OR source_page IN ('messages', 'media', 'favorites')",
            name="ck_archive_favorites_source_page",
        ),
    )
    op.create_index(
        "ix_archive_favorites_tenant_active_time",
        "archive_favorites",
        ["tenant_id", "canceled_at", "favorited_at", "id"],
    )
    op.create_index(
        "ix_archive_favorites_tenant_active_actor",
        "archive_favorites",
        ["tenant_id", "canceled_at", "favorited_by_admin_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_archive_favorites_tenant_active_actor", table_name="archive_favorites"
    )
    op.drop_index(
        "ix_archive_favorites_tenant_active_time", table_name="archive_favorites"
    )
    op.drop_table("archive_favorites")
    with op.batch_alter_table("media_files") as batch:
        batch.drop_constraint("uq_media_files_tenant_id_id", type_="unique")
