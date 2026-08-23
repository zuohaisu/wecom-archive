"""Add platform operator invitations and password history (RND-415).

Revision ID: 0067
Revises: 0066
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: Union[str, None] = "0066"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column("platform_admins", sa.Column("name", sa.Text(), nullable=True))
    op.add_column(
        "platform_admins",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    if _is_postgresql():
        # ADD VALUE is intentionally non-destructive and is safe to retry on
        # PostgreSQL versions supported by the deployment target.
        op.execute("ALTER TYPE platform_admin_status ADD VALUE IF NOT EXISTS 'pending'")
        op.alter_column(
            "platform_admins",
            "password_hash",
            existing_type=sa.Text(),
            nullable=True,
        )
    else:
        # SQLite represents the existing enum as VARCHAR. Its batch rebuild
        # is needed only for the nullable password column.
        with op.batch_alter_table("platform_admins") as batch_op:
            batch_op.alter_column(
                "password_hash", existing_type=sa.Text(), nullable=True
            )

    op.create_table(
        "platform_admin_password_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("platform_admin_id", sa.String(length=36), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["platform_admin_id"], ["platform_admins.id"],
            name="fk_platform_admin_password_history_platform_admin_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_admin_password_history_platform_admin_id",
        "platform_admin_password_history",
        ["platform_admin_id"],
    )
    op.create_index(
        "ix_platform_admin_password_history_admin_created",
        "platform_admin_password_history",
        ["platform_admin_id", "created_at"],
    )

    op.create_table(
        "platform_admin_invitations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("platform_admin_id", sa.String(length=36), nullable=False),
        sa.Column("invited_by_platform_admin_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["platform_admin_id"], ["platform_admins.id"],
            name="fk_platform_admin_invitations_platform_admin_id",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_platform_admin_id"], ["platform_admins.id"],
            name="fk_platform_admin_invitations_invited_by_platform_admin_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_admin_invitations_platform_admin_id",
        "platform_admin_invitations",
        ["platform_admin_id"],
    )
    op.create_index(
        "ix_platform_admin_invitations_invited_by_platform_admin_id",
        "platform_admin_invitations",
        ["invited_by_platform_admin_id"],
    )
    op.create_index(
        "ix_platform_admin_invitations_token_hash",
        "platform_admin_invitations",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_platform_admin_invitations_expires_at",
        "platform_admin_invitations",
        ["expires_at"],
    )


def downgrade() -> None:
    pending = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM platform_admins WHERE status = 'pending'")
    ).scalar()
    if pending:
        raise RuntimeError(
            "Cannot downgrade RND-415 while pending platform operator invitations exist"
        )

    op.drop_index(
        "ix_platform_admin_invitations_expires_at",
        table_name="platform_admin_invitations",
    )
    op.drop_index(
        "ix_platform_admin_invitations_token_hash",
        table_name="platform_admin_invitations",
    )
    op.drop_index(
        "ix_platform_admin_invitations_invited_by_platform_admin_id",
        table_name="platform_admin_invitations",
    )
    op.drop_index(
        "ix_platform_admin_invitations_platform_admin_id",
        table_name="platform_admin_invitations",
    )
    op.drop_table("platform_admin_invitations")
    op.drop_index(
        "ix_platform_admin_password_history_admin_created",
        table_name="platform_admin_password_history",
    )
    op.drop_index(
        "ix_platform_admin_password_history_platform_admin_id",
        table_name="platform_admin_password_history",
    )
    op.drop_table("platform_admin_password_history")

    if _is_postgresql():
        op.alter_column(
            "platform_admins",
            "password_hash",
            existing_type=sa.Text(),
            nullable=False,
        )
        # PostgreSQL cannot drop an enum value. Recreate the original type
        # after proving no pending row exists, rather than silently leaving a
        # schema whose type no longer matches the downgraded model.
        op.execute("ALTER TABLE platform_admins ALTER COLUMN status DROP DEFAULT")
        op.execute("ALTER TYPE platform_admin_status RENAME TO platform_admin_status_rnd415")
        op.execute("CREATE TYPE platform_admin_status AS ENUM ('active', 'disabled')")
        op.execute(
            "ALTER TABLE platform_admins ALTER COLUMN status TYPE platform_admin_status "
            "USING status::text::platform_admin_status"
        )
        op.execute(
            "ALTER TABLE platform_admins ALTER COLUMN status SET DEFAULT 'active'::platform_admin_status"
        )
        op.execute("DROP TYPE platform_admin_status_rnd415")
        op.drop_column("platform_admins", "last_login_at")
        op.drop_column("platform_admins", "name")
    else:
        with op.batch_alter_table("platform_admins") as batch_op:
            batch_op.alter_column(
                "password_hash", existing_type=sa.Text(), nullable=False
            )
            batch_op.drop_column("last_login_at")
            batch_op.drop_column("name")
