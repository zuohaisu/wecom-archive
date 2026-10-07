"""Retire the persisted tenant activity compatibility projection.

Revision ID: 0075
Revises: 0074
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: Union[str, None] = "0074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EXPECTED_PROJECTION = sa.text(
    "SELECT 1 FROM tenants "
    "WHERE is_active <> CASE WHEN lifecycle_status = 'active' "
    "THEN true ELSE false END LIMIT 1"
)


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Tenant.is_active retirement requires an online invariant check"
        )

    # Do not repair from the boolean or silently change service access. An
    # unexpected row blocks the schema change for explicit operator review.
    if op.get_bind().execute(_EXPECTED_PROJECTION).first() is not None:
        raise RuntimeError(
            "Tenant.is_active does not match lifecycle_status; refusing to drop it"
        )
    op.drop_column("tenants", "is_active")


def downgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE tenants SET is_active = "
            "CASE WHEN lifecycle_status = 'active' THEN true ELSE false END"
        )
    )
