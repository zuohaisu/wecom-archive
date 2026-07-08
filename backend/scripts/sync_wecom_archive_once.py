#!/usr/bin/env python3
"""
One-shot real WeCom archive sync worker.

Loads environment, initialises the WeCom Finance SDK, reads the current seq
cursor from the database, calls GetChatData, persists encrypted archive records
into PostgreSQL, and advances the cursor after a successful commit.

Idempotent — re-running never creates duplicate rows (keyed on (tenant_id, msgid)).

Usage (from backend/):
    python scripts/sync_wecom_archive_once.py

Required environment variables:
    DATABASE_URL          PostgreSQL connection string
    WECOM_SDK_LIB_PATH    Absolute path to libWeWorkFinanceSdk_C.so
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

import json
import os
import sys

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, SyncState, TenantWecomConfig
from app.sdk import wecom_sdk


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
# Sync state
# ---------------------------------------------------------------------------


def _read_seq(session: Session, corp_id: str, tenant_id: str) -> int:
    """Return the last synced seq for *(tenant_id, corp_id)*, or 0 if absent."""
    row = (
        session.query(SyncState)
        .filter(SyncState.corp_id == corp_id, SyncState.tenant_id == tenant_id)
        .with_for_update(skip_locked=True)
        .first()
    )
    if row is None:
        return 0
    return row.last_seq


def _upsert_seq(session: Session, corp_id: str, new_seq: int, tenant_id: str) -> None:
    """Create or update the sync state row for *(tenant_id, corp_id)* to *new_seq*."""
    row = (
        session.query(SyncState)
        .filter(SyncState.corp_id == corp_id, SyncState.tenant_id == tenant_id)
        .first()
    )
    if row is None:
        session.add(SyncState(corp_id=corp_id, last_seq=new_seq, tenant_id=tenant_id))
    else:
        row.last_seq = new_seq


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # --- 1. Load environment ---
    database_url = _require_env("DATABASE_URL")
    lib_path = _require_env("WECOM_SDK_LIB_PATH")
    corp_id = _require_env("WECOM_CORP_ID")
    secret = _require_env("WECOM_ARCHIVE_SECRET")
    limit = _optional_int_env("WECOM_CHAT_LIMIT", 500)

    # --- 2. Resolve tenant and read seq cursor (before SDK init) ---
    # Fail fast: exit non-zero if no active tenant config exists for this corp.
    # This prevents any archive writes with tenant_id=None.
    engine = create_engine(database_url)

    with Session(engine) as session:
        tenant_id: str = _require_tenant_id(session, corp_id)
        prev_seq: int = _read_seq(session, corp_id, tenant_id)

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

    # --- 4. Call GetChatData ---
    slice_ptr = wecom_sdk.new_slice(lib)
    if not slice_ptr:
        print("[FAIL] NewSlice() returned null", flush=True)
        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception:
            pass
        sys.exit(1)

    chat_ret = wecom_sdk.get_chat_data(lib, handle, slice_ptr, prev_seq, limit)

    records: list[dict] = []
    if chat_ret == 0:
        slice_len = wecom_sdk.get_slice_len(lib, slice_ptr)
        if slice_len > 0:
            raw = wecom_sdk.get_content_from_slice(lib, slice_ptr)
            if raw:
                try:
                    parsed = json.loads(raw)
                    records = parsed.get("chatdata", [])
                except (json.JSONDecodeError, ValueError):
                    pass  # records stays empty

    # --- 5. Persist records ---
    inserted = 0
    skipped = 0
    max_seq = prev_seq

    with Session(engine) as session:
        for rec in records:
            msgid = rec.get("msgid", "")
            if not msgid:
                continue

            seq_val = rec.get("seq", 0)
            if seq_val > max_seq:
                max_seq = seq_val

            # Idempotency check scoped to (tenant_id, msgid) per the unique constraint
            existing = (
                session.query(ArchiveMessage)
                .filter(
                    ArchiveMessage.msgid == msgid,
                    ArchiveMessage.tenant_id == tenant_id,
                )
                .first()
            )
            if existing:
                skipped += 1
                continue

            msg = ArchiveMessage(
                msgid=msgid,
                seq=seq_val,
                # Encrypted envelope — store the raw record exactly as returned
                publickey_ver=rec.get("publickey_ver", 0),
                raw_encrypted_payload=rec,
                encrypt_random_key=rec.get("encrypt_random_key", ""),
                encrypt_chat_msg=rec.get("encrypt_chat_msg", ""),
                # Decryption state — not yet attempted
                decrypt_status="pending",
                tenant_id=tenant_id,
            )
            session.add(msg)
            inserted += 1

        # Only commit if we actually got records back
        if records:
            session.commit()

            # --- 6. Update seq cursor after successful commit ---
            new_seq = max_seq
            with Session(engine) as update_session:
                _upsert_seq(update_session, corp_id, new_seq, tenant_id)
                update_session.commit()
        else:
            new_seq = prev_seq

    # Always attempt cleanup
    try:
        wecom_sdk.free_slice(lib, slice_ptr)
    except Exception:
        pass
    try:
        wecom_sdk.destroy_sdk(lib, handle)
    except Exception:
        pass

    # --- 7. Print safe operational metrics ---
    print(f"[INFO] return_code: {chat_ret}", flush=True)
    print(f"[INFO] record_count: {len(records)}", flush=True)
    print(f"[INFO] inserted: {inserted}", flush=True)
    print(f"[INFO] skipped_duplicate: {skipped}", flush=True)
    print(f"[INFO] previous_seq: {prev_seq}", flush=True)
    print(f"[INFO] new_seq: {new_seq}", flush=True)

    if chat_ret != 0:
        print(f"[FAIL] GetChatData returned code {chat_ret}", flush=True)
        sys.exit(1)

    print("[PASS] sync_wecom_archive_once completed successfully", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()