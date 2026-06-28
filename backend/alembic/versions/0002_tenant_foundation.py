"""Tenant foundation: tenants, tenant_wecom_configs, admin_users, admin_sessions tables;
add tenant_id column to archive_messages, archive_message_recipients, sync_states, contacts.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-28

Migration is additive only — no existing data or columns are modified.
Run bootstrap_default_tenant.py after this migration to create the default
tenant and backfill existing rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # tenants
    # ------------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )

    # ------------------------------------------------------------------
    # tenant_wecom_configs
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_wecom_configs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("corp_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("app_secret", sa.Text(), nullable=False),
        sa.Column(
            "callback_domain",
            sa.String(255),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_wecom_configs_tenant_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_wecom_configs_tenant"),
    )

    # ------------------------------------------------------------------
    # admin_users
    # ------------------------------------------------------------------
    op.create_table(
        "admin_users",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("wecom_user_id", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_admin_users_tenant_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "wecom_user_id", name="uq_admin_users_tenant_wecom"
        ),
    )
    op.create_index("ix_admin_users_tenant_id", "admin_users", ["tenant_id"])

    # ------------------------------------------------------------------
    # admin_sessions
    # ------------------------------------------------------------------
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("admin_user_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("wecom_user_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_revoked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["admin_users.id"],
            name="fk_admin_sessions_admin_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_admin_sessions_tenant_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_sessions_admin_user_id", "admin_sessions", ["admin_user_id"]
    )
    op.create_index("ix_admin_sessions_tenant_id", "admin_sessions", ["tenant_id"])
    op.create_index("ix_admin_sessions_expires_at", "admin_sessions", ["expires_at"])

    # ------------------------------------------------------------------
    # Add tenant_id to existing archive tables (nullable — backfill via
    # bootstrap_default_tenant.py, then NOT NULL enforced by that script)
    # ------------------------------------------------------------------
    op.add_column(
        "archive_messages", sa.Column("tenant_id", sa.String(36), nullable=True)
    )
    op.create_foreign_key(
        "fk_archive_messages_tenant_id",
        "archive_messages",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.create_index(
        "ix_archive_messages_tenant_id", "archive_messages", ["tenant_id"]
    )

    op.add_column(
        "archive_message_recipients",
        sa.Column("tenant_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_archive_message_recipients_tenant_id",
        "archive_message_recipients",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.create_index(
        "ix_archive_message_recipients_tenant_id",
        "archive_message_recipients",
        ["tenant_id"],
    )

    op.add_column(
        "sync_states", sa.Column("tenant_id", sa.String(36), nullable=True)
    )
    op.create_foreign_key(
        "fk_sync_states_tenant_id",
        "sync_states",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.create_index("ix_sync_states_tenant_id", "sync_states", ["tenant_id"])

    op.add_column("contacts", sa.Column("tenant_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_contacts_tenant_id",
        "contacts",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.create_index("ix_contacts_tenant_id", "contacts", ["tenant_id"])

    # ------------------------------------------------------------------
    # Replace global unique constraints with tenant-scoped equivalents.
    # tenant_id columns are present at this point (added above), so the
    # composite constraints can be created immediately.  Constraint swap
    # is done last so that if the preceding tenant_id additions fail, the
    # original unique constraints are still intact for rollback safety.
    # ------------------------------------------------------------------

    # archive_messages: UNIQUE(msgid) → UNIQUE(tenant_id, msgid)
    op.drop_constraint(
        "uq_archive_messages_msgid", "archive_messages", type_="unique"
    )
    op.create_unique_constraint(
        "uq_archive_messages_tenant_msgid",
        "archive_messages",
        ["tenant_id", "msgid"],
    )

    # contacts: UNIQUE(wecom_userid) → UNIQUE(tenant_id, wecom_userid)
    op.drop_constraint(
        "uq_contacts_wecom_userid", "contacts", type_="unique"
    )
    op.create_unique_constraint(
        "uq_contacts_tenant_wecom_userid",
        "contacts",
        ["tenant_id", "wecom_userid"],
    )

    # sync_states: UNIQUE(corp_id) → UNIQUE(tenant_id, corp_id)
    op.drop_constraint(
        "uq_sync_states_corp_id", "sync_states", type_="unique"
    )
    op.create_unique_constraint(
        "uq_sync_states_tenant_corp_id",
        "sync_states",
        ["tenant_id", "corp_id"],
    )


def downgrade() -> None:
    # Restore global unique constraints before removing tenant_id columns.
    op.drop_constraint(
        "uq_sync_states_tenant_corp_id", "sync_states", type_="unique"
    )
    op.create_unique_constraint(
        "uq_sync_states_corp_id", "sync_states", ["corp_id"]
    )

    op.drop_constraint(
        "uq_contacts_tenant_wecom_userid", "contacts", type_="unique"
    )
    op.create_unique_constraint(
        "uq_contacts_wecom_userid", "contacts", ["wecom_userid"]
    )

    op.drop_constraint(
        "uq_archive_messages_tenant_msgid", "archive_messages", type_="unique"
    )
    op.create_unique_constraint(
        "uq_archive_messages_msgid", "archive_messages", ["msgid"]
    )

    # Remove tenant_id from archive tables (reverse order)
    op.drop_index("ix_contacts_tenant_id", table_name="contacts")
    op.drop_constraint("fk_contacts_tenant_id", "contacts", type_="foreignkey")
    op.drop_column("contacts", "tenant_id")

    op.drop_index("ix_sync_states_tenant_id", table_name="sync_states")
    op.drop_constraint("fk_sync_states_tenant_id", "sync_states", type_="foreignkey")
    op.drop_column("sync_states", "tenant_id")

    op.drop_index(
        "ix_archive_message_recipients_tenant_id",
        table_name="archive_message_recipients",
    )
    op.drop_constraint(
        "fk_archive_message_recipients_tenant_id",
        "archive_message_recipients",
        type_="foreignkey",
    )
    op.drop_column("archive_message_recipients", "tenant_id")

    op.drop_index("ix_archive_messages_tenant_id", table_name="archive_messages")
    op.drop_constraint(
        "fk_archive_messages_tenant_id", "archive_messages", type_="foreignkey"
    )
    op.drop_column("archive_messages", "tenant_id")

    # Drop new tables in reverse FK dependency order
    op.drop_index("ix_admin_sessions_expires_at", table_name="admin_sessions")
    op.drop_index("ix_admin_sessions_tenant_id", table_name="admin_sessions")
    op.drop_index("ix_admin_sessions_admin_user_id", table_name="admin_sessions")
    op.drop_table("admin_sessions")

    op.drop_index("ix_admin_users_tenant_id", table_name="admin_users")
    op.drop_table("admin_users")
    op.drop_table("tenant_wecom_configs")
    op.drop_table("tenants")
