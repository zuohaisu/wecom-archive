from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class DuplicateCorpIdError(ValueError):
    """Raised when a TenantWecomConfig write would assign an active corp_id
    to more than one tenant. See RND-184."""


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
        # A given WeCom corp_id must resolve to exactly one active tenant.
        # Partial index — inactive/disabled configs are exempt so a corp_id
        # can be freely reassigned after the old config is deactivated.
        Index(
            "uq_tenant_wecom_configs_active_corp_id",
            "corp_id",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
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


def _reject_duplicate_active_corp_id(connection, target: "TenantWecomConfig") -> None:
    """Application-level guard mirroring uq_tenant_wecom_configs_active_corp_id.

    Runs on every ORM insert/update of TenantWecomConfig so callers get a
    clear error before the DB constraint would reject the write. Inactive
    configs are exempt — a corp_id may be reassigned once its old config is
    deactivated.
    """
    if not target.is_active:
        return

    conflict = connection.execute(
        select(TenantWecomConfig.__table__.c.id).where(
            TenantWecomConfig.__table__.c.corp_id == target.corp_id,
            TenantWecomConfig.__table__.c.is_active.is_(True),
            TenantWecomConfig.__table__.c.id != target.id,
        )
    ).first()
    if conflict is not None:
        raise DuplicateCorpIdError(
            "This WeCom CorpID is already assigned to another tenant."
        )


@event.listens_for(TenantWecomConfig, "before_insert")
def _validate_corp_id_on_insert(mapper, connection, target: TenantWecomConfig) -> None:
    _reject_duplicate_active_corp_id(connection, target)


@event.listens_for(TenantWecomConfig, "before_update")
def _validate_corp_id_on_update(mapper, connection, target: TenantWecomConfig) -> None:
    _reject_duplicate_active_corp_id(connection, target)


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
        # RND-201 round 3 (B4, migration 0011): supports the composite
        # foreign keys on message_revocations (tenant_id,
        # revoke_event_message_id/original_message_id) -> here. id alone
        # is already globally unique (primary key); this adds the
        # tenant-scoped pairing Postgres requires as a composite FK
        # target, proving a referencing row's declared tenant actually
        # matches the tenant of the archive_messages row it points to.
        UniqueConstraint(
            "tenant_id", "id", name="uq_archive_messages_tenant_id_id"
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
        Index(
            "ix_archive_messages_structured_content_gin",
            "structured_content",
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

    # --- Structured content (RND-197) ---
    # Type-specific normalized fields + scoped raw sub-payload for the
    # basic structured message types (link/location/markdown/news/
    # miniprogram/card/docmsg/audio_doc). Deliberately separate from
    # decrypted_payload above -- see migration 0008 docstring for why.
    structured_content = Column(JSONB, nullable=True)

    # --- Fields extracted from decrypted_payload ---
    content_text = Column(Text, nullable=True)
    msgtype = Column(String(32), nullable=True, index=True)
    sender = Column(String(64), nullable=True, index=True)
    roomid = Column(String(64), nullable=True, index=True)
    msgtime = Column(BigInteger, nullable=True, index=True)
    tolist = Column(JSONB, nullable=True)
    sdkfileid = Column(Text, nullable=True)

    # --- Revoke association (RND-201) ---
    # Set only by app.revoke_reconciliation, never by the parser/decrypt
    # self-update above -- a message marks itself revoked only as a side
    # effect of a *different* row (its matching "revoke" event) being
    # processed. content_text/structured_content/decrypted_payload and all
    # media_files rows are never touched when these are set -- see
    # app.revoke_reconciliation module docstring.
    is_revoked = Column(Boolean, nullable=False, server_default=text("false"))
    revoked_at = Column(DateTime(timezone=True), nullable=True)

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

    storage_backend / storage_ref (RND-174 QA remediation, migration 0005):
    the authoritative, per-row record of which MediaStorageProvider holds
    this row's bytes ("local" or "qiniu_kodo") and that provider's own
    reference (a local path for "local", a Qiniu object key for
    "qiniu_kodo"). Media access must resolve the provider from THESE
    columns, never from the deployment-wide MEDIA_STORAGE_PROVIDER setting
    — that setting only controls where NEW media is written, so switching
    it must never reinterpret an existing row (see
    app.media_storage.resolve_media_file_state). Migration 0005 backfills
    every pre-existing row as storage_backend="local",
    storage_ref=local_path.

    local_path is kept for backward compatibility only — legacy/local-only,
    never authoritative for a Qiniu-backed row (a Qiniu row's local_path is
    always left None; only its storage_ref holds the object key). New code
    should read storage_backend/storage_ref, falling back to a populated
    legacy local_path only when storage_backend was never backfilled (see
    app.media_storage.resolve_effective_storage_reference).

    migration_status / migration_attempted_at / migration_error (RND-186,
    migration 0006): bookkeeping for scripts/migrate_local_media_to_qiniu.py
    only — no serving/timeline code path reads these. "Already migrated" is
    fully determined by storage_backend=="qiniu_kodo" alone (a migrated row
    is naturally excluded from any future migration candidate scan); these
    columns exist only to distinguish "never attempted" (migration_status
    IS NULL) from "attempted and failed" (migration_status="failed"), since
    a failed attempt must leave storage_backend/storage_ref completely
    untouched (still "local", still fully servable) rather than encoding
    failure there. migration_error is a short, sanitized diagnostic tag —
    never a raw path, sdkfileid, or exception string.

    bucket / mime_type / checksum_sha256 (RND-186 QA fix, migration 0007):
    full storage metadata for a migrated row, populated by
    scripts/migrate_local_media_to_qiniu.py at the moment of a successful
    upload — computed from the exact bytes uploaded, never re-derived from
    a remote round-trip. bucket is NULL for storage_backend="local" (no
    bucket concept) and is read from the provider that performed the
    upload (never re-read from config independently, so it can never drift
    from what was actually used). mime_type is content-sniffed (see
    app.media_storage.detect_media_signature_from_bytes) — never inferred
    from a file extension alone. checksum_sha256 is a fixed algorithm
    (SHA-256), hex-encoded. storage_ref remains the sole authoritative
    object key / local path reference — deliberately not duplicated into a
    second "object_key" column.
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
    storage_backend = Column(String(32), nullable=True, index=True)
    storage_ref = Column(Text, nullable=True)
    file_size = Column(BigInteger, nullable=True)
    download_status = Column(String(16), nullable=False, default="pending")
    migration_status = Column(String(16), nullable=True, index=True)
    migration_attempted_at = Column(DateTime(timezone=True), nullable=True)
    migration_error = Column(Text, nullable=True)
    bucket = Column(String(128), nullable=True)
    mime_type = Column(String(128), nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MessageRevocation(Base):
    """One row per WeCom "revoke" (撤回) event, associating it with the
    original ArchiveMessage it targets (RND-201).

    A revoke event arrives as its own archive_messages row
    (msgtype="revoke"); its decrypted payload's revoke.pre_msgid field
    names the msgid of the message being revoked (WeCom session-archive
    convention: https://developer.work.weixin.qq.com/document/path/91774,
    e.g. {"msgid":"...","action":"recall","msgtype":"revoke",
    "revoke":{"pre_msgid":"..."}}). This table is the durable record of
    that association, independent of which order the two rows arrive in
    or which worker run processes them -- see app.revoke_reconciliation,
    the single canonical place this table is written from.

    status:
      "pending"   -- revoke event decrypted and target_msgid extracted,
                     but no archive_messages row with msgid=target_msgid
                     exists yet in this tenant. Retried on every future
                     decrypt sweep (reconcile_pending_revocations).
      "linked"    -- original_message_id is set; the original row's
                     is_revoked/revoked_at have been updated. Terminal.
      "malformed" -- the revoke event's decrypted payload had no usable
                     target_msgid. Terminal -- there is nothing to retry,
                     but the event and its raw payload are retained
                     rather than dropped.

    original_message_id is set exactly once, on first successful link,
    and never cleared or reassigned afterwards. If two revoke events ever
    target the same original message, the second one still links (so it
    is never left dangling in "pending") but never overwrites the first
    revoke's revoked_at on the original row -- see
    app.revoke_reconciliation for the earliest-wins tie-break rule.

    tenant_id is REQUIRED (NOT NULL, RND-201 round 3 / B4, migration
    0011) -- unlike the nullable-during-migration convention every other
    archive-adjacent table follows, message_revocations was introduced
    by this feature (migration 0009) with no legacy pre-tenant-
    foundation rows possible, so there was never a valid reason for it
    to be nullable here.

    Two CHECK constraints (migration 0010, RND-201 round 2 QA fix)
    enforce association consistency at the database level, not just in
    app.revoke_reconciliation's application logic:

      ck_message_revocations_status_valid -- status can only ever be one
      of the three values this model's docstring documents. Catches a
      future typo/regression at write time instead of silently storing
      an unrecognized status that app.revoke_reconciliation.
      display_status() and every caller would then mis-handle.

      ck_message_revocations_linked_consistency -- original_message_id
      is set if and only if status="linked". This is the exact invariant
      _try_link() maintains by construction (both columns are written
      together in one statement — see app.revoke_reconciliation), made
      impossible to violate even by a future bug or an out-of-band
      manual UPDATE.

    Tenant + uniqueness integrity (migration 0011, RND-201 round 3 / B4
    QA fix) -- database-enforced, not just application-filtered:

      uq_message_revocations_revoke_event_message_id -- a PLAIN unique
      constraint on revoke_event_message_id alone (not
      (tenant_id, revoke_event_message_id)): one revoke event can only
      ever have exactly one association row, period -- a composite
      unique constraint would still theoretically allow the same
      revoke_event_message_id to appear twice under two different
      tenant_id values, which is exactly the ambiguity this closes.

      fk_message_revocations_tenant_revoke_event /
      fk_message_revocations_tenant_original -- composite foreign keys
      (tenant_id, revoke_event_message_id) and
      (tenant_id, original_message_id), both referencing
      archive_messages(tenant_id, id) (see ArchiveMessage's
      uq_archive_messages_tenant_id_id). These replace the old
      single-column FKs to archive_messages.id: Postgres's default
      MATCH SIMPLE means a composite FK is skipped entirely when any of
      its columns is NULL, so original_message_id staying NULL for
      pending/malformed rows is unaffected -- but whenever
      original_message_id IS set (or always, for the NOT-NULL
      revoke_event_message_id side), the constraint now additionally
      proves the referenced archive_messages row belongs to the SAME
      tenant this association row declares, not merely that a row with
      that id exists somewhere.
    """

    __tablename__ = "message_revocations"
    __table_args__ = (
        UniqueConstraint(
            "revoke_event_message_id",
            name="uq_message_revocations_revoke_event_message_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "revoke_event_message_id"],
            ["archive_messages.tenant_id", "archive_messages.id"],
            name="fk_message_revocations_tenant_revoke_event",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "original_message_id"],
            ["archive_messages.tenant_id", "archive_messages.id"],
            name="fk_message_revocations_tenant_original",
        ),
        Index(
            "ix_message_revocations_tenant_target_msgid",
            "tenant_id",
            "target_msgid",
        ),
        CheckConstraint(
            "status IN ('pending', 'linked', 'malformed')",
            name="ck_message_revocations_status_valid",
        ),
        CheckConstraint(
            "(status = 'linked') = (original_message_id IS NOT NULL)",
            name="ck_message_revocations_linked_consistency",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )

    # No per-column ForeignKey(...) here -- the referential integrity for
    # both of these (existence AND tenant match) is provided by the
    # composite ForeignKeyConstraints in __table_args__ above.
    revoke_event_message_id = Column(BigInteger, nullable=False, index=True)
    revoke_event_msgid = Column(String(64), nullable=False)
    revoke_event_msgtime = Column(BigInteger, nullable=True)

    target_msgid = Column(String(64), nullable=True)
    original_message_id = Column(BigInteger, nullable=True, index=True)

    status = Column(String(16), nullable=False, default="pending")

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
