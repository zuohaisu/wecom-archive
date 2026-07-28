"""Add AdminUser account-system fields (RND-277 F0-1).

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE_ENUM = sa.Enum(
    "owner",
    "admin",
    "compliance",
    "legal",
    "readonlyaudit",
    name="admin_user_role",
)
_STATUS_ENUM = sa.Enum("active", "disabled", name="admin_user_status")


def upgrade() -> None:
    # add_column() does not create PostgreSQL native enum types itself.
    _ROLE_ENUM.create(op.get_bind(), checkfirst=True)
    _STATUS_ENUM.create(op.get_bind(), checkfirst=True)

    op.add_column("admin_users", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column(
        "admin_users",
        sa.Column(
            "role",
            _ROLE_ENUM,
            nullable=False,
            server_default=sa.text("'admin'"),
        ),
    )
    op.add_column(
        "admin_users",
        sa.Column(
            "status",
            _STATUS_ENUM,
            nullable=False,
            server_default=sa.text("'active'"),
        ),
    )
    op.add_column("admin_users", sa.Column("email", sa.Text(), nullable=True))
    op.add_column("admin_users", sa.Column("phone", sa.Text(), nullable=True))
    op.add_column("admin_users", sa.Column("department", sa.Text(), nullable=True))
    op.add_column(
        "admin_users", sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("admin_users", sa.Column("invite_token", sa.Text(), nullable=True))
    op.add_column(
        "admin_users",
        sa.Column(
            "invited_by",
            sa.String(length=36),
            sa.ForeignKey("admin_users.id"),
            nullable=True,
        ),
    )
    op.add_column("admin_users", sa.Column("invite_status", sa.Text(), nullable=True))
    op.create_index("ix_admin_users_invite_token", "admin_users", ["invite_token"])


def downgrade() -> None:
    op.drop_index("ix_admin_users_invite_token", table_name="admin_users")
    op.drop_column("admin_users", "invite_status")
    op.drop_column("admin_users", "invited_by")
    op.drop_column("admin_users", "invite_token")
    op.drop_column("admin_users", "last_active_at")
    op.drop_column("admin_users", "department")
    op.drop_column("admin_users", "phone")
    op.drop_column("admin_users", "email")
    op.drop_column("admin_users", "status")
    op.drop_column("admin_users", "role")
    op.drop_column("admin_users", "password_hash")
    _STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
    _ROLE_ENUM.drop(op.get_bind(), checkfirst=True)
