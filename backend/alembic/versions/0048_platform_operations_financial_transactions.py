"""Add platform-recorded manual receipt and refund entries.

Revision ID: 0048
Revises: 0047
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "manual_financial_transactions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default=sa.text("'CNY'"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference", sa.String(length=128), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recorded_by_platform_admin_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('receipt', 'refund')",
            name="ck_manual_financial_transactions_kind",
        ),
        sa.CheckConstraint(
            "amount_cents > 0",
            name="ck_manual_financial_transactions_amount",
        ),
        sa.CheckConstraint(
            "length(currency) = 3",
            name="ck_manual_financial_transactions_currency",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["recorded_by_platform_admin_id"], ["platform_admins.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_manual_financial_transactions_tenant_occurred",
        "manual_financial_transactions",
        ["tenant_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_manual_financial_transactions_tenant_occurred",
        table_name="manual_financial_transactions",
    )
    op.drop_table("manual_financial_transactions")
