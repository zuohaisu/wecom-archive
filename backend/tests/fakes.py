"""
Shared fakes for worker service tests (RND-222).

Provides:
  - FakeWecomSdk: a programmable stand-in for the app.sdk.wecom_sdk module
    surface used by app.services.{sync,decrypt,media}_worker — load_sdk,
    configure_sdk*, new_sdk, init_sdk, destroy_sdk, new_slice, free_slice,
    get_chat_data, get_slice_len, get_content_from_slice, decrypt_data,
    iter_media_chunks. Worker services accept an `sdk` parameter that
    defaults to the real module; passing a FakeWecomSdk() instance instead
    exercises the exact same call sites with no real ctypes library, no
    real private key, and no real WeCom network call.
  - install_fake_sdk(): monkeypatches every one of those attributes onto a
    real module object (app.sdk.wecom_sdk, or any module that re-exports
    it, e.g. a script's own `wecom_sdk` name) so call sites that reach the
    SDK through the real module directly — app.media_download.download_one
    is the one such call site in this codebase, see its docstring — see
    the fake's behavior too, not just service call sites that accept an
    explicit `sdk` parameter.
  - A hand-written sqlite schema (mirrors tests/test_reachability_audit.py's
    technique) covering every table the three worker services touch:
    tenants, tenant_wecom_configs, sync_states, archive_messages,
    archive_message_recipients, message_revocations, media_files.
    archive_messages/archive_message_recipients use Postgres JSONB columns
    in production; sqlite's DDL compiler rejects JSONB, so the DDL here
    uses TEXT while DML still goes through the real ORM model classes
    (SQLAlchemy's JSONB type handles bind/result processing generically,
    independent of dialect-specific DDL support).
  - _TENANT_A / _TENANT_B constants and insert_* helpers for multi-tenant
    isolation tests.

Run any test file importing this module (from backend/):
    pytest tests/test_decrypt_worker_service.py -v
"""

from __future__ import annotations

import base64
import json
from typing import Optional

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import (
    ArchiveMessage,
    ArchiveMessageRecipient,
    SyncState,
    TenantWecomConfig,
)

_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"


# ---------------------------------------------------------------------------
# RSA test fixtures — decrypt_worker's _rsa_decrypt_encrypt_key() is
# exercised for real (never mocked) so tests catch a real PKCS1v15
# mismatch; only the C SDK ctypes boundary (FakeWecomSdk) and the DB are
# faked.
# ---------------------------------------------------------------------------


def generate_test_rsa_keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def rsa_encrypt_key_b64(public_key: rsa.RSAPublicKey, plaintext: str) -> str:
    """Mirror WeCom's own encoding of encrypt_random_key: PKCS1v15-encrypt
    then base64. Matches decrypt_worker._rsa_decrypt_encrypt_key's decode
    side exactly."""
    ciphertext = public_key.encrypt(plaintext.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(ciphertext).decode("ascii")


def write_private_key_pem(private_key: rsa.RSAPrivateKey, path) -> None:
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)


# ---------------------------------------------------------------------------
# FakeWecomSdk
# ---------------------------------------------------------------------------


