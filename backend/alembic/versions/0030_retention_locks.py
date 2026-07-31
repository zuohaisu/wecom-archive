"""Create independent expired-message retention locks (RND-319).

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "retention_locks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("archive_message_id", sa.Integer(), nullable=False),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["archive_message_id"], ["archive_messages.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "archive_message_id", name="uq_retention_locks_archive_message_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("retention_locks")
