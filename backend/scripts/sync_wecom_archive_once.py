#!/usr/bin/env python3
"""
One-shot real WeCom archive sync worker.

Loads environment, resolves the active tenant for WECOM_CORP_ID,
initialises the WeCom Finance SDK, then delegates the actual GetChatData
call and record persistence to
app.services.sync_worker.run_sync_once() (RND-222) — this script is now
only the CLI shell: env parsing, tenant resolution, SDK/slice lifecycle,
and translating the returned summary into the documented print/exit-code
contract below. See app/services/sync_worker.py's module docstring for
the core loop itself.

Idempotent — re-running never creates duplicate rows (keyed on (tenant_id, msgid)).

Usage (from backend/):
    python scripts/sync_wecom_archive_once.py

Required environment variables:
    DATABASE_URL          PostgreSQL connection string
    WECOM_SDK_LIB_PATH    Absolute path to libWeWorkFinanceSdk_C.so

    Either the per-tenant mode:
    WECOM_TENANT_ID       Resolves the active tenant config row and uses its
                          stored CorpID / archive secret (FIELD_ENCRYPTION_KEY
                          must be set), or the legacy single-corp mode:
    WECOM_CORP_ID         WeCom corporation ID
    WECOM_ARCHIVE_SECRET  WeCom conversation archive secret

Optional environment variables:
    WECOM_CHAT_LIMIT      Max records to fetch per call (default: 500)

Exit codes:
    0  Success
    1  Any failure

Safety constraints:
    - No message content, encrypted payload, secrets, private keys, or raw
      customer data is printed.
    - No decryption.
    - No media download.
"""

from __future__ import annotations

import os
import sys

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import TenantWecomConfig
from app.sdk import wecom_sdk
from app.services.sync_worker import (  # noqa: F401 -- re-exported for backward-compat imports
    _read_seq,
    _upsert_seq,
    run_sync_once,
)
from app.services.tenant_credentials import (
    TenantCredentialError,
    resolve_tenant_archive_credentials,
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


def _optional_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[FAIL] Environment variable {name} is not a valid integer: {raw!r}", flush=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Tenant resolution
# ---------------------------------------------------------------------------


def _require_tenant_id(session: Session, corp_id: str) -> str:
    """Return the active tenant_id for *corp_id*, or exit 1.

    Exits non-zero if the tenant_wecom_configs table is absent (migration 0002
    not applied), no active row matches the corp, or any DB error occurs.  Sync
    must never proceed without a valid tenant — caller must not handle SystemExit.
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
    limit = _optional_int_env("WECOM_CHAT_LIMIT", 500)

    engine = create_engine(database_url)

    # --- 2. Resolve credentials and tenant (before SDK init) ---
    # Fail fast: exit non-zero if no active tenant/tenant config exists, so no
    # archive write can ever happen with a wrong or missing tenant_id.
    tenant_id_env = os.environ.get("WECOM_TENANT_ID", "").strip()
    if tenant_id_env:
        with Session(engine) as session:
            try:
                credentials = resolve_tenant_archive_credentials(session, tenant_id_env)
            except TenantCredentialError as exc:
                print(
                    f"[FAIL] sync_worker error_class={exc.error_class} "
                    "Tenant archive credentials are unavailable",
                    flush=True,
                )
                sys.exit(1)
            corp_id = credentials.corp_id
            secret = credentials.archive_secret
            tenant_id = credentials.tenant_id
    else:
        corp_id = _require_env("WECOM_CORP_ID")
        secret = _require_env("WECOM_ARCHIVE_SECRET")
        with Session(engine) as session:
            tenant_id: str = _require_tenant_id(session, corp_id)

    # --- 3. Initialise WeCom SDK ---
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

    # --- 4. Allocate the GetChatData output slice ---
    slice_ptr = wecom_sdk.new_slice(lib)
    if not slice_ptr:
        print("[FAIL] NewSlice() returned null", flush=True)
        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception:
            pass
        sys.exit(1)

    # --- 5. Fetch + persist records via the core loop ---
    with Session(engine) as session:
        summary = run_sync_once(session, tenant_id, corp_id, lib, handle, slice_ptr, limit)

    # Always attempt cleanup
    try:
        wecom_sdk.free_slice(lib, slice_ptr)
    except Exception:
        pass
    try:
        wecom_sdk.destroy_sdk(lib, handle)
    except Exception:
        pass

    # --- 6. Print safe operational metrics ---
    print(f"[INFO] return_code: {summary.return_code}", flush=True)
    print(f"[INFO] record_count: {summary.record_count}", flush=True)
    print(f"[INFO] inserted: {summary.inserted}", flush=True)
    print(f"[INFO] skipped_duplicate: {summary.skipped_duplicate}", flush=True)
    print(f"[INFO] previous_seq: {summary.previous_seq}", flush=True)
    print(f"[INFO] new_seq: {summary.new_seq}", flush=True)

    if summary.return_code != 0:
        print(f"[FAIL] GetChatData returned code {summary.return_code}", flush=True)
        sys.exit(1)

    print("[PASS] sync_wecom_archive_once completed successfully", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
