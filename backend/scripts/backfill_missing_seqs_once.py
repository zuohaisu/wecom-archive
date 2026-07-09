#!/usr/bin/env python3
"""
Backfill: recover messages lost due to RND-182 sync seq off-by-one bug.

Scans all single-seq gaps in archive_messages, queries the WeCom API for
each gap seq, and inserts any valid record found (decrypt_status='pending').

Idempotent — re-running skips already-present records by msgid.

Usage (from backend/):
    python scripts/backfill_missing_seqs_once.py [--batch N]

Safety:
- Never moves the seq cursor backwards (sync_states stays untouched).
- Only inserts, never updates or deletes existing rows.
- Prints safe operational metrics only.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage
from app.sdk import wecom_sdk

BATCH_SIZE = 20  # How many API calls per batch (rate limit: 4000/min)


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"[FAIL] {name} not set", flush=True)
        sys.exit(1)
    return v


def _get_gap_seqs(engine) -> list[int]:
    """Return sorted list of all single-seq gap seqs."""
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT seq + 1 AS gap_seq
            FROM (
                SELECT seq, LEAD(seq) OVER (ORDER BY seq) AS n
                FROM (SELECT DISTINCT seq FROM archive_messages) s
            ) sub
            WHERE n IS NOT NULL AND n > seq + 1 AND n - seq - 1 = 1
            ORDER BY gap_seq
        """)).fetchall()
    return [r[0] for r in rows]


def main() -> None:
    database_url = _require_env("DATABASE_URL")
    lib_path = _require_env("WECOM_SDK_LIB_PATH")
    corp_id = _require_env("WECOM_CORP_ID")
    secret = _require_env("WECOM_ARCHIVE_SECRET")

    engine = create_engine(database_url)

    # --- Resolve tenant_id ---
    with Session(engine) as s:
        row = s.execute(
            text("SELECT tenant_id FROM tenant_wecom_configs WHERE is_active = TRUE LIMIT 1")
        ).one_or_none()
        if not row:
            print("[FAIL] No active tenant found", flush=True)
            sys.exit(1)
        tenant_id = row[0]
    print(f"[INFO] tenant_id: {tenant_id}", flush=True)

    # --- Get gap seqs ---
    gap_seqs = _get_gap_seqs(engine)
    print(f"[INFO] gap_seqs_found: {len(gap_seqs)}", flush=True)
    if not gap_seqs:
        print("[PASS] No gaps to backfill", flush=True)
        sys.exit(0)

    # --- Init SDK ---
    lib = wecom_sdk.load_sdk(lib_path)
    wecom_sdk.configure_sdk(lib)
    wecom_sdk.configure_sdk_get_chat_data(lib)
    handle = wecom_sdk.new_sdk(lib)
    init_ret = wecom_sdk.init_sdk(lib, handle, corp_id, secret)
    if init_ret != 0:
        print(f"[FAIL] Init() returned {init_ret}", flush=True)
        sys.exit(1)

    inserted = 0
    skipped_not_found = 0
    skipped_dup = 0
    api_errors = 0

    # Process in batches
    for batch_start in range(0, len(gap_seqs), BATCH_SIZE):
        batch = gap_seqs[batch_start:batch_start + BATCH_SIZE]
        print(f"[INFO] batch: seqs {batch[0]}..{batch[-1]} ({len(batch)} gaps)", flush=True)

        for gap_seq in batch:
            query_seq = gap_seq - 1  # API returns from query_seq+1

            slice_ptr = wecom_sdk.new_slice(lib)
            ret = wecom_sdk.get_chat_data(lib, handle, slice_ptr, query_seq, 1)

            if ret != 0:
                api_errors += 1
                wecom_sdk.free_slice(lib, slice_ptr)
                continue

            slice_len = wecom_sdk.get_slice_len(lib, slice_ptr)
            if slice_len == 0:
                skipped_not_found += 1
                wecom_sdk.free_slice(lib, slice_ptr)
                continue

            raw = wecom_sdk.get_content_from_slice(lib, slice_ptr)
            wecom_sdk.free_slice(lib, slice_ptr)

            if not raw:
                skipped_not_found += 1
                continue

            try:
                parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                records = parsed.get("chatdata", [])
            except Exception:
                skipped_not_found += 1
                continue

            if not records:
                skipped_not_found += 1
                continue

            rec = records[0]
            rec_seq = rec.get("seq", 0)
            msgid = rec.get("msgid", "")

            if rec_seq != gap_seq:
                # Didn't get the seq we were looking for
                skipped_not_found += 1
                continue

            if not msgid:
                skipped_not_found += 1
                continue

            # Idempotency: skip if msgid already exists for this tenant
            with Session(engine) as s:
                existing = s.execute(
                    text("SELECT id FROM archive_messages WHERE msgid = :m AND tenant_id = :t"),
                    {"m": msgid, "t": tenant_id},
                ).one_or_none()
                if existing:
                    skipped_dup += 1
                    continue

                # Insert the missing record
                msg = ArchiveMessage(
                    msgid=msgid,
                    seq=rec_seq,
                    publickey_ver=rec.get("publickey_ver", 0),
                    raw_encrypted_payload=rec,
                    encrypt_random_key=rec.get("encrypt_random_key", ""),
                    encrypt_chat_msg=rec.get("encrypt_chat_msg", ""),
                    decrypt_status="pending",
                    tenant_id=tenant_id,
                )
                s.add(msg)
                s.commit()
                inserted += 1
                print(f"[INFO] backfilled: seq={gap_seq} msgid={msgid}", flush=True)

            # Small delay to stay within rate limits
            time.sleep(0.05)

    # --- Cleanup ---
    try:
        wecom_sdk.destroy_sdk(lib, handle)
    except Exception:
        pass

    # --- Report ---
    print(f"[INFO] backfill_gaps_total: {len(gap_seqs)}", flush=True)
    print(f"[INFO] backfill_inserted: {inserted}", flush=True)
    print(f"[INFO] backfill_skipped_not_found: {skipped_not_found}", flush=True)
    print(f"[INFO] backfill_skipped_duplicate: {skipped_dup}", flush=True)
    print(f"[INFO] backfill_api_errors: {api_errors}", flush=True)
    print("[PASS] backfill_missing_seqs_once completed", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
