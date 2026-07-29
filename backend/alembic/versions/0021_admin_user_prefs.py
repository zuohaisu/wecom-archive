"""Add UI theme/locale preferences to admin_users (RND-297 A8-2).

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column("ui_theme", sa.String(length=16), nullable=False, server_default="light"),
    )
    op.add_column(
        "admin_users",
        sa.Column("ui_locale", sa.String(length=16), nullable=False, server_default="zh-CN"),
    )


def downgrade() -> None:
    op.drop_column("admin_users", "ui_locale")
    op.drop_column("admin_users", "ui_theme")
