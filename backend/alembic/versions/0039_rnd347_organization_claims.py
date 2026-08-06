"""Add minimal organization creation claims.

Revision ID: 0039
Revises: 0038
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wecom_organization_claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("public_ref_hash", sa.String(length=64), nullable=False),
        sa.Column("corp_id", sa.String(length=64), nullable=False),
        sa.Column("corp_name", sa.String(length=255), nullable=False),
        sa.Column("authorized_subject", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("permanent_code_encrypted", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_ref_hash"),
    )
    op.create_index("ix_wecom_organization_claims_expires_at", "wecom_organization_claims", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_wecom_organization_claims_expires_at", table_name="wecom_organization_claims")
    op.drop_table("wecom_organization_claims")
