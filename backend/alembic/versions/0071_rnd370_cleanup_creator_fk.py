"""Add missing cleanup-task creator foreign key (RND-370 QA fix).

Revision ID: 0071
Revises: 0070
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: Union[str, None] = "0070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "fk_message_cleanup_tasks_created_by_admin_user"


def upgrade() -> None:
    op.create_foreign_key(
        _CONSTRAINT,
        "message_cleanup_tasks",
        "admin_users",
        ["created_by_admin_user_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "message_cleanup_tasks", type_="foreignkey")
