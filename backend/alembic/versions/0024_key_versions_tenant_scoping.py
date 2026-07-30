"""Tenant-scope WeCom key versions (RND-332).

Revision ID: 0024
Revises: 0023
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_UNIQUE = "uq_key_versions_publickey_ver"
_NEW_UNIQUE = "uq_key_versions_tenant_publickey_ver"
_FK = "fk_key_versions_tenant_id"
_DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _backfill_existing_rows() -> None:
    """Assign legacy keys only to the well-known single-tenant bootstrap row."""
    bind = op.get_bind()
    legacy_count = bind.execute(sa.text("SELECT COUNT(*) FROM key_versions")).scalar_one()
    if not legacy_count:
        return
    default_exists = bind.execute(
        sa.text("SELECT COUNT(*) FROM tenants WHERE id = :tenant_id"),
        {"tenant_id": _DEFAULT_TENANT_ID},
    ).scalar_one()
    if not default_exists:
        raise RuntimeError(
            "RND-332 migration blocked: key_versions contains legacy rows but the "
            "default tenant is absent; assign their tenant ownership manually."
        )
    bind.execute(
        sa.text("UPDATE key_versions SET tenant_id = :tenant_id WHERE tenant_id IS NULL"),
        {"tenant_id": _DEFAULT_TENANT_ID},
    )


def upgrade() -> None:
    # Nullable first permits an empty table and safely backfills only the known
    # bootstrap tenant. Never guess if a non-default production tenant owns keys.
    op.add_column("key_versions", sa.Column("tenant_id", sa.String(length=36), nullable=True))
    _backfill_existing_rows()

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("key_versions", recreate="always") as batch_op:
            batch_op.alter_column("tenant_id", nullable=False)
            batch_op.create_foreign_key(_FK, "tenants", ["tenant_id"], ["id"])
            batch_op.drop_constraint(_OLD_UNIQUE, type_="unique")
            batch_op.create_unique_constraint(_NEW_UNIQUE, ["tenant_id", "publickey_ver"])
    else:
        op.alter_column("key_versions", "tenant_id", nullable=False)
        op.create_foreign_key(_FK, "key_versions", "tenants", ["tenant_id"], ["id"])
        op.drop_constraint(_OLD_UNIQUE, "key_versions", type_="unique")
        op.create_unique_constraint(_NEW_UNIQUE, "key_versions", ["tenant_id", "publickey_ver"])


def _fail_if_downgrade_would_merge_tenant_key_versions() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            "SELECT publickey_ver FROM key_versions GROUP BY publickey_ver HAVING COUNT(*) > 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "RND-332 downgrade blocked: tenant-scoped key versions overlap and cannot "
            "be represented by the legacy global unique constraint."
        )


def downgrade() -> None:
    _fail_if_downgrade_would_merge_tenant_key_versions()
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("key_versions", recreate="always") as batch_op:
            batch_op.drop_constraint(_NEW_UNIQUE, type_="unique")
            batch_op.drop_constraint(_FK, type_="foreignkey")
            batch_op.create_unique_constraint(_OLD_UNIQUE, ["publickey_ver"])
            batch_op.drop_column("tenant_id")
    else:
        op.drop_constraint(_NEW_UNIQUE, "key_versions", type_="unique")
        op.drop_constraint(_FK, "key_versions", type_="foreignkey")
        op.create_unique_constraint(_OLD_UNIQUE, "key_versions", ["publickey_ver"])
        op.drop_column("key_versions", "tenant_id")
