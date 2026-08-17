"""Add ai_handoff triage columns and ai_eval_runs table (RND-359 / T5).

Revision ID: 0062
Revises: 0061
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0062"
down_revision: Union[str, None] = "0061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("ai_handoff") as batch_op:
        batch_op.add_column(sa.Column("reason", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("resolution_category", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("resolved_by", sa.String(length=255), nullable=True))

    op.create_table(
        "ai_eval_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dataset_version", sa.String(length=32), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("triggered_by", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ai_eval_runs"),
    )


def downgrade() -> None:
    op.drop_table("ai_eval_runs")

    with op.batch_alter_table("ai_handoff") as batch_op:
        batch_op.drop_column("resolved_by")
        batch_op.drop_column("resolved_at")
        batch_op.drop_column("resolution_category")
        batch_op.drop_column("reason")
