#!/usr/bin/env python3
"""
One-shot decrypt and normalise pipeline for WeCom archived text messages.

Loads environment, resolves the active tenant for WECOM_CORP_ID,
initialises the WeCom Finance SDK, then delegates the actual decrypt loop
to app.services.decrypt_worker.run_decrypt_once() (RND-222) — this script
is now only the CLI shell: env/argument parsing, tenant resolution, SDK
lifecycle, and translating the returned summary (or a raised
DecryptCommitError) into the documented print/exit-code contract below.
See app/services/decrypt_worker.py's module docstring for the core loop
itself, including the RND-222 tenant-scope audit fix.

Only handles text (msgtype="text") messages.  All other msgtypes are counted
as skipped/unsupported.  Media download, image/audio/video handling, and AI
features are out of scope.

Idempotent — re-running re-processes records that still have decrypt_status
"pending" or "failed".

Usage (from backend/):
    python scripts/decrypt_wecom_messages_once.py

Required environment variables:
    DATABASE_URL              PostgreSQL connection string
    WECOM_SDK_LIB_PATH        Absolute path to libWeWorkFinanceSdk_C.so
    WECOM_CORP_ID             WeCom corporation ID
    WECOM_ARCHIVE_SECRET      WeCom conversation archive secret
    WECOM_PRIVATE_KEY_PATH    Absolute path to RSA private key PEM file
    WECOM_PUBLIC_KEY_VERSION  Expected publickey_ver for current private key

Exit codes:
    0  Success (all records processed, possibly some failed)
    1  Fatal initialisation failure (env, SDK load, DB connect, no active
       tenant for WECOM_CORP_ID)

Safety constraints:
    - No decrypted message content is printed.
    - No encrypted payloads are printed.
    - No private keys, secrets, or raw customer data is printed.
    - No decrypted encrypt_key is printed.
"""

from __future__ import annotations

