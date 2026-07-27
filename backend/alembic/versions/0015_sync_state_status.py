"""Archive sync status and console refresh version (RND-211).

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYNC_STATUS = sa.Enum("idle", "syncing", "error", name="sync_state_status")


def upgrade() -> None:
    # Create the PostgreSQL enum explicitly because add_column() does not
    # create a native enum type as create_table() would.
    _SYNC_STATUS.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "sync_states",
        sa.Column(
            "status",
            _SYNC_STATUS,
            nullable=False,
            server_default=sa.text("'idle'"),
        ),
    )
    op.add_column(
        "sync_states", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("sync_states", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column(
        "sync_states",
        sa.Column(
            "seq_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Defaults above backfill existing rows safely; ORM defaults own future
    # inserts, consistent with the rest of the model's cursor fields.
    op.alter_column("sync_states", "status", server_default=None)
    op.alter_column("sync_states", "seq_version", server_default=None)


def downgrade() -> None:
    op.drop_column("sync_states", "seq_version")
    op.drop_column("sync_states", "error_message")
    op.drop_column("sync_states", "started_at")
    op.drop_column("sync_states", "status")
    _SYNC_STATUS.drop(op.get_bind(), checkfirst=True)
