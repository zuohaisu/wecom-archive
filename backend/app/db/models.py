from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
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
    onboarding_completed_at = Column(DateTime(timezone=True), nullable=True)
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

    app_secret and private_key_encrypted are Fernet ciphertext at rest.
    Do NOT log either credential or their decrypted accessors.
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
    # RND-311 (B2-1): encrypted PEM private key; nullable for existing rows.
    private_key_encrypted = Column(Text, nullable=True)

    def set_app_secret(self, plain: str) -> None:
        """Encrypt and assign the permanent WeCom app credential for storage."""
        # Lazy import keeps this ORM model independent of crypto module import
        # order and avoids circular imports during application startup.
        from app.crypto import encrypt_value

        self.app_secret = encrypt_value(plain)

    @property
    def decrypted_app_secret(self) -> str:
        """Return the stored WeCom app credential; never log this value."""
        # See set_app_secret() for why this import intentionally stays local.
        from app.crypto import decrypt_value

        return decrypt_value(self.app_secret)

    def set_credentials(self, secret: str, private_key_pem: str) -> None:
        """Encrypt and assign the WeCom secret and RSA PEM private key."""
        from app.crypto import encrypt_value

        self.app_secret = encrypt_value(secret)
        self.private_key_encrypted = encrypt_value(private_key_pem)

    @property
    def decrypted_private_key(self) -> str:
        """Return the stored RSA PEM private key; never log this value."""
        from app.crypto import decrypt_value

        return decrypt_value(self.private_key_encrypted)

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