import os
import sys

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.db.models import TenantWecomConfig
from app.sdk import wecom_sdk
from app.services.decrypt_worker import (  # noqa: F401 -- re-exported for backward-compat imports
    DecryptCommitError,
    _decrypt_message,
    _normalise_fields,
    _rsa_decrypt_encrypt_key,
    _upsert_recipients,
    build_missing_recipient_repair_query,
    repair_missing_recipients,
    run_decrypt_once,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


def _configure_sqlite_for_savepoints_if_needed(engine) -> None:
    """app.revoke_reconciliation.reconcile_revoke_event() uses
    Session.begin_nested() (a SAVEPOINT) for conflict-safe inserts.
    Production always runs this against Postgres, which needs no special
    handling -- but pysqlite's own implicit transaction management can
    make a SAVEPOINT RELEASE behave like a premature commit of the outer
    transaction unless SQLAlchemy's documented sqlite recipe is applied.
    A no-op for every other dialect (see
    scripts/backfill_revoke_associations_once.py for the identical
    helper -- kept duplicated rather than shared to avoid adding an
    import-time dependency between the two standalone scripts)."""
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _do_connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _do_begin(conn):
        conn.exec_driver_sql("BEGIN")


# ---------------------------------------------------------------------------
# RSA key loading (stays in the shell -- env/file I/O, not core decrypt logic)
# ---------------------------------------------------------------------------


def _load_private_key(pem_path: str) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM file path.

    The WeCom C SDK demo uses PKCS#1 v1.5 padding for RSA decryption of
    encrypt_random_key.  Returns the private key object.

    Raises: FileNotFoundError, ValueError, cryptography exceptions.
    """
    with open(pem_path, "rb") as f:
        pem_data = f.read()
    return serialization.load_pem_private_key(pem_data, password=None)


# ---------------------------------------------------------------------------
# Tenant resolution
# ---------------------------------------------------------------------------


def _require_tenant_id(session: Session, corp_id: str) -> str:
    """Return the active tenant_id for *corp_id*, or exit 1.

    Exits non-zero if the tenant_wecom_configs table is absent (migration
    0002 not applied), no active row matches the corp, or any DB error
    occurs. Decrypt must never proceed without a valid tenant (RND-222
    tenant-scope audit fix — see app/services/decrypt_worker.py's module
    docstring) — caller must not handle SystemExit.
    """
    try:
        row = (
            session.query(TenantWecomConfig)
            .filter(
                TenantWecomConfig.corp_id == corp_id,
                TenantWecomConfig.is_active == True,  # noqa: E712
            )
            .first()
        )
    except Exception as exc:
        print(
            f"[FAIL] Tenant resolution DB error ({type(exc).__name__}). "
            "Run alembic upgrade head and bootstrap_default_tenant.py first.",
            flush=True,
        )
        sys.exit(1)

    if row is None:
        print(
            "[FAIL] No active tenant found for this corp. "
            "Run bootstrap_default_tenant.py after migration 0002.",
            flush=True,
        )
        sys.exit(1)

    return row.tenant_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # --- 1. Load environment ---
    database_url = _require_env("DATABASE_URL")
    lib_path = _require_env("WECOM_SDK_LIB_PATH")
    corp_id = _require_env("WECOM_CORP_ID")
    secret = _require_env("WECOM_ARCHIVE_SECRET")
    private_key_path = _require_env("WECOM_PRIVATE_KEY_PATH")
    expected_pubkey_ver_str = _require_env("WECOM_PUBLIC_KEY_VERSION")

    try:
        expected_pubkey_ver = int(expected_pubkey_ver_str)
    except ValueError:
        print(
            f"[FAIL] WECOM_PUBLIC_KEY_VERSION is not a valid integer: "
            f"{expected_pubkey_ver_str!r}",
            flush=True,
        )
        sys.exit(1)

    # --- 2. Load RSA private key ---
    try:
        private_key = _load_private_key(private_key_path)
    except FileNotFoundError:
        print(
            f"[FAIL] Private key file not found: {os.path.basename(private_key_path)}",
            flush=True,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"[FAIL] Failed to load private key: {exc}", flush=True)
        sys.exit(1)

    # --- 3. Resolve tenant (before SDK init — RND-222 tenant-scope audit
    # fix; fail fast, matching sync_wecom_archive_once.py and
    # download_wecom_media_once.py's existing tenant-first ordering) ---
    engine = create_engine(database_url)
    _configure_sqlite_for_savepoints_if_needed(engine)

    with Session(engine) as session:
        tenant_id: str = _require_tenant_id(session, corp_id)

    # --- 4. Initialise WeCom SDK ---
    try:
        lib = wecom_sdk.load_sdk(lib_path)
    except FileNotFoundError as exc:
        print(f"[FAIL] {exc}", flush=True)
        sys.exit(1)
    except OSError as exc:
        print(f"[FAIL] Failed to load SDK library: {exc}", flush=True)
        sys.exit(1)

    try:
        wecom_sdk.configure_sdk(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK missing expected symbol (Init path): {exc}", flush=True)
        sys.exit(1)

    try:
        wecom_sdk.configure_sdk_get_chat_data(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK missing expected symbol (GetChatData path): {exc}", flush=True)
        sys.exit(1)

    try:
        wecom_sdk.configure_sdk_decrypt_data(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK missing expected symbol (DecryptData path): {exc}", flush=True)
        sys.exit(1)

    handle = wecom_sdk.new_sdk(lib)
    if not handle:
        print("[FAIL] NewSdk() returned a null handle", flush=True)
        sys.exit(1)

    init_ret = wecom_sdk.init_sdk(lib, handle, corp_id, secret)
    if init_ret != 0:
        print(f"[FAIL] Init() failed (return code {init_ret})", flush=True)
        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception:
            pass
        sys.exit(1)

    # --- 5. Run the decrypt core loop ---
    with Session(engine) as session:
        try:
            summary = run_decrypt_once(
                session,
                tenant_id,
                lib,
                private_key,
                expected_pubkey_ver,
                lib_path=lib_path,
            )
        except DecryptCommitError as exc:
            print(f"[FAIL] Database commit failed: {exc}", flush=True)
            sys.exit(1)

    # --- 6. Cleanup SDK ---
    try:
        wecom_sdk.destroy_sdk(lib, handle)
    except Exception:
        pass

    # --- 7. Print safe operational metrics ---
    print(f"[INFO] decrypt scanned: {summary.scanned}", flush=True)
    print(f"[INFO] decrypt success: {summary.success}", flush=True)
    print(f"[INFO] decrypt failed: {summary.failed}", flush=True)
    print(f"[INFO] decrypt skipped_unsupported: {summary.unsupported}", flush=True)
    print(f"[INFO] decrypt pending_remaining: {summary.pending_remaining}", flush=True)
    if summary.key_mismatch:
        print(f"[INFO] decrypt key_version_mismatch: {summary.key_mismatch}", flush=True)
    if summary.rsa_failed:
        print(f"[INFO] decrypt rsa_decrypt_failed: {summary.rsa_failed}", flush=True)
    if summary.sigsegv:
        print(f"[INFO] decrypt sigsegv: {summary.sigsegv}", flush=True)
    if summary.isolation_other:
        print(f"[INFO] decrypt isolation_other: {summary.isolation_other}", flush=True)
    if summary.malformed_input:
        print(f"[INFO] decrypt malformed_input: {summary.malformed_input}", flush=True)
    if summary.recipient_upsert_failed:
        print(
            f"[INFO] decrypt recipient_upsert_failed: {summary.recipient_upsert_failed}",
            flush=True,
        )
    if summary.recipients_repaired:
        print(
            f"[INFO] decrypt recipients_repaired: {summary.recipients_repaired}",
            flush=True,
        )
    if summary.revoke_event_seen:
        print(f"[INFO] decrypt revoke_events_seen: {summary.revoke_event_seen}", flush=True)
    if summary.revoke_reconcile_failed:
        print(
            f"[INFO] decrypt revoke_reconcile_failed: {summary.revoke_reconcile_failed}",
            flush=True,
        )
    if summary.revocations_reconciled:
        print(
            f"[INFO] decrypt revocations_reconciled: {summary.revocations_reconciled}",
            flush=True,
        )
    # Safe return-code diagnostic (e.g. "ret_0=3, ret_90002=1")
    if summary.return_codes:
        sorted_codes = sorted(summary.return_codes.items())
        rc_diag = ", ".join(f"ret_{k}={v}" for k, v in sorted_codes)
        print(f"[INFO] decrypt return_codes: {rc_diag}", flush=True)
    print("[PASS] decrypt_wecom_messages_once completed", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
