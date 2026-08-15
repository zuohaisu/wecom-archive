"""Persist Qiniu mkzip operation state for asynchronous media exports.

Revision ID: 0050
Revises: 0049
"""

from alembic import op
import sqlalchemy as sa


revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("export_jobs") as batch_op:
        batch_op.add_column(sa.Column("provider_operation_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("provider_index_ref", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("provider_manifest_ref", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("export_jobs") as batch_op:
        batch_op.drop_column("provider_manifest_ref")
        batch_op.drop_column("provider_index_ref")
        batch_op.drop_column("provider_operation_id")
