"""Media tenant scoping: add tenant_id to media_files, replace the global
UNIQUE(sdkfileid) constraint with UNIQUE(tenant_id, sdkfileid).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-09

Migration is additive/renaming only — no row data is modified. Run
bootstrap_default_tenant.py after this migration to backfill
media_files.tenant_id for existing rows (same pattern as 0002).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_files", sa.Column("tenant_id", sa.String(36), nullable=True)
    )
    op.create_foreign_key(
        "fk_media_files_tenant_id",
        "media_files",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.create_index("ix_media_files_tenant_id", "media_files", ["tenant_id"])

    # media_files: UNIQUE(sdkfileid) → UNIQUE(tenant_id, sdkfileid)
    op.drop_constraint("uq_media_files_sdkfileid", "media_files", type_="unique")
    op.create_unique_constraint(
        "uq_media_files_tenant_sdkfileid",
        "media_files",
        ["tenant_id", "sdkfileid"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_media_files_tenant_sdkfileid", "media_files", type_="unique"
    )
    op.create_unique_constraint(
        "uq_media_files_sdkfileid", "media_files", ["sdkfileid"]
    )

    op.drop_index("ix_media_files_tenant_id", table_name="media_files")
    op.drop_constraint("fk_media_files_tenant_id", "media_files", type_="foreignkey")
    op.drop_column("media_files", "tenant_id")