class FakeWecomSdk:
    """Programmable replacement for app.sdk.wecom_sdk in worker service tests.

    Every method mirrors the real module function's signature (lib/handle
    first, matching how services call `sdk.xxx(lib, ...)` — see
    app/sdk/wecom_sdk.py). State (init_calls, destroyed, freed_slices) is
    inspectable after a run for assertions.
    """

    def __init__(self) -> None:
        self.lib = "fake-lib"
        self.handle = "fake-handle"
        self.init_return_code = 0
        self.destroyed = False
        self.init_calls: list[tuple[str, str]] = []
        self.freed_slices = 0

        self._chat_data_ret = 0
        self._chat_data_records: list[dict] = []
        self._pending_content: Optional[bytes] = None

        self._decrypt_responses: dict[str, tuple[int, Optional[dict]]] = {}
        self._media_chunks: dict[str, object] = {}

    # ---- lifecycle ----------------------------------------------------

    def load_sdk(self, lib_path: str):
        return self.lib

    def configure_sdk(self, lib) -> None:
        pass

    def configure_sdk_get_chat_data(self, lib) -> None:
        pass

    def configure_sdk_decrypt_data(self, lib) -> None:
        pass

    def configure_sdk_media_data(self, lib) -> None:
        pass

    def new_sdk(self, lib):
        return self.handle

    def init_sdk(self, lib, handle, corp_id: str, secret: str) -> int:
        self.init_calls.append((corp_id, secret))
        return self.init_return_code

    def destroy_sdk(self, lib, handle) -> None:
        self.destroyed = True

    # ---- slice plumbing shared by GetChatData / DecryptData -----------

    def new_slice(self, lib):
        return "fake-slice"

    def free_slice(self, lib, slice_ptr) -> None:
        self.freed_slices += 1

    def get_slice_len(self, lib, slice_ptr) -> int:
        return len(self._pending_content) if self._pending_content else 0

    def get_content_from_slice(self, lib, slice_ptr):
        return self._pending_content

    # ---- sync: GetChatData ---------------------------------------------

    def set_chat_data(self, records: list, ret: int = 0) -> None:
        """Program the next get_chat_data() call's return code and the
        chatdata records visible through get_slice_len/get_content_from_slice."""
        self._chat_data_ret = ret
        self._chat_data_records = records

    def get_chat_data(
        self, lib, handle, slice_ptr, seq, limit, proxy="", passwd="", timeout=5
    ) -> int:
        self._pending_content = json.dumps(
            {"chatdata": self._chat_data_records}
        ).encode("utf-8")
        return self._chat_data_ret

    # ---- decrypt: DecryptData -------------------------------------------

    def set_decrypt_response(
        self, encrypt_msg: str, decrypted: Optional[dict], ret: int = 0
    ) -> None:
        """Program decrypt_data()'s response for a given encrypt_chat_msg
        value. decrypted=None simulates a failed/empty decrypt."""
        self._decrypt_responses[encrypt_msg] = (ret, decrypted)

    def decrypt_data(self, lib, encrypt_key: str, encrypt_msg: str, slice_ptr) -> int:
        ret, decrypted = self._decrypt_responses.get(encrypt_msg, (0, None))
        self._pending_content = (
            json.dumps(decrypted).encode("utf-8") if decrypted is not None else None
        )
        return ret

    # ---- media: iter_media_chunks --------------------------------------

    def set_media_chunks(self, sdkfileid: str, chunks: list) -> None:
        self._media_chunks[sdkfileid] = list(chunks)

    def set_media_error(self, sdkfileid: str, exc: Exception) -> None:
        self._media_chunks[sdkfileid] = exc

    def iter_media_chunks(
        self, lib, handle, sdkfileid, proxy="", passwd="", timeout=30, max_chunks=10_000
    ):
        configured = self._media_chunks.get(sdkfileid)
        if isinstance(configured, Exception):
            raise configured
        for chunk in configured or []:
            yield chunk


_PATCHED_SDK_ATTRS = (
    "load_sdk",
    "configure_sdk",
    "configure_sdk_get_chat_data",
    "configure_sdk_decrypt_data",
    "configure_sdk_media_data",
    "new_sdk",
    "init_sdk",
    "destroy_sdk",
    "new_slice",
    "free_slice",
    "get_slice_len",
    "get_content_from_slice",
    "get_chat_data",
    "decrypt_data",
    "iter_media_chunks",
)


def install_fake_sdk(monkeypatch: pytest.MonkeyPatch, fake: FakeWecomSdk, module) -> None:
    """Patch every wecom_sdk attribute *module* exposes so call sites that
    reach the SDK through that module directly (not via an injected `sdk`
    parameter — app.media_download.download_one is the one such call site,
    see its docstring) see *fake*'s behavior too."""
    for name in _PATCHED_SDK_ATTRS:
        monkeypatch.setattr(module, name, getattr(fake, name))


