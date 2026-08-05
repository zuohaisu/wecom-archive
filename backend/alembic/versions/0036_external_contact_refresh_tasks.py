"""Durable event-driven external-contact refresh tasks.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_contact_refresh_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("external_userid", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "external_userid",
            name="uq_external_contact_refresh_tasks_tenant_external_userid",
        ),
    )
    op.create_index(
        "ix_external_contact_refresh_tasks_tenant_ready",
        "external_contact_refresh_tasks",
        ["tenant_id", "state", "next_attempt_at", "id"],
    )
    op.create_index(
        "ix_external_contact_refresh_tasks_tenant_id",
        "external_contact_refresh_tasks",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_contact_refresh_tasks_tenant_id",
        table_name="external_contact_refresh_tasks",
    )
    op.drop_index(
        "ix_external_contact_refresh_tasks_tenant_ready",
        table_name="external_contact_refresh_tasks",
    )
    op.drop_table("external_contact_refresh_tasks")
