"""Add PlatformAdmin entity (RND-306 B1-1).

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# create_type=False: unlike add_column(), create_table() DOES create native
# enum types for its columns automatically, which would duplicate the
# explicit checkfirst creation below.
_ROLE_ENUM = postgresql.ENUM(
    "superadmin", name="platform_admin_role", create_type=False
)
_STATUS_ENUM = postgresql.ENUM(
    "active", "disabled", name="platform_admin_status", create_type=False
)


def upgrade() -> None:
    # Created explicitly (idempotently) so a partially-applied run can retry.
    _ROLE_ENUM.create(op.get_bind(), checkfirst=True)
    _STATUS_ENUM.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "platform_admins",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "role",
            _ROLE_ENUM,
            nullable=False,
            server_default=sa.text("'superadmin'"),
        ),
        sa.Column(
            "status",
            _STATUS_ENUM,
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_platform_admins_email"),
    )
    op.create_index(
        "ix_platform_admins_status", "platform_admins", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_platform_admins_status", table_name="platform_admins")
    op.drop_table("platform_admins")
    _STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
    _ROLE_ENUM.drop(op.get_bind(), checkfirst=True)
