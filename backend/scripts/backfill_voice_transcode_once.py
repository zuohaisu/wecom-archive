#!/usr/bin/env python3
"""One-shot, resumable voice-playback backfill for RND-258.

Historical downloaded ``voice`` and ``audio_archive`` rows are read from
their own storage backend, converted once to a co-located MP3/WAV derivative,
and marked generated. Failures are recorded as fixed status tags and never
stop the batch or modify the archival original.

Run from ``backend/``:

    python scripts/backfill_voice_transcode_once.py --count-only
    python scripts/backfill_voice_transcode_once.py --dry-run --limit 20
    python scripts/backfill_voice_transcode_once.py --limit 20 --batch-size 10

``DATABASE_URL`` and ``WECOM_CORP_ID`` select the tenant server-side; no
tenant id, sdkfileid, object key, signed URL, or local storage path is ever
accepted or printed.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from typing import List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.models import MediaFile, TenantWecomConfig  # noqa: E402
from app.media_storage import (  # noqa: E402
    MediaStorageConfigurationError,
    UnsupportedMediaStorageProvider,
    get_media_storage_provider_for_backend,
)
from app.voice_playback_pipeline import (  # noqa: E402
    build_backfill_query,
    count_backfill_candidates,
    generate_and_persist,
    voice_transcode_enabled,
)

_DEFAULT_LOCK_PATH = "/srv/apps/wecom-archive-365/shared/run/wecom-voice-transcode-backfill.lock"
_DEFAULT_LIMIT = 100
_DEFAULT_BATCH_SIZE = 25


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


def _acquire_lock(lock_path: str) -> Optional[int]:
    lock_dir = os.path.dirname(lock_path)
    if lock_dir and not os.path.isdir(lock_dir):
        try:
            os.makedirs(lock_dir, mode=0o750, exist_ok=True)
        except OSError:
            print("[FAIL] Cannot create lock directory", flush=True)
            sys.exit(1)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return None
    return lock_fd


def _release_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001
        pass
    try:
        os.close(lock_fd)
    except Exception:  # noqa: BLE001
        pass


def _require_tenant_id(session: Session, corp_id: str) -> str:
    row = (
        session.query(TenantWecomConfig)
        .filter(
            TenantWecomConfig.corp_id == corp_id,
            TenantWecomConfig.is_active == True,  # noqa: E712
        )
        .first()
    )
    if row is None:
        print("[FAIL] No active tenant found for this corp", flush=True)
        sys.exit(1)
    return row.tenant_id


def _select_candidates(
    session: Session, tenant_id: str, retry: bool, limit: int, batch_size: int
) -> List[MediaFile]:
    selected: List[MediaFile] = []
    last_id = 0
    while len(selected) < limit:
        page = (
            build_backfill_query(session, tenant_id, retry)
            .filter(MediaFile.id > last_id)
            .limit(batch_size)
            .all()
        )
        if not page:
            break
        selected.extend(page[: limit - len(selected)])
        last_id = page[-1].id
        if len(page) < batch_size:
            break
    return selected


def _provider_for_row(media_file: MediaFile):
    backend = getattr(media_file, "storage_backend", None)
    if not backend:
        return None
    try:
        return get_media_storage_provider_for_backend(backend)
    except (MediaStorageConfigurationError, UnsupportedMediaStorageProvider):
        return None


def _run(args: argparse.Namespace) -> None:
    database_url = _require_env("DATABASE_URL")
    corp_id = _require_env("WECOM_CORP_ID")
    if not voice_transcode_enabled():
        print("[PASS] VOICE_TRANSCODE_ENABLED is false — backfill is a no-op", flush=True)
        return

    with Session(create_engine(database_url)) as session:
        tenant_id = _require_tenant_id(session, corp_id)
        total = count_backfill_candidates(session, tenant_id, args.retry)
        print(f"[INFO] candidate_total: {total}", flush=True)
        if args.count_only:
            print("[PASS] count-only mode — no reads or writes performed", flush=True)
            return

        candidates = _select_candidates(
            session, tenant_id, args.retry, args.limit, args.batch_size
        )
        print(f"[INFO] candidate_selected: {len(candidates)}", flush=True)
        generated = failed = unsupported = skipped = would_generate = 0
        for media_file in candidates:
            provider = _provider_for_row(media_file)
            status = (
                "failed"
                if provider is None
                else generate_and_persist(
                    session,
                    provider,
                    media_file,
                    force=args.retry,
                    dry_run=args.dry_run,
                )
            )
            if status in {"generated", "exists"}:
                generated += 1
            elif status == "would_generate":
                would_generate += 1
            elif status == "unsupported_format":
                unsupported += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1

        if args.dry_run:
            print(f"[INFO] would_generate: {would_generate}", flush=True)
        else:
            print(f"[INFO] generated: {generated}", flush=True)
        print(f"[INFO] unsupported_format: {unsupported}", flush=True)
        print(f"[INFO] skipped: {skipped}", flush=True)
        print(f"[INFO] failed: {failed}", flush=True)
        print("[PASS] backfill_voice_transcode_once completed", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="RND-258 voice playback backfill")
    parser.add_argument("--count-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE)
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    if args.limit <= 0 or args.batch_size <= 0:
        print("[FAIL] --limit and --batch-size must be positive integers", flush=True)
        sys.exit(1)

    lock_path = (
        os.environ.get("MEDIA_VOICE_TRANSCODE_BACKFILL_LOCK_PATH", "").strip()
        or _DEFAULT_LOCK_PATH
    )
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
        print("[PASS] another instance already holds the run lock", flush=True)
        return
    try:
        _run(args)
    finally:
        _release_lock(lock_fd)


if __name__ == "__main__":
    main()
