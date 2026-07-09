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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class Tenant(Base):
    """Top-level tenant entity. One row per company in future SaaS; one default row for MVP."""

    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_tenants_slug"),
    )

    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(128), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantWecomConfig(Base):
    """Per-tenant WeCom app credentials. One row per tenant for MVP.

    app_secret: Phase 1 stores plaintext (internal deployment only).
    Phase 3 must encrypt at rest using Fernet or Vault/KMS.
    Do NOT log app_secret — it is a permanent credential.
    """

    __tablename__ = "tenant_wecom_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_wecom_configs_tenant"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36),
        ForeignKey("tenants.id"),
        nullable=False,
    )
    corp_id = Column(String(64), nullable=False)
    agent_id = Column(String(64), nullable=False)
    app_secret = Column(Text, nullable=False)
    callback_domain = Column(String(255), nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AdminUser(Base):
    """WeCom employees who have authenticated via OAuth. Created on first login."""

    __tablename__ = "admin_users"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "wecom_user_id", name="uq_admin_users_tenant_wecom"
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )
    wecom_user_id = Column(String(64), nullable=False)
    name = Column(Text, nullable=True)
    avatar_url = Column(Text, nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AdminSession(Base):
    """Active admin login sessions. session_id (cookie value) is the PK.

    is_revoked: set on logout or forced expiry.
    expires_at: hard TTL enforced server-side on every request.
    Phase 2 (RND-110) implements get_current_user() dependency that reads this table.
    """

    __tablename__ = "admin_sessions"
    __table_args__ = (
        Index("ix_admin_sessions_expires_at", "expires_at"),
    )

    id = Column(String(36), primary_key=True)
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=False, index=True
    )
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )
    wecom_user_id = Column(String(64), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_revoked = Column(Boolean, nullable=False, default=False)


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
    __table_args__ = (
        UniqueConstraint("tenant_id", "corp_id", name="uq_sync_states_tenant_corp_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    corp_id = Column(String(64), nullable=False)
    last_seq = Column(BigInteger, nullable=False, default=0)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
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
        UniqueConstraint(
            "tenant_id", "msgid", name="uq_archive_messages_tenant_msgid"
        ),
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
    msgid = Column(String(64), nullable=False)
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

    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
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
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MediaFile(Base):
    """Tracks download state for media attachments referenced by archive messages.

    tenant_id is nullable during migration (backfilled by
    bootstrap_default_tenant.py, then enforced NOT NULL), matching the same
    pattern used for archive_messages/archive_message_recipients/sync_states/
    contacts. sdkfileid uniqueness is scoped to (tenant_id, sdkfileid), not
    global — two different tenants' WeCom corps could in principle hand back
    the same sdkfileid, and a global unique constraint would make the second
    tenant's insert fail outright.
    """

    __tablename__ = "media_files"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "sdkfileid", name="uq_media_files_tenant_sdkfileid"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sdkfileid = Column(Text, nullable=False)
    archive_message_id = Column(
        BigInteger, ForeignKey("archive_messages.id"), nullable=False, index=True
    )
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
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
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "wecom_userid", name="uq_contacts_tenant_wecom_userid"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    wecom_userid = Column(String(64), nullable=False)
    name = Column(Text, nullable=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
