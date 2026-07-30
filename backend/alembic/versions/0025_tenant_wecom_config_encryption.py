"""Add encrypted tenant WeCom RSA private-key storage (RND-311).

Revision ID: 0025
Revises: 0024
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenant_wecom_configs",
        sa.Column("private_key_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_wecom_configs", "private_key_encrypted")
