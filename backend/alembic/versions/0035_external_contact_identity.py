"""Separate external-contact nickname, employee remarks, and nickname history.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Do not copy legacy external_contacts.name into any new nickname column:
    # historic rows cannot distinguish an employee remark from a customer
    # nickname. The idempotent external-contact sync performs that safe API
    # backfill after deployment.
    op.add_column("external_contacts", sa.Column("current_nickname_raw", sa.Text(), nullable=True))
    op.add_column(
        "external_contacts",
        sa.Column("current_nickname_normalized", sa.Text(), nullable=True),
    )
    op.add_column(
        "external_contacts",
        sa.Column("current_nickname_display", sa.Text(), nullable=True),
    )
    op.add_column(
        "external_contacts",
        sa.Column("current_nickname_observed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "external_contact_follows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("external_userid", sa.String(length=64), nullable=False),
        sa.Column("follow_userid", sa.String(length=64), nullable=False),
        sa.Column("remark_raw", sa.Text(), nullable=True),
        sa.Column("remark_normalized", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "external_userid"],
            ["external_contacts.tenant_id", "external_contacts.external_userid"],
            name="fk_external_contact_follows_tenant_contact",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "external_userid",
            "follow_userid",
            name="uq_external_contact_follows_tenant_contact_user",
        ),
    )
    op.create_table(
        "external_contact_nickname_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("external_userid", sa.String(length=64), nullable=False),
        sa.Column("old_nickname_raw", sa.Text(), nullable=True),
        sa.Column("old_nickname_normalized", sa.Text(), nullable=True),
        sa.Column("old_nickname_display", sa.Text(), nullable=True),
        sa.Column("new_nickname_raw", sa.Text(), nullable=True),
        sa.Column("new_nickname_normalized", sa.Text(), nullable=True),
        sa.Column("new_nickname_display", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "external_userid"],
            ["external_contacts.tenant_id", "external_contacts.external_userid"],
            name="fk_external_contact_nickname_history_tenant_contact",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_external_contact_follows_tenant_contact_active",
        "external_contact_follows",
        ["tenant_id", "external_userid", "is_active"],
    )
    op.create_index(
        "ix_external_contact_nickname_history_tenant_contact_observed",
        "external_contact_nickname_history",
        ["tenant_id", "external_userid", "observed_at", "id"],
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_external_contacts_current_nickname_trgm",
        "external_contacts",
        ["current_nickname_normalized"],
        postgresql_using="gin",
        postgresql_ops={"current_nickname_normalized": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_external_contact_follows_remark_trgm",
        "external_contact_follows",
        ["remark_normalized"],
        postgresql_using="gin",
        postgresql_ops={"remark_normalized": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_external_contact_nickname_history_old_trgm",
        "external_contact_nickname_history",
        ["old_nickname_normalized"],
        postgresql_using="gin",
        postgresql_ops={"old_nickname_normalized": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_external_contact_nickname_history_new_trgm",
        "external_contact_nickname_history",
        ["new_nickname_normalized"],
        postgresql_using="gin",
        postgresql_ops={"new_nickname_normalized": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_external_contact_nickname_history_new_trgm", table_name="external_contact_nickname_history")
    op.drop_index("ix_external_contact_nickname_history_old_trgm", table_name="external_contact_nickname_history")
    op.drop_index("ix_external_contact_follows_remark_trgm", table_name="external_contact_follows")
    op.drop_index("ix_external_contacts_current_nickname_trgm", table_name="external_contacts")
    op.drop_index(
        "ix_external_contact_nickname_history_tenant_contact_observed",
        table_name="external_contact_nickname_history",
    )
    op.drop_index(
        "ix_external_contact_follows_tenant_contact_active",
        table_name="external_contact_follows",
    )
    op.drop_table("external_contact_nickname_history")
    op.drop_table("external_contact_follows")
    op.drop_column("external_contacts", "current_nickname_observed_at")
    op.drop_column("external_contacts", "current_nickname_display")
    op.drop_column("external_contacts", "current_nickname_normalized")
    op.drop_column("external_contacts", "current_nickname_raw")
