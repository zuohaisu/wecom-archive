"""Add export approval tokens (RND-316 C2-2).

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "export_approval_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("admin_user_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("params_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_export_approval_tokens_tenant_id",
        "export_approval_tokens",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_export_approval_tokens_admin_user_id",
        "export_approval_tokens",
        ["admin_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_export_approval_tokens_params_hash",
        "export_approval_tokens",
        ["params_hash"],
        unique=False,
    )
    op.create_index(
        "ix_export_approval_tokens_token",
        "export_approval_tokens",
        ["token"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_export_approval_tokens_token", table_name="export_approval_tokens")
    op.drop_index("ix_export_approval_tokens_params_hash", table_name="export_approval_tokens")
    op.drop_index("ix_export_approval_tokens_admin_user_id", table_name="export_approval_tokens")
    op.drop_index("ix_export_approval_tokens_tenant_id", table_name="export_approval_tokens")
    op.drop_table("export_approval_tokens")
