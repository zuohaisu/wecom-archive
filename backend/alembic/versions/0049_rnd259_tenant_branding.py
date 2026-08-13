"""Add tenant-scoped paid branding and custom-domain control-plane state.

Revision ID: 0049
Revises: 0048
"""

from alembic import op
import sqlalchemy as sa


revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_branding",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("logo_content", sa.LargeBinary(), nullable=True),
        sa.Column("logo_mime_type", sa.String(length=32), nullable=True),
        sa.Column("logo_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("favicon_content", sa.LargeBinary(), nullable=True),
        sa.Column("favicon_mime_type", sa.String(length=32), nullable=True),
        sa.Column("favicon_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("custom_domain", sa.String(length=253), nullable=True),
        sa.Column("domain_state", sa.String(length=32), nullable=True),
        sa.Column("domain_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("verification_token_hash", sa.String(length=64), nullable=True),
        sa.Column("verification_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("domain_last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "certificate_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'not_requested'"),
        ),
        sa.Column("certificate_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("certificate_last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("domain_failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "domain_state IS NULL OR domain_state IN "
            "('pending_verification', 'verified', 'active')",
            name="ck_tenant_branding_domain_state",
        ),
        sa.CheckConstraint(
            "certificate_status IN ('not_requested', 'pending', 'issued', 'failed', 'expired')",
            name="ck_tenant_branding_certificate_status",
        ),
        sa.CheckConstraint(
            "(logo_content IS NULL) = (logo_mime_type IS NULL)",
            name="ck_tenant_branding_logo_pair",
        ),
        sa.CheckConstraint(
            "(favicon_content IS NULL) = (favicon_mime_type IS NULL)",
            name="ck_tenant_branding_favicon_pair",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_branding_tenant"),
        sa.UniqueConstraint("custom_domain", name="uq_tenant_branding_custom_domain"),
    )
    op.create_index(
        "ix_tenant_branding_custom_domain",
        "tenant_branding",
        ["custom_domain"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_branding_custom_domain", table_name="tenant_branding")
    op.drop_table("tenant_branding")
