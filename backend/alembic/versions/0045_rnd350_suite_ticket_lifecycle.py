"""Add encrypted WeCom third-party suite_ticket authority.

Revision ID: 0045
Revises: 0044
"""

from alembic import op
import sqlalchemy as sa


revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wecom_suite_ticket_states",
        sa.Column("suite_id", sa.String(length=128), nullable=False),
        sa.Column("ticket_encrypted", sa.Text(), nullable=False),
        sa.Column("ticket_digest", sa.String(length=64), nullable=False),
        sa.Column("source_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_timestamp > 0",
            name="ck_wecom_suite_ticket_source_timestamp_positive",
        ),
        sa.PrimaryKeyConstraint("suite_id"),
    )


def downgrade() -> None:
    op.drop_table("wecom_suite_ticket_states")
