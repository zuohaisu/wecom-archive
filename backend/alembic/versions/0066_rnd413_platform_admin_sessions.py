"""Add platform-admin login sessions (RND-413).

Revision ID: 0066
Revises: 0065
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: Union[str, None] = "0065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_admin_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("platform_admin_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_revoked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["platform_admin_id"],
            ["platform_admins.id"],
            name="fk_platform_admin_sessions_platform_admin_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_admin_sessions_expires_at",
        "platform_admin_sessions",
        ["expires_at"],
    )
    op.create_index(
        "ix_platform_admin_sessions_platform_admin_id",
        "platform_admin_sessions",
        ["platform_admin_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_admin_sessions_platform_admin_id",
        table_name="platform_admin_sessions",
    )
    op.drop_index(
        "ix_platform_admin_sessions_expires_at",
        table_name="platform_admin_sessions",
    )
    op.drop_table("platform_admin_sessions")
