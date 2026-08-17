"""Add controlled avatar-cache metadata for archive contacts (RND-371).

Revision ID: 0064
Revises: 0063
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: Union[str, None] = "0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def _avatar_columns() -> tuple[sa.Column, ...]:
    """Return fresh Column instances for each altered table."""
    return (
        sa.Column("avatar_storage_backend", sa.String(length=32), nullable=True),
        sa.Column("avatar_storage_ref", sa.Text(), nullable=True),
        sa.Column("avatar_content_type", sa.String(length=32), nullable=True),
        sa.Column("avatar_source", sa.String(length=32), nullable=True),
        sa.Column("avatar_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("avatar_status", sa.String(length=16), nullable=True),
    )


def upgrade() -> None:
    for table_name in ("contacts", "external_contacts"):
        with op.batch_alter_table(table_name) as batch_op:
            for column in _avatar_columns():
                batch_op.add_column(column)


def downgrade() -> None:
    for table_name in ("external_contacts", "contacts"):
        with op.batch_alter_table(table_name) as batch_op:
            for column_name in reversed(
                (
                    "avatar_storage_backend",
                    "avatar_storage_ref",
                    "avatar_content_type",
                    "avatar_source",
                    "avatar_synced_at",
                    "avatar_status",
                )
            ):
                batch_op.drop_column(column_name)
