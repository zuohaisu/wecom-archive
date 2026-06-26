from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class KeyVersion(Base):
    """Maps WeCom publickey_ver to the private key used for decryption."""

    __tablename__ = "key_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    publickey_ver = Column(Integer, unique=True, nullable=False)
    key_alias = Column(String(128), nullable=False)
    private_key_path = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SyncState(Base):
    """Persists the last synced seq per corp so restarts resume safely."""

    __tablename__ = "sync_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    corp_id = Column(String(64), unique=True, nullable=False)
    last_seq = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ArchiveMessage(Base):
    """
    Stores one WeCom archive message per row.

    Encrypted envelope fields (raw_encrypted_payload, encrypt_random_key,
    encrypt_chat_msg) are always retained so decryption can be re-run after a
    key rotation.  decrypt_status tracks whether decryption has been attempted:
      pending  — row inserted but decryption not yet run
      success  — decrypted_payload is populated
      failed   — decryption failed; decrypted_payload is null

    content_text holds the plain-text body extracted from decrypted_payload and
    is indexed for full-text search via a GIN tsvector index.
    """

    __tablename__ = "archive_messages"
    __table_args__ = (
        Index("ix_archive_messages_msgtime_msgtype", "msgtime", "msgtype"),
        Index(
            "ix_archive_messages_decrypted_payload_gin",
            "decrypted_payload",
            postgresql_using="gin",
        ),
        Index(
            "ix_archive_messages_content_text_fts",
            text("to_tsvector('simple', coalesce(content_text, ''))"),
            postgresql_using="gin",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    msgid = Column(String(64), unique=True, nullable=False)
    seq = Column(BigInteger, nullable=False, index=True)

    # --- Encrypted envelope ---
    publickey_ver = Column(Integer, nullable=False)
    raw_encrypted_payload = Column(JSONB, nullable=True)
    encrypt_random_key = Column(Text, nullable=False)
    encrypt_chat_msg = Column(Text, nullable=False)

    # --- Decryption state ---
    decrypt_status = Column(String(16), nullable=False, default="pending")
    decrypted_payload = Column(JSONB, nullable=True)

    # --- Fields extracted from decrypted_payload ---
    content_text = Column(Text, nullable=True)
    msgtype = Column(String(32), nullable=True, index=True)
    sender = Column(String(64), nullable=True, index=True)
    roomid = Column(String(64), nullable=True, index=True)
    msgtime = Column(BigInteger, nullable=True, index=True)
    tolist = Column(JSONB, nullable=True)
    sdkfileid = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ArchiveMessageRecipient(Base):
    """
    Per-receiver lookup rows derived from archive_messages.tolist.

    One row per (message, receiver) pair.  Populated alongside the parent
    archive_messages row so that queries like "find all messages received by
    user X" can use a plain indexed B-tree lookup instead of JSONB containment.
    """

    __tablename__ = "archive_message_recipients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(
        BigInteger, ForeignKey("archive_messages.id"), nullable=False, index=True
    )
    receiver_userid = Column(String(64), nullable=False, index=True)
    receiver_type = Column(String(32), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MediaFile(Base):
    """Tracks download state for media attachments referenced by archive messages."""

    __tablename__ = "media_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sdkfileid = Column(Text, unique=True, nullable=False)
    archive_message_id = Column(
        BigInteger, ForeignKey("archive_messages.id"), nullable=False, index=True
    )
    file_type = Column(String(32), nullable=True)
    local_path = Column(Text, nullable=True)
    oss_key = Column(Text, nullable=True)
    file_size = Column(BigInteger, nullable=True)
    download_status = Column(String(16), nullable=False, default="pending")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Contact(Base):
    """Lightweight registry of WeCom user identities seen in the archive."""

    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wecom_userid = Column(String(64), unique=True, nullable=False)
    name = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
