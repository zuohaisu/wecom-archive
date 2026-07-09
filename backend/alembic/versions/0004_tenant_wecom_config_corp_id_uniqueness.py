"""Tenant WeCom config corp_id uniqueness (RND-184).

Adds a partial unique index on tenant_wecom_configs.corp_id, scoped to
active (is_active = true) rows, so a given WeCom corp_id can resolve to
exactly one active tenant. Auth and sync both resolve tenant context by
corp_id (WHERE is_active = true) — an unconstrained corp_id allowed two
tenants to collide and made resolution ambiguous.

Inactive/disabled configs are exempt from the constraint so a corp_id can
be freely reassigned once its old config is deactivated.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-09

Migration is additive only. If duplicate active corp_id rows already exist,
upgrade() fails loudly with a RuntimeError listing the offending corp_id(s)
instead of silently deactivating or picking a winner — an operator must
resolve the duplicates (deactivate or reassign one of the conflicting rows)
before this migration can proceed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    duplicates = connection.execute(
        sa.text(
            """
            SELECT corp_id, COUNT(*) AS cnt
            FROM tenant_wecom_configs
            WHERE is_active = true
            GROUP BY corp_id
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()

    if duplicates:
        corp_ids = ", ".join(row[0] for row in duplicates)
        raise RuntimeError(
            "Cannot apply migration 0004: multiple active "
            "tenant_wecom_configs rows share the same corp_id "
            f"({corp_ids}). Deactivate or reassign the conflicting rows "
            "before re-running this migration."
        )

    op.create_index(
        "uq_tenant_wecom_configs_active_corp_id",
        "tenant_wecom_configs",
        ["corp_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_tenant_wecom_configs_active_corp_id",
        table_name="tenant_wecom_configs",
    )
