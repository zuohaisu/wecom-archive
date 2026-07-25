"""
Core application logic for the one-shot real WeCom archive sync worker
(RND-222).

Extracted from scripts/sync_wecom_archive_once.py so it can be called and
tested directly, independent of env parsing, SDK lifecycle (load/
configure/new/init/destroy, and slice allocation — see the script's
module docstring for why slice alloc stays there), and CLI printing/exit
codes. scripts/sync_wecom_archive_once.py re-exports every name here for
backward-compatibility with existing test imports
(tests/test_tenant_sync_state.py, tests/test_tenant_foundation.py).

Tenant scope: sync was already tenant-scoped before RND-222 (see
_require_tenant_id in the script) — this extraction preserves that
unchanged. tenant_id is a required parameter here too, for the same
reason it is in app.services.decrypt_worker: no fallback, no None
default, so a caller can never accidentally run this against every
tenant's rows.

Transaction boundary (unchanged from the original script — see RND-222
ticket §0.3): per-run commit, not per-record — inserted records are
committed once, then the seq cursor is advanced and committed once more.
Both commits happen on the one Session the caller passes in (a
simplification from the original two-session structure — the original
opened a second Session purely to bump the cursor, against the same
engine/DB; using the caller's existing session for the second commit is
observably identical). Deliberately has NO try/except around either
commit call, matching the original script exactly: an unexpected commit
failure is allowed to propagate as an uncaught exception (Python's
default non-zero exit with a traceback), not translated into a `[FAIL]`
message — this worker has never had that translation and RND-222 does not
add it (only decrypt_wecom_messages_once.py's documented tenant-scope fix
changes behavior; see app/services/decrypt_worker.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, SyncState
from app.sdk import wecom_sdk as _default_sdk


@dataclass
class SyncRunSummary:
    return_code: int = 0
    record_count: int = 0
    inserted: int = 0
    skipped_duplicate: int = 0
    previous_seq: int = 0
    new_seq: int = 0


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
# Core loop
# ---------------------------------------------------------------------------


def run_sync_once(
    session: Session,
    tenant_id: str,
    corp_id: str,
    lib,
    handle,
    slice_ptr,
    limit: int,
    sdk=_default_sdk,
) -> SyncRunSummary:
    """Call GetChatData once, persist any new records, and advance the seq
    cursor. *slice_ptr* must already be allocated (sdk.new_slice(lib)) by
    the caller — slice lifecycle stays with SDK lifecycle in the shell,
    see module docstring — this function never allocates or frees it.

    Never prints and never calls sys.exit. A non-zero GetChatData return
    code is reported via the returned summary's return_code, not raised —
    the shell decides whether that is fatal (see
    scripts/sync_wecom_archive_once.py's main(), which prints the full
    summary either way before checking return_code).
    """
    summary = SyncRunSummary()

    prev_seq = _read_seq(session, corp_id, tenant_id)
    summary.previous_seq = prev_seq

    chat_ret = sdk.get_chat_data(lib, handle, slice_ptr, prev_seq, limit)
    summary.return_code = chat_ret

    records: list = []
    if chat_ret == 0:
        slice_len = sdk.get_slice_len(lib, slice_ptr)
        if slice_len > 0:
            raw = sdk.get_content_from_slice(lib, slice_ptr)
            if raw:
                try:
                    parsed = json.loads(raw)
                    records = parsed.get("chatdata", [])
                except (json.JSONDecodeError, ValueError):
                    pass  # records stays empty

    summary.record_count = len(records)

    inserted = 0
    skipped = 0
    max_seq = prev_seq

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

    summary.inserted = inserted
    summary.skipped_duplicate = skipped

    # Only commit if we actually got records back
    if records:
        session.commit()

        new_seq = max_seq
        _upsert_seq(session, corp_id, new_seq, tenant_id)
        session.commit()
    else:
        new_seq = prev_seq

    summary.new_seq = new_seq
    return summary
