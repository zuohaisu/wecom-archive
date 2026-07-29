"""Create tenant-scoped WeCom external contacts (RND-287).

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "external_contacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_userid", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("owner_wecom_userid", sa.String(length=64), nullable=True),
        sa.Column("last_interaction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "external_userid", name="uq_external_contacts_tenant_ext_userid"
        ),
    )
    op.create_index("ix_external_contacts_tenant_id", "external_contacts", ["tenant_id"])
    op.execute(
        "CREATE INDEX ix_external_contacts_name_trgm "
        "ON external_contacts USING gin (name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_external_contacts_company_trgm "
        "ON external_contacts USING gin (company gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_external_contacts_company_trgm")
    op.execute("DROP INDEX IF EXISTS ix_external_contacts_name_trgm")
    op.drop_index("ix_external_contacts_tenant_id", table_name="external_contacts")
    op.drop_table("external_contacts")
