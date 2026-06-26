"""Initial schema: key_versions, sync_states, archive_messages,
archive_message_recipients, media_files, contacts

Revision ID: 0001
Revises:
Create Date: 2026-06-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # key_versions
    # ------------------------------------------------------------------
    op.create_table(
        "key_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("publickey_ver", sa.Integer(), nullable=False),
        sa.Column("key_alias", sa.String(128), nullable=False),
        sa.Column("private_key_path", sa.Text(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("publickey_ver", name="uq_key_versions_publickey_ver"),
    )
    op.create_index("ix_key_versions_publickey_ver", "key_versions", ["publickey_ver"])

    # ------------------------------------------------------------------
    # sync_states
    # ------------------------------------------------------------------
    op.create_table(
        "sync_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("corp_id", sa.String(64), nullable=False),
        sa.Column(
            "last_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("corp_id", name="uq_sync_states_corp_id"),
    )

    # ------------------------------------------------------------------
    # archive_messages
    # ------------------------------------------------------------------
    op.create_table(
        "archive_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("msgid", sa.String(64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        # Encrypted envelope
        sa.Column("publickey_ver", sa.Integer(), nullable=False),
        sa.Column("raw_encrypted_payload", JSONB(), nullable=True),
        sa.Column("encrypt_random_key", sa.Text(), nullable=False),
        sa.Column("encrypt_chat_msg", sa.Text(), nullable=False),
        # Decryption state
        sa.Column(
            "decrypt_status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("decrypted_payload", JSONB(), nullable=True),
        # Fields extracted from decrypted_payload
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("msgtype", sa.String(32), nullable=True),
        sa.Column("sender", sa.String(64), nullable=True),
        sa.Column("roomid", sa.String(64), nullable=True),
        sa.Column("msgtime", sa.BigInteger(), nullable=True),
        sa.Column("tolist", JSONB(), nullable=True),
        sa.Column("sdkfileid", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("msgid", name="uq_archive_messages_msgid"),
    )
    op.create_index("ix_archive_messages_seq", "archive_messages", ["seq"])
    op.create_index("ix_archive_messages_msgtype", "archive_messages", ["msgtype"])
    op.create_index("ix_archive_messages_sender", "archive_messages", ["sender"])
    op.create_index("ix_archive_messages_roomid", "archive_messages", ["roomid"])
    op.create_index("ix_archive_messages_msgtime", "archive_messages", ["msgtime"])
    op.create_index(
        "ix_archive_messages_msgtime_msgtype",
        "archive_messages",
        ["msgtime", "msgtype"],
    )
    op.create_index(
        "ix_archive_messages_decrypted_payload_gin",
        "archive_messages",
        ["decrypted_payload"],
        postgresql_using="gin",
    )
    # Functional GIN index for full-text search on extracted content_text.
    # Uses 'simple' dictionary so no language stemming is applied — suitable for
    # mixed Chinese/English WeCom messages.
    op.execute(
        "CREATE INDEX ix_archive_messages_content_text_fts "
        "ON archive_messages USING gin "
        "(to_tsvector('simple', coalesce(content_text, '')))"
    )

    # ------------------------------------------------------------------
    # archive_message_recipients
    # ------------------------------------------------------------------
    op.create_table(
        "archive_message_recipients",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("receiver_userid", sa.String(64), nullable=False),
        sa.Column("receiver_type", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["archive_messages.id"],
            name="fk_archive_message_recipients_message_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_archive_message_recipients_message_id",
        "archive_message_recipients",
        ["message_id"],
    )
    op.create_index(
        "ix_archive_message_recipients_receiver_userid",
        "archive_message_recipients",
        ["receiver_userid"],
    )

    # ------------------------------------------------------------------
    # media_files
    # ------------------------------------------------------------------
    op.create_table(
        "media_files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sdkfileid", sa.Text(), nullable=False),
        sa.Column("archive_message_id", sa.BigInteger(), nullable=False),
        sa.Column("file_type", sa.String(32), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("oss_key", sa.Text(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column(
            "download_status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
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
            ["archive_message_id"],
            ["archive_messages.id"],
            name="fk_media_files_archive_message_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sdkfileid", name="uq_media_files_sdkfileid"),
    )
    op.create_index(
        "ix_media_files_archive_message_id", "media_files", ["archive_message_id"]
    )

    # ------------------------------------------------------------------
    # contacts
    # ------------------------------------------------------------------
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("wecom_userid", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("wecom_userid", name="uq_contacts_wecom_userid"),
    )


def downgrade() -> None:
    op.drop_table("contacts")
    op.drop_table("media_files")
    op.drop_table("archive_message_recipients")
    op.execute(
        "DROP INDEX IF EXISTS ix_archive_messages_content_text_fts"
    )
    op.drop_index(
        "ix_archive_messages_decrypted_payload_gin",
        table_name="archive_messages",
    )
    op.drop_index("ix_archive_messages_msgtime_msgtype", table_name="archive_messages")
    op.drop_index("ix_archive_messages_msgtime", table_name="archive_messages")
    op.drop_index("ix_archive_messages_roomid", table_name="archive_messages")
    op.drop_index("ix_archive_messages_sender", table_name="archive_messages")
    op.drop_index("ix_archive_messages_msgtype", table_name="archive_messages")
    op.drop_index("ix_archive_messages_seq", table_name="archive_messages")
    op.drop_table("archive_messages")
    op.drop_table("sync_states")
    op.drop_index("ix_key_versions_publickey_ver", table_name="key_versions")
    op.drop_table("key_versions")
