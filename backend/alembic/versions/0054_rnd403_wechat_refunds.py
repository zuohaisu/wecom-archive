"""Persist WeChat refund identity and original payment references.

Revision ID: 0054
Revises: 0053
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("refund_orders") as batch_op:
        batch_op.add_column(sa.Column("provider_refund_id", sa.String(length=64)))
        batch_op.create_unique_constraint(
            "uq_refund_orders_provider_refund_id",
            ["provider", "provider_refund_id"],
        )
    with op.batch_alter_table("refund_events") as batch_op:
        # Nullable only for pre-0054 evidence. Every newly accepted provider
        # event is application-validated and writes all three references.
        batch_op.add_column(sa.Column("provider_refund_id", sa.String(length=64)))
        batch_op.add_column(sa.Column("provider_order_ref", sa.String(length=64)))
        batch_op.add_column(
            sa.Column("provider_transaction_id", sa.String(length=64))
        )


def downgrade() -> None:
    with op.batch_alter_table("refund_events") as batch_op:
        batch_op.drop_column("provider_transaction_id")
        batch_op.drop_column("provider_order_ref")
        batch_op.drop_column("provider_refund_id")
    with op.batch_alter_table("refund_orders") as batch_op:
        batch_op.drop_constraint(
            "uq_refund_orders_provider_refund_id", type_="unique"
        )
        batch_op.drop_column("provider_refund_id")