class RetentionConfig(Base):
    """Per-tenant message retention policy (RND-318)."""

    __tablename__ = "retention_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_retention_configs_tenant"),
        CheckConstraint(
            "retention_days >= 1 AND retention_days <= 3650",
            name="ck_retention_configs_retention_days_range",
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    retention_days = Column(Integer, nullable=False)
    is_locked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AppConfigStore(Base):
    """Application-level configuration values for this deployment (RND-246)."""

    __tablename__ = "app_config_store"

    key = Column(String, primary_key=True)
    group = Column(String, nullable=False)
    value = Column(Text, nullable=True)
    is_secret = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    value_type = Column(String, nullable=False)
    requires_restart = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by = Column(String, nullable=True)


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
        # RND-191: pg_trgm GIN indexes so search_contacts' ILIKE '%term%'
        # predicates (app/routers/search.py) are index-backed instead of a
        # sequential scan. See migration 0013.
        Index(
            "ix_admin_users_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_admin_users_wecom_user_id_trgm",
            "wecom_user_id",
            postgresql_using="gin",
            postgresql_ops={"wecom_user_id": "gin_trgm_ops"},
        ),
        Index("ix_admin_users_invite_token", "invite_token"),
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

    # RND-277 (F0-1) account system fields.
    password_hash = Column(Text, nullable=True)
    role = Column(
        Enum(
            "owner",
            "admin",
            "compliance",
            "legal",
            "readonlyaudit",
            name="admin_user_role",
        ),
        nullable=False,
        server_default=text("'admin'"),
    )
    status = Column(
        Enum("active", "disabled", name="admin_user_status"),
        nullable=False,
        server_default=text("'active'"),
    )
    email = Column(Text, nullable=True)
    phone = Column(Text, nullable=True)
    department = Column(Text, nullable=True)
    last_active_at = Column(DateTime(timezone=True), nullable=True)
    invite_token = Column(Text, nullable=True)
    invited_by = Column(String(36), ForeignKey("admin_users.id"), nullable=True)
    invite_status = Column(Text, nullable=True)

    # RND-297 (A8-2): persisted UI appearance and language preferences.
    ui_theme = Column(String(16), nullable=False, server_default=text("'light'"))
    ui_locale = Column(String(16), nullable=False, server_default=text("'zh-CN'"))


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


class AdminLoginIdentity(Base):
    """RND-321: which AdminUser a verified external login identity (a
    WeCom UserId, scoped to one tenant) is authorized to sign a session
    for.

    This is the source of truth for "is this scanned/OAuth'd identity
    allowed in" — a callback looks up (tenant_id, provider, subject) here,
    never by scanning AdminUser.wecom_user_id directly. That legacy column
    is kept in sync as a read-only compatibility field only *after* a bind
    succeeds here (see `_bind_login_identity` in app/routers/auth.py),
    never the other way around, so it can never become a second, drifting
    source of truth for who is allowed to log in.
    """

    __tablename__ = "admin_login_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "subject",
            name="uq_login_identity_tenant_provider_subject",
        ),
        # At most one identity of a given provider per account. Nothing in
        # this ticket's scope needs an account to hold two WeCom identities;
        # relaxing this later needs its own tested reason, not a silent
        # side effect of some other change.
        UniqueConstraint(
            "admin_user_id", "provider",
            name="uq_login_identity_admin_user_provider",
        ),
        Index("ix_admin_login_identities_admin_user_id", "admin_user_id"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    provider = Column(Text, nullable=False)
    subject = Column(Text, nullable=False)
    admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdminAccessRequest(Base):
    """RND-321: a verified-but-unbound external identity asking for
    console access. No AdminUser exists for this yet — an owner/admin must
    explicitly link it to an existing account or create a new one before
    any session can ever be issued for it.

    The partial unique index enforces "one open request per identity" —
    repeat scans while pending find and return the same row — without
    blocking a *new* request once the previous one is resolved (linked or
    used to create an account), since by then the identity is bound and
    this code path is never reached for it again.
    """

    __tablename__ = "admin_access_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'resolved')",
            name="ck_admin_access_requests_status_valid",
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('linked', 'created')",
            name="ck_admin_access_requests_resolution_valid",
        ),
        Index("ix_admin_access_requests_tenant_status", "tenant_id", "status"),
        Index(
            "uq_admin_access_requests_pending_tenant_provider_subject",
            "tenant_id", "provider", "subject",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    provider = Column(Text, nullable=False)
    subject = Column(Text, nullable=False)
    # Human-review clues only — never used for automatic matching/binding.
    display_name = Column(Text, nullable=True)
    email_hint = Column(Text, nullable=True)
    status = Column(
        Enum("pending", "resolved", name="admin_access_request_status"),
        nullable=False,
        server_default=text("'pending'"),
    )
    resolution = Column(Text, nullable=True)
    resolved_admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=True)
    resolved_by_admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AuditLog(Base):
    """Immutable, append-only audit trail (RND-293 / A7-1).

    Records admin actions for compliance evidence. There is NO update/delete
    path at the application layer — rows are written once (A7-2) and read
    (A7-3) but never mutated. `detail` holds structured context only; it
    MUST NOT contain message bodies or decrypted payloads (SF-1).
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_id", "tenant_id"),
        Index("ix_audit_logs_admin_user_id", "admin_user_id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=False
    )
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=True, index=False
    )
    action = Column(Text, nullable=False)
    object_type = Column(Text, nullable=False)
    object_id = Column(Text, nullable=True)
    detail = Column(JSONB, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# —— RND-306 (B1-1) 平台超管实体（独立于 admin_users，tenant-less）——
class PlatformAdmin(Base):
    """Platform super-admin, isolated from per-tenant admin_users.

    Tenant-less by design: a platform admin operates across all tenants
    (cross-tenant scope is enforced at the auth layer, see B1-2 / RND-305).
    """

    __tablename__ = "platform_admins"
    __table_args__ = (
        UniqueConstraint("email", name="uq_platform_admins_email"),
        Index("ix_platform_admins_status", "status"),
    )

    id = Column(String(36), primary_key=True)
    email = Column(Text, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(
        Enum("superadmin", name="platform_admin_role"),
        nullable=False,
        server_default=text("'superadmin'"),
    )
    status = Column(
        Enum("active", "disabled", name="platform_admin_status"),
        nullable=False,
        server_default=text("'active'"),
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at = Column(DateTime(timezone=True), nullable=True)


# —— RND-278 (F0-3) 密码重置令牌 ——
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(String(36), primary_key=True)
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=False, index=True
    )
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    # Stores only the SHA-256 hex digest of the raw token. The raw token is
    # limited to the reset email link and the browser URL.
    token = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# —— RND-316 (C2-2) 导出安全审批令牌 ——
class ExportApprovalToken(Base):
    __tablename__ = "export_approval_tokens"

    id = Column(String(36), primary_key=True)
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=False, index=True
    )
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )
    # Only the SHA-256 hex digest is persisted; the raw token is returned once.
    token = Column(String(64), nullable=False, unique=True, index=True)
    # SHA-256 of canonical export parameters binds approval to one request.
    params_hash = Column(String(64), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class KeyVersion(Base):
    """Maps WeCom publickey_ver to the private key used for decryption."""

    __tablename__ = "key_versions"
    # Keep the standalone version lookup index from migration 0001 while
    # enforcing uniqueness inside, rather than across, tenant key spaces.
    __table_args__ = (
        Index("ix_key_versions_publickey_ver", "publickey_ver"),
        UniqueConstraint(
            "tenant_id", "publickey_ver", name="uq_key_versions_tenant_publickey_ver"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    publickey_ver = Column(Integer, nullable=False)
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
    # RND-211: the cursor remains the source of truth for incremental SDK
    # reads, while this separate version lets the console cheaply decide
    # whether a completed sync requires it to reload its view.
    status = Column(
        Enum("idle", "syncing", "error", name="sync_state_status"),
        nullable=False,
        default="idle",
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    seq_version = Column(Integer, nullable=False, default=0)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ReachabilityAuditRun(Base):
    """Tenant-scoped, aggregate-only reachability-check snapshot (RND-337)."""

    __tablename__ = "reachability_audit_runs"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_reachability_audit_runs_public_id"),
        CheckConstraint(
            "status IN ('checking', 'completed', 'incomplete', 'error')",
            name="ck_reachability_audit_runs_status_valid",
        ),
        CheckConstraint(
            "source IN ('manual', 'incremental', 'reconcile')",
            name="ck_reachability_audit_runs_source_valid",
        ),
        CheckConstraint(
            "matching_count >= 0 AND checked_count >= 0 AND reachable_count >= 0 "
            "AND unreachable_count >= 0",
            name="ck_reachability_audit_runs_counts_nonnegative",
        ),
        Index("ix_reachability_audit_runs_tenant_created", "tenant_id", "created_at"),
        # The database, rather than a process-local lock, makes a tenant's
        # active run idempotent across concurrent web processes.
        Index(
            "uq_reachability_audit_runs_tenant_checking",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'checking'"),
            sqlite_where=text("status = 'checking'"),
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    public_id = Column(String(36), nullable=False)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    status = Column(String(16), nullable=False, default="checking")
    source = Column(String(16), nullable=False, default="manual")
    algorithm_version = Column(String(32), nullable=False)
    scope_from = Column(DateTime(timezone=True), nullable=False)
    scope_to = Column(DateTime(timezone=True), nullable=False)
    # Internal-only candidate watermark. It is never exposed by API/CLI/logs.
    scope_max_message_id = Column(BigInteger, nullable=False, default=0)
    matching_count = Column(Integer, nullable=False, default=0)
    checked_count = Column(Integer, nullable=False, default=0)
    reachable_count = Column(Integer, nullable=False, default=0)
    unreachable_count = Column(Integer, nullable=False, default=0)
    reason_counts = Column(JSONB, nullable=False, default=dict)
    safe_error_code = Column(String(48), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReachabilityFinding(Base):
    """Internal-only lifecycle record for one classified visibility issue."""

    __tablename__ = "reachability_findings"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_reachability_findings_public_id"),
        UniqueConstraint(
            "tenant_id", "archive_message_id", "reason_code", "algorithm_version",
            name="uq_reachability_findings_identity",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "archive_message_id"],
            ["archive_messages.tenant_id", "archive_messages.id"],
            name="fk_reachability_findings_tenant_message",
        ),
        CheckConstraint(
            "status IN ('active', 'resolved')",
            name="ck_reachability_findings_status_valid",
        ),
        CheckConstraint(
            "occurrence_count >= 0",
            name="ck_reachability_findings_occurrences_nonnegative",
        ),
        Index(
            "ix_reachability_findings_tenant_status_last_seen",
            "tenant_id", "status", "last_seen", "id",
        ),
        Index(
            "ix_reachability_findings_tenant_message_active",
            "tenant_id", "archive_message_id", "status",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    public_id = Column(String(36), nullable=False)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    # This is the deliberately internal-only message reference. It must never
    # be serialized, logged, used in a cursor, or accepted from a request.
    archive_message_id = Column(BigInteger, nullable=False)
    reason_code = Column(String(96), nullable=False)
    status = Column(String(16), nullable=False, default="active")
    first_seen = Column(DateTime(timezone=True), nullable=False)
    last_seen = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    occurrence_count = Column(Integer, nullable=False, default=0)
    first_run_id = Column(BigInteger, ForeignKey("reachability_audit_runs.id"), nullable=False)
    last_run_id = Column(BigInteger, ForeignKey("reachability_audit_runs.id"), nullable=False)
    algorithm_version = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
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
        # RND-191: pg_trgm GIN index so search_messages' ILIKE '%term%'
        # predicate (app/routers/search.py) is index-backed -- the FTS GIN
        # index above only supports to_tsvector(...) @@ to_tsquery(...)
        # matching, never a plain ILIKE substring predicate, so ILIKE was
        # falling back to a sequential scan. Same substring/case-
        # insensitive matching semantics as before; only the query plan
        # changes. See migration 0013.
        Index(
            "ix_archive_messages_content_text_trgm",
            "content_text",
            postgresql_using="gin",
            postgresql_ops={"content_text": "gin_trgm_ops"},
        ),
        # RND-191: tenant_id-leading composite indexes for the hot,
        # always-tenant-scoped query shapes -- see migration 0013's
        # docstring for why a composite beats intersecting single-column
        # indexes here.
        Index("ix_archive_messages_tenant_msgtime_id", "tenant_id", "msgtime", "id"),
        Index("ix_archive_messages_tenant_roomid", "tenant_id", "roomid"),
        Index(
            "ix_archive_messages_tenant_decrypt_revoked",
            "tenant_id",
            "decrypt_status",
            "is_revoked",
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


class GroupChatMetadata(Base):
    """Tenant-scoped current metadata for an archived WeCom group chat.

    ``roomid`` remains the immutable archive correlation key.  ``display_name``
    is only a validated current name from the customer-group API; no message
    payload, roster, or name history is retained here.
    """

    __tablename__ = "group_chat_metadata"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "roomid", name="uq_group_chat_metadata_tenant_roomid"
        ),
        Index("ix_group_chat_metadata_tenant_roomid", "tenant_id", "roomid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    roomid = Column(String(64), nullable=False)
    display_name = Column(Text, nullable=True)
    source = Column(String(64), nullable=False, default="wecom_external_groupchat")
    sync_status = Column(String(32), nullable=False, default="unresolved")
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ArchiveMessageRecipient(Base):
    """
    Per-receiver lookup rows derived from archive_messages.tolist.

    One row per (message, receiver) pair.  Populated alongside the parent
    archive_messages row so that queries like "find all messages received by
    user X" can use a plain indexed B-tree lookup instead of JSONB containment.
    """

    __tablename__ = "archive_message_recipients"
    __table_args__ = (
        # RND-191: composite index for the recipient-side membership/
        # participation lookups (tenant_id + receiver_userid always filter
        # together) -- see migration 0013.
        Index(
            "ix_archive_message_recipients_tenant_receiver",
            "tenant_id",
            "receiver_userid",
        ),
    )

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
        CheckConstraint(
            "download_status != 'downloaded' OR file_size IS NOT NULL",
            name="ck_media_files_downloaded_requires_file_size",
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
    # RND-172: durable event-sweep retry budget. Incremented before every
    # SDK attempt so restarts cannot reset a failing item's retry count.
    download_attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    migration_status = Column(String(16), nullable=True, index=True)
    migration_attempted_at = Column(DateTime(timezone=True), nullable=True)
    migration_error = Column(Text, nullable=True)
    bucket = Column(String(128), nullable=True)
    mime_type = Column(String(128), nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    # RND-207 (migration 0012): list/timeline thumbnail metadata for image
    # media. thumbnail_ref is a derived object key / local path held in the
    # SAME storage backend as the original (storage_backend); NULL means
    # "serve the original". image_width/image_height are the original's
    # post-EXIF pixel dimensions, used only to reserve an aspect-ratio box in
    # the list. thumbnail_status/thumbnail_attempted_at/thumbnail_error are
    # backfill bookkeeping mirroring the migration_* columns above: no
    # serving path depends on them, and thumbnail_error is a short sanitized
    # tag only — never a raw path/key/exception.
    thumbnail_ref = Column(Text, nullable=True)
    image_width = Column(Integer, nullable=True)
    image_height = Column(Integer, nullable=True)
    thumbnail_status = Column(String(16), nullable=True, index=True)
    thumbnail_attempted_at = Column(DateTime(timezone=True), nullable=True)
    thumbnail_error = Column(Text, nullable=True)
    # RND-258: browser-playable derivative for voice / audio_archive media.
    # The original archival object remains untouched and downloadable.
    playback_ref = Column(Text, nullable=True)
    playback_status = Column(
        String(32),
        nullable=True,
        default="not_applicable",
        server_default=text("'not_applicable'"),
        index=True,
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


class TenantStorageDaily(Base):
    """Daily materialized media-byte total for one tenant (RND-331).

    The rollup service is the sole writer. ``used_bytes`` is calculated only
    from MediaFile rows whose download_status is ``downloaded``.
    """

    __tablename__ = "tenant_storage_daily"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "usage_date", name="uq_tenant_storage_daily_tenant_date"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    usage_date = Column(Date, nullable=False)
    used_bytes = Column(BigInteger, nullable=False)
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


class RetentionLock(Base):
    """One immutable retention-lock record per expired archive message."""

    __tablename__ = "retention_locks"
    __table_args__ = (
        UniqueConstraint(
            "archive_message_id", name="uq_retention_locks_archive_message_id"
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    archive_message_id = Column(
        Integer, ForeignKey("archive_messages.id"), nullable=False
    )
    locked_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Contact(Base):
    """Lightweight registry of WeCom user identities seen in the archive."""

    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "wecom_userid", name="uq_contacts_tenant_wecom_userid"
        ),
        # RND-191: pg_trgm GIN indexes so search_contacts' ILIKE '%term%'
        # predicates (app/routers/search.py) are index-backed instead of a
        # sequential scan. See migration 0013.
        Index(
            "ix_contacts_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_contacts_wecom_userid_trgm",
            "wecom_userid",
            postgresql_using="gin",
            postgresql_ops={"wecom_userid": "gin_trgm_ops"},
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


class ExternalContact(Base):
    """WeCom external contact, populated by the external-contact API."""

    __tablename__ = "external_contacts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "external_userid",
            name="uq_external_contacts_tenant_ext_userid",
        ),
        Index(
            "ix_external_contacts_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_external_contacts_current_nickname_trgm",
            "current_nickname_normalized",
            postgresql_using="gin",
            postgresql_ops={"current_nickname_normalized": "gin_trgm_ops"},
        ),
        Index(
            "ix_external_contacts_company_trgm",
            "company",
            postgresql_using="gin",
            postgresql_ops={"company": "gin_trgm_ops"},
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    external_userid = Column(String(64), nullable=False)
    # Compatibility display label from RND-287. It can contain an employee
    # remark and is deliberately not repurposed as the customer's real name.
    name = Column(Text, nullable=True)
    # The current WeCom self-chosen nickname is a separate customer-level
    # identity field. The raw value remains auditable; normalized/display
    # values keep comparison and rendering safe.
    current_nickname_raw = Column(Text, nullable=True)
    current_nickname_normalized = Column(Text, nullable=True)
    current_nickname_display = Column(Text, nullable=True)
    current_nickname_observed_at = Column(DateTime(timezone=True), nullable=True)
    company = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    source = Column(Text, nullable=True)
    owner_wecom_userid = Column(String(64), nullable=True)
    last_interaction_at = Column(DateTime(timezone=True), nullable=True)
    message_count = Column(Integer, nullable=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
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


class ExternalContactRefreshTask(Base):
    """Durable, coalesced external-contact metadata refresh request.

    The task contains only the bounded external identifier needed to call the
    WeCom API. It is intentionally separate from ``external_contacts`` so an
    incoming direct message can request a lookup even when WeCom has not yet
    made that user a readable external-contact relationship.
    """

    __tablename__ = "external_contact_refresh_tasks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "external_userid",
            name="uq_external_contact_refresh_tasks_tenant_external_userid",
        ),
        Index(
            "ix_external_contact_refresh_tasks_tenant_ready",
            "tenant_id",
            "state",
            "next_attempt_at",
            "id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    external_userid = Column(String(64), nullable=False)
    source = Column(String(32), nullable=False)
    state = Column(String(16), nullable=False, server_default=text("'pending'"))
    attempt_count = Column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_error_class = Column(String(32), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExternalContactFollow(Base):
    """One tenant-scoped employee-to-external-contact follow relationship."""

    __tablename__ = "external_contact_follows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "external_userid"],
            ["external_contacts.tenant_id", "external_contacts.external_userid"],
            name="fk_external_contact_follows_tenant_contact",
        ),
        UniqueConstraint(
            "tenant_id",
            "external_userid",
            "follow_userid",
            name="uq_external_contact_follows_tenant_contact_user",
        ),
        Index(
            "ix_external_contact_follows_tenant_contact_active",
            "tenant_id",
            "external_userid",
            "is_active",
        ),
        Index(
            "ix_external_contact_follows_remark_trgm",
            "remark_normalized",
            postgresql_using="gin",
            postgresql_ops={"remark_normalized": "gin_trgm_ops"},
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    external_userid = Column(String(64), nullable=False)
    follow_userid = Column(String(64), nullable=False)
    remark_raw = Column(Text, nullable=True)
    remark_normalized = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    observed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExternalContactNicknameHistory(Base):
    """Observed transitions of a customer's own WeCom nickname."""

    __tablename__ = "external_contact_nickname_history"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "external_userid"],
            ["external_contacts.tenant_id", "external_contacts.external_userid"],
            name="fk_external_contact_nickname_history_tenant_contact",
        ),
        Index(
            "ix_external_contact_nickname_history_tenant_contact_observed",
            "tenant_id",
            "external_userid",
            "observed_at",
            "id",
        ),
        Index(
            "ix_external_contact_nickname_history_old_trgm",
            "old_nickname_normalized",
            postgresql_using="gin",
            postgresql_ops={"old_nickname_normalized": "gin_trgm_ops"},
        ),
        Index(
            "ix_external_contact_nickname_history_new_trgm",
            "new_nickname_normalized",
            postgresql_using="gin",
            postgresql_ops={"new_nickname_normalized": "gin_trgm_ops"},
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    external_userid = Column(String(64), nullable=False)
    old_nickname_raw = Column(Text, nullable=True)
    old_nickname_normalized = Column(Text, nullable=True)
    old_nickname_display = Column(Text, nullable=True)
    new_nickname_raw = Column(Text, nullable=True)
    new_nickname_normalized = Column(Text, nullable=True)
    new_nickname_display = Column(Text, nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
