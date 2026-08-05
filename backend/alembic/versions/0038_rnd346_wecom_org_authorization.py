"""Add isolated third-party WeCom organization authorization evidence.

Revision ID: 0038
Revises: 0037
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wecom_authorization_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index("ix_wecom_authorization_attempts_expires_at", "wecom_authorization_attempts", ["expires_at"])
    op.create_table(
        "wecom_authorization_proofs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("browser_token_hash", sa.String(length=64), nullable=False),
        sa.Column("corp_id", sa.String(length=64), nullable=False),
        sa.Column("corp_name", sa.String(length=255), nullable=False),
        sa.Column("authorized_subject", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("authorization_mode", sa.String(length=16), nullable=False),
        sa.Column("permanent_code_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("authorization_mode = 'admin'", name="ck_wecom_auth_proof_admin_mode"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("browser_token_hash"),
    )
    op.create_index("ix_wecom_authorization_proofs_expires_at", "wecom_authorization_proofs", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_wecom_authorization_proofs_expires_at", table_name="wecom_authorization_proofs")
    op.drop_table("wecom_authorization_proofs")
    op.drop_index("ix_wecom_authorization_attempts_expires_at", table_name="wecom_authorization_attempts")
    op.drop_table("wecom_authorization_attempts")
