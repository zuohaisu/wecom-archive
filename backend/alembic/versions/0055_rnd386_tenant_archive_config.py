"""Add per-tenant archive callback credentials and public key version.

Revision ID: 0055
Revises: 0054
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_wecom_configs") as batch_op:
        # RND-386 (T1): customer-provided session-archive callback credentials,
        # Fernet ciphertext at rest. Nullable — the legacy single-corp
        # deployment keeps using env-scoped WECOM_CALLBACK_TOKEN/AESKey.
        batch_op.add_column(sa.Column("callback_token_encrypted", sa.Text()))
        batch_op.add_column(
            sa.Column("callback_encoding_aes_key_encrypted", sa.Text())
        )
        batch_op.add_column(sa.Column("publickey_version", sa.Integer()))


def downgrade() -> None:
    with op.batch_alter_table("tenant_wecom_configs") as batch_op:
        batch_op.drop_column("publickey_version")
        batch_op.drop_column("callback_encoding_aes_key_encrypted")
        batch_op.drop_column("callback_token_encrypted")
