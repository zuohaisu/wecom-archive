"""Add durable asynchronous media export jobs.

Revision ID: 0046
Revises: 0045
"""

from alembic import op
import sqlalchemy as sa


revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("storage_backend", sa.String(length=32), nullable=True),
        sa.Column("storage_ref", sa.Text(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column(
            "notification_status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "notification_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "notification_last_attempt_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("notification_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("kind = 'media_zip'", name="ck_export_jobs_kind"),
        sa.CheckConstraint("format = 'zip'", name="ck_export_jobs_format"),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'ready', 'failed', 'expired')",
            name="ck_export_jobs_status",
        ),
        sa.CheckConstraint(
            "notification_status IN ('pending', 'sent', 'failed')",
            name="ck_export_jobs_notification_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_export_jobs_attempt_count"),
        sa.CheckConstraint(
            "notification_attempts >= 0",
            name="ck_export_jobs_notification_attempts",
        ),
        sa.CheckConstraint(
            "file_size IS NULL OR file_size >= 0", name="ck_export_jobs_file_size"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["admin_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_export_jobs_requested_by", "export_jobs", ["requested_by"], unique=False
    )
    op.create_index(
        "ix_export_jobs_tenant_requested",
        "export_jobs",
        ["tenant_id", "requested_at"],
        unique=False,
    )
    op.create_index(
        "ix_export_jobs_status_requested",
        "export_jobs",
        ["status", "requested_at"],
        unique=False,
    )
    op.create_index(
        "ix_export_jobs_status_expires",
        "export_jobs",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_export_jobs_status_expires", table_name="export_jobs")
    op.drop_index("ix_export_jobs_status_requested", table_name="export_jobs")
    op.drop_index("ix_export_jobs_tenant_requested", table_name="export_jobs")
    op.drop_index("ix_export_jobs_requested_by", table_name="export_jobs")
    op.drop_table("export_jobs")
