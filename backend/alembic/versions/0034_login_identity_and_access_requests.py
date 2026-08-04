"""Create login-identity binding and access-request tables (RND-321).

The WeCom-identity-to-account binding was previously implicit in
admin_users.wecom_user_id: an unrecognized scan upserted a brand-new
AdminUser row directly. That conflated "WeCom proved this person is an
employee" with "this app grants them a console account", and made it
impossible to tell a genuinely new person from the same person's email
account under a different WeCom identity (or a departed employee's
WeCom UserId later reassigned to someone else).

admin_login_identities is now the source of truth for "which AdminUser may
this verified identity sign a session for" — admin_users.wecom_user_id is
kept only as a read-after-write compatibility field, synced once a bind
succeeds here, never read as authoritative by the login path itself.

admin_access_requests holds identities WeCom has verified but that are not
yet bound to any account — no AdminUser is created for these; an
owner/admin must explicitly link or create one via the review API.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-04
"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_ENUM = sa.Enum("pending", "resolved", name="admin_access_request_status")


def upgrade() -> None:
    op.create_table(
        "admin_login_identities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("admin_user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "verified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "provider", "subject",
            name="uq_login_identity_tenant_provider_subject",
        ),
        sa.UniqueConstraint(
            "admin_user_id", "provider",
            name="uq_login_identity_admin_user_provider",
        ),
    )
    op.create_index(
        "ix_admin_login_identities_admin_user_id",
        "admin_login_identities",
        ["admin_user_id"],
    )
    op.create_index(
        "ix_admin_login_identities_tenant_id",
        "admin_login_identities",
        ["tenant_id"],
    )

    # Unlike add_column() (see e.g. 0015, 0017), create_table() creates a
    # column's native enum type itself as part of the table DDL — an
    # explicit _STATUS_ENUM.create() first would collide with it.
    op.create_table(
        "admin_access_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("email_hint", sa.Text(), nullable=True),
        sa.Column("status", _STATUS_ENUM, server_default=sa.text("'pending'"), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolved_admin_user_id", sa.String(length=36), nullable=True),
        sa.Column("resolved_by_admin_user_id", sa.String(length=36), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved')",
            name="ck_admin_access_requests_status_valid",
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('linked', 'created')",
            name="ck_admin_access_requests_resolution_valid",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["resolved_admin_user_id"], ["admin_users.id"]),
        sa.ForeignKeyConstraint(["resolved_by_admin_user_id"], ["admin_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_access_requests_tenant_id",
        "admin_access_requests",
        ["tenant_id"],
    )
    op.create_index(
        "ix_admin_access_requests_tenant_status",
        "admin_access_requests",
        ["tenant_id", "status"],
    )
    # Idempotency for repeat scans while pending — see model docstring.
    # Only postgresql_where is set: this partial-unique-index technique
    # already exists once in this codebase (reachability_audit_runs,
    # migration 0032) with the identical shape, and that migration also
    # only sets postgresql_where — SQLite offline tests build tables from
    # the SQLAlchemy declarative models directly (which separately carry a
    # sqlite_where), not from op.create_index's Alembic DDL.
    op.create_index(
        "uq_admin_access_requests_pending_tenant_provider_subject",
        "admin_access_requests",
        ["tenant_id", "provider", "subject"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Backfill: every existing AdminUser with a real, verified WeCom
    # identity gets a matching login-identity row so their next scan
    # resolves through the new table instead of falling through to "no
    # identity bound" and generating a spurious access request.
    #
    # Two shapes are deliberately excluded, matching the ticket's explicit
    # instruction not to treat placeholders as WeCom identities:
    #   - password-mode sentinel: "{PASSWORD_MODE_WECOM_PREFIX}<username>__"
    #     (see app/auth.py PASSWORD_MODE_WECOM_PREFIX) — not a real WeCom
    #     UserId, just how the legacy env-admin bootstrap account is keyed.
    #   - invite placeholder: "invited:<email>" (see
    #     app/routers/auth.py _create_pending_invite) — assigned before the
    #     invitee has ever authenticated with WeCom at all.
    # Rows from the *old* RND-321 attempt (invite_status='access_requested',
    # a real disabled AdminUser created directly from an unrecognized scan)
    # are excluded too: backfilling an identity for them would silently
    # bind and unblock accounts nobody has reviewed, which is exactly the
    # auto-merge behavior this rework exists to stop. They are left
    # exactly as they are — an owner reviews and resolves them by hand.
    #
    # IDs are generated in Python (uuid.uuid4(), same as every other ID in
    # this app) rather than a database-native function: gen_random_uuid()
    # needs Postgres 13+ or pgcrypto, and nothing else in this codebase
    # currently assumes that's available, so this migration doesn't either.
    connection = op.get_bind()
    admin_users_table = sa.table(
        "admin_users",
        sa.column("id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("wecom_user_id", sa.Text),
        sa.column("invite_status", sa.Text),
        sa.column("last_login_at", sa.DateTime),
        sa.column("created_at", sa.DateTime),
    )
    candidates = connection.execute(
        sa.select(
            admin_users_table.c.id,
            admin_users_table.c.tenant_id,
            admin_users_table.c.wecom_user_id,
            admin_users_table.c.last_login_at,
            admin_users_table.c.created_at,
        ).where(
            admin_users_table.c.wecom_user_id.isnot(None),
            admin_users_table.c.wecom_user_id != "",
            sa.func.substr(admin_users_table.c.wecom_user_id, 1, 7) != "__pwd__",
            sa.func.substr(admin_users_table.c.wecom_user_id, 1, 8) != "invited:",
            sa.or_(
                admin_users_table.c.invite_status.is_(None),
                admin_users_table.c.invite_status != "access_requested",
            ),
        )
    ).fetchall()

    if candidates:
        login_identities_table = sa.table(
            "admin_login_identities",
            sa.column("id", sa.String),
            sa.column("tenant_id", sa.String),
            sa.column("provider", sa.Text),
            sa.column("subject", sa.Text),
            sa.column("admin_user_id", sa.String),
            sa.column("verified_at", sa.DateTime),
            sa.column("created_at", sa.DateTime),
        )
        connection.execute(
            login_identities_table.insert(),
            [
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": row.tenant_id,
                    "provider": "wecom",
                    "subject": row.wecom_user_id,
                    "admin_user_id": row.id,
                    "verified_at": row.last_login_at or row.created_at,
                    "created_at": row.created_at,
                }
                for row in candidates
            ],
        )

    # Rows from the *old* RND-321 attempt (invite_status='access_requested')
    # are deliberately left untouched here, including their legacy
    # wecom_user_id — this migration only ever creates the two new tables
    # and inserts identity rows for *other* accounts; it never mutates an
    # existing admin_users row. A colliding wecom_user_id blocking
    # resolution of a fresh request for the same identity is handled at
    # request-resolution time in app/routers/users.py (an explicit,
    # owner-only, audited release), not here — see that module for why:
    # this keeps upgrade/downgrade fully symmetric on pre-existing data
    # (round-trip tested against real Postgres below) and avoids retaining
    # the real WeCom UserId anywhere just to make a rarely-used manual
    # downgrade "restore" a value nothing authoritative reads anymore.


def downgrade() -> None:
    op.drop_index(
        "uq_admin_access_requests_pending_tenant_provider_subject",
        table_name="admin_access_requests",
    )
    op.drop_index("ix_admin_access_requests_tenant_status", table_name="admin_access_requests")
    op.drop_index("ix_admin_access_requests_tenant_id", table_name="admin_access_requests")
    op.drop_table("admin_access_requests")
    op.drop_index("ix_admin_login_identities_tenant_id", table_name="admin_login_identities")
    op.drop_index("ix_admin_login_identities_admin_user_id", table_name="admin_login_identities")
    op.drop_table("admin_login_identities")
    _STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