# ---------------------------------------------------------------------------
# sqlite-backed test schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE tenants (
    id TEXT PRIMARY KEY, name TEXT, slug TEXT, is_active INTEGER,
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    onboarding_completed_at TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE billing_plans (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    billing_period_months INTEGER NOT NULL,
    storage_quota_bytes INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE plan_entitlements (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plan_id, capability)
);
CREATE TABLE subscriptions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL UNIQUE,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL,
    starts_at DATETIME NOT NULL,
    ends_at DATETIME NOT NULL,
    source TEXT NOT NULL,
    renewal_count INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE tenant_wecom_configs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    corp_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    app_secret TEXT NOT NULL,
    callback_domain TEXT NOT NULL DEFAULT '',
    private_key_encrypted TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE sync_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corp_id TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle',
    started_at DATETIME,
    error_message TEXT,
    seq_version INTEGER NOT NULL DEFAULT 0,
    tenant_id TEXT,
    updated_at TEXT,
    UNIQUE(tenant_id, corp_id)
);
CREATE TABLE archive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid TEXT NOT NULL,
    seq INTEGER NOT NULL,
    publickey_ver INTEGER NOT NULL,
    raw_encrypted_payload TEXT,
    encrypt_random_key TEXT NOT NULL,
    encrypt_chat_msg TEXT NOT NULL,
    decrypt_status TEXT NOT NULL DEFAULT 'pending',
    decrypted_payload TEXT,
    structured_content TEXT,
    content_text TEXT,
    msgtype TEXT,
    sender TEXT,
    roomid TEXT,
    msgtime INTEGER,
    tolist TEXT,
    sdkfileid TEXT,
    is_revoked INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT,
    tenant_id TEXT,
    created_at TEXT
);
CREATE TABLE message_revocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT,
    revoke_event_message_id INTEGER NOT NULL,
    revoke_event_msgid TEXT NOT NULL,
    revoke_event_msgtime INTEGER,
    target_msgid TEXT,
    original_message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    UNIQUE(tenant_id, revoke_event_message_id)
);
CREATE TABLE archive_message_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    receiver_userid TEXT NOT NULL,
    receiver_type TEXT,
    tenant_id TEXT,
    created_at TEXT
);
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sdkfileid TEXT NOT NULL,
    archive_message_id INTEGER NOT NULL,
    file_type TEXT,
    local_path TEXT,
    oss_key TEXT,
    storage_backend TEXT,
    storage_ref TEXT,
    file_size INTEGER,
    download_status TEXT NOT NULL DEFAULT 'pending',
    download_attempts INTEGER NOT NULL DEFAULT 0,
    migration_status TEXT,
    migration_attempted_at TEXT,
    migration_error TEXT,
    bucket TEXT,
    mime_type TEXT,
    checksum_sha256 TEXT,
    thumbnail_ref TEXT,
    image_width INTEGER,
    image_height INTEGER,
    thumbnail_status TEXT,
    thumbnail_attempted_at TEXT,
    thumbnail_error TEXT,
    playback_ref TEXT,
    playback_status TEXT,
    tenant_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(tenant_id, sdkfileid)
);
CREATE TABLE media_quota_blocks (
    media_file_id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    observed_bytes INTEGER NOT NULL,
    reason TEXT NOT NULL,
    blocked_at DATETIME NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE tenant_storage_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    usage_date DATE NOT NULL,
    used_bytes INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tenant_id, usage_date)
);
"""


def configure_sqlite_for_savepoints(engine) -> None:
    """Apply SQLAlchemy's documented pysqlite recipe for real SAVEPOINT
    support, needed by app.revoke_reconciliation's Session.begin_nested()
    (see tests/test_reachability_audit.py's identical helper for the full
    rationale). A no-op would make a SAVEPOINT RELEASE behave like a
    premature commit of the outer transaction under pysqlite's own
    implicit transaction handling — irrelevant for Postgres (production)."""

    @event.listens_for(engine, "connect")
    def _do_connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _do_begin(conn):
        conn.exec_driver_sql("BEGIN")


def make_worker_engine(configure_savepoints: bool = True):
    """configure_savepoints=False for CLI-equivalence tests that monkeypatch
    a shell's own create_engine() to return this engine — the shell
    (scripts/decrypt_wecom_messages_once.py,
    scripts/backfill_revoke_associations_once.py) applies the identical
    sqlite savepoint recipe itself right after creating the engine;
    applying it twice registers duplicate "begin" listeners, which double-
    issues `BEGIN` and raises OperationalError."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    if configure_savepoints:
        configure_sqlite_for_savepoints(engine)
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    return engine


@pytest.fixture()
def worker_db():
    engine = make_worker_engine()
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture()
def worker_engine():
    """Like worker_db, but yields the engine itself, without the sqlite
    savepoint recipe pre-applied — for CLI-equivalence tests exercising a
    shell's own create_engine()/_configure_sqlite_for_savepoints_if_needed
    lifecycle (see make_worker_engine's configure_savepoints docstring)."""
    engine = make_worker_engine(configure_savepoints=False)
    yield engine


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------


def insert_tenant(db: Session, tenant_id: str = _TENANT_A) -> None:
    db.execute(
        text(
            "INSERT INTO tenants (id, name, slug, is_active) VALUES (:id, :id, :id, 1)"
        ),
        {"id": tenant_id},
    )
    db.commit()


def insert_tenant_wecom_config(
    db: Session,
    tenant_id: str,
    corp_id: str,
    is_active: bool = True,
) -> TenantWecomConfig:
    config = TenantWecomConfig(
        id=f"cfg-{tenant_id}-{corp_id}",
        tenant_id=tenant_id,
        corp_id=corp_id,
        agent_id="1000001",
        app_secret="secret",
        is_active=is_active,
    )
    db.add(config)
    db.commit()
    return config


def insert_sync_state(
    db: Session, tenant_id: str, corp_id: str, last_seq: int = 0
) -> SyncState:
    row = SyncState(tenant_id=tenant_id, corp_id=corp_id, last_seq=last_seq)
    db.add(row)
    db.commit()
    return row


def insert_archive_message(db: Session, **kwargs) -> ArchiveMessage:
    defaults = dict(
        seq=1,
        publickey_ver=1,
        encrypt_random_key="x",
        encrypt_chat_msg="y",
        decrypt_status="success",
        tenant_id=_TENANT_A,
    )
    defaults.update(kwargs)
    defaults.setdefault("msgid", f"msg-{defaults['seq']}-{id(defaults)}")
    msg = ArchiveMessage(**defaults)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def insert_recipient(
    db: Session, message_id: int, receiver_userid: str, tenant_id: str = _TENANT_A
) -> ArchiveMessageRecipient:
    r = ArchiveMessageRecipient(
        message_id=message_id,
        receiver_userid=receiver_userid,
        receiver_type="user",
        tenant_id=tenant_id,
    )
    db.add(r)
    db.commit()
    return r
