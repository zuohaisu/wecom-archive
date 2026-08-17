"""Add persisted per-tenant activation-check state (RND-388).

Revision ID: 0056
Revises: 0055
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_activation_checks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column(
            "tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("gate_results", JSONB, nullable=False),
        sa.Column("safe_error_code", sa.String(64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('not_started', 'blocked', 'ready')",
            name="ck_tenant_activation_checks_state",
        ),
        sa.UniqueConstraint(
            "tenant_id", name="uq_tenant_activation_checks_tenant"
        ),
    )


def downgrade() -> None:
    op.drop_table("tenant_activation_checks")
