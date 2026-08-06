"""Add provisioning lifecycle, third-party binding and restricted sessions.

Revision ID: 0040
Revises: 0039
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("lifecycle_status", sa.String(length=16), server_default="active", nullable=False),
    )
    op.execute("UPDATE tenants SET lifecycle_status = CASE WHEN is_active THEN 'active' ELSE 'suspended' END")
    op.create_check_constraint(
        "ck_tenants_lifecycle_status",
        "tenants",
        "lifecycle_status IN ('provisioning', 'active', 'suspended')",
    )
    op.add_column(
        "admin_sessions",
        sa.Column("session_scope", sa.String(length=16), server_default="admin", nullable=False),
    )
    op.create_check_constraint(
        "ck_admin_sessions_scope",
        "admin_sessions",
        "session_scope IN ('admin', 'provisioning')",
    )
    op.create_table(
        "third_party_organization_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("corp_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("permanent_code_encrypted", sa.Text(), nullable=False),
        sa.Column("authorization_mode", sa.String(length=16), server_default="admin", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("authorization_mode = 'admin'", name="ck_third_party_binding_admin_mode"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("corp_id", name="uq_third_party_binding_corp"),
        sa.UniqueConstraint("tenant_id", name="uq_third_party_binding_tenant"),
    )
    op.add_column("wecom_organization_claims", sa.Column("provisioned_tenant_id", sa.String(length=36), nullable=True))
    op.add_column("wecom_organization_claims", sa.Column("provisioning_session_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_wecom_claim_provisioned_tenant",
        "wecom_organization_claims",
        "tenants",
        ["provisioned_tenant_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_wecom_claim_provisioned_tenant", "wecom_organization_claims", type_="foreignkey")
    op.drop_column("wecom_organization_claims", "provisioning_session_id")
    op.drop_column("wecom_organization_claims", "provisioned_tenant_id")
    op.drop_table("third_party_organization_bindings")
    op.drop_constraint("ck_admin_sessions_scope", "admin_sessions", type_="check")
    op.drop_column("admin_sessions", "session_scope")
    op.drop_constraint("ck_tenants_lifecycle_status", "tenants", type_="check")
    op.drop_column("tenants", "lifecycle_status")
