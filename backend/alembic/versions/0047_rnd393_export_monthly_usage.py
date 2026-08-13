"""Add server-authoritative monthly export usage counters.

Revision ID: 0047
Revises: 0046
"""

from alembic import op
import sqlalchemy as sa


revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "export_monthly_usage",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("export_type", sa.String(length=24), nullable=False),
        sa.Column("used_count", sa.Integer(), server_default="0", nullable=False),
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
            "export_type IN ('text', 'media_zip')",
            name="ck_export_monthly_usage_type",
        ),
        sa.CheckConstraint("used_count >= 0", name="ck_export_monthly_usage_count"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "period_start",
            "export_type",
            name="uq_export_monthly_usage_period_type",
        ),
    )
    op.create_index(
        "ix_export_monthly_usage_tenant_period",
        "export_monthly_usage",
        ["tenant_id", "period_start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_export_monthly_usage_tenant_period",
        table_name="export_monthly_usage",
    )
    op.drop_table("export_monthly_usage")
