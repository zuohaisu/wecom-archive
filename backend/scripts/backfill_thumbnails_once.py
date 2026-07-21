#!/usr/bin/env python3
"""
One-shot, repeatable: backfill list/timeline thumbnails for historical image
media_files rows (RND-207).

The chat archive UI previously loaded full-resolution originals in the
timeline. RND-207 adds a small server-generated thumbnail (a separate stored
object, NOT a Qiniu CDN image-processing transform) that the list renders
while the viewer still opens the original. New downloads generate their
thumbnail automatically (scripts/download_wecom_media_once.py); this tool
generates thumbnails for the images that already existed before the feature
shipped.

This is a TOOL, not a one-time event: designed to be run manually, repeatedly,
in small batches, safely interrupted, and resumed, without ever touching the
original media a user is actively viewing. It never modifies, deletes, or
re-encodes an original object — it only reads an original's bytes, writes a
derived thumbnail object at a deterministic co-located key, and stamps the
row's thumbnail_* bookkeeping columns (migration 0012). A thumbnail failure
never affects the original's archival or serving.

Usage (from backend/):
    python scripts/backfill_thumbnails_once.py --count-only
    python scripts/backfill_thumbnails_once.py --dry-run --limit 20
    python scripts/backfill_thumbnails_once.py --limit 20 --batch-size 10
    python scripts/backfill_thumbnails_once.py --limit 50 --retry

Required environment variables (only DATABASE_URL / WECOM_CORP_ID are needed
for --count-only, which never touches storage):
    DATABASE_URL          PostgreSQL connection string
    WECOM_CORP_ID         WeCom corporation ID (resolves the active tenant)

Storage credentials are resolved PER ROW from each row's own storage_backend
(migration 0005) — a "local" row is read from local storage and its thumbnail
written back to local; a "qiniu_kodo" row is read from and written to Qiniu.
So the exact same QINIU_* / STORAGE_LOCAL_PATH variables the serving path
already needs are what this tool needs, for whichever backends the candidate
rows actually use.

Optional environment variables:
    MEDIA_THUMBNAIL_ENABLED / MEDIA_THUMBNAIL_MAX_EDGE /
    MEDIA_THUMBNAIL_JPEG_QUALITY   Thumbnail generation knobs (see
                                   app.media_thumbnails). With
                                   MEDIA_THUMBNAIL_ENABLED=false this tool is
                                   a no-op.
    MEDIA_THUMBNAIL_BACKFILL_LOCK_PATH   Lock file path (default:
        /srv/apps/wecom-archive-365/shared/run/wecom-thumbnail-backfill.lock)

Tenant scoping: resolved server-side from WECOM_CORP_ID via
tenant_wecom_configs, exactly like every other one-shot script in this
codebase. There is no --tenant-id flag — tenant_id is never accepted from
caller-supplied input, and every query/write is scoped by the resolved
tenant_id.

Candidate selection (tenant-scoped) — see
app.thumbnail_pipeline.build_backfill_query:
    file_type IN ("image", "emotion"), download_status == "downloaded" (only
    rows that actually have bytes), and thumbnail_status IS NULL (never
    attempted), or — only with --retry — thumbnail_status == "failed". A row
    already thumbnailed (thumbnail_status == "generated") or skipped
    (non-image) is excluded by construction, making a no-flag re-run a true
    no-op for it — zero reads, zero uploads.

Idempotency / resumability: generation writes to a deterministic key
(app.media_storage.build_thumbnail_storage_ref), so a retried row overwrites
its own thumbnail in place rather than accumulating duplicates, and the DB
flip to thumbnail_status="generated" is what removes it from future candidate
scans. All generation/upload/stamp logic is shared with the download worker
via app.thumbnail_pipeline.generate_and_persist (failure-isolated, never
raises).

Resource control (2C2G box): rows are processed strictly sequentially, one at
a time, committing per row; --limit bounds how many are attempted per run and
--batch-size bounds the fetch page size (memory), so an operator can pace CPU
by running small --limit batches. There is no parallelism.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from typing import List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Ensure `import app.*` works when run directly from backend/ (mirrors the
# sibling one-shot scripts).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.models import MediaFile, TenantWecomConfig  # noqa: E402
from app.media_storage import (  # noqa: E402
    MediaStorageConfigurationError,
    UnsupportedMediaStorageProvider,
    get_media_storage_provider_for_backend,
)
from app.media_thumbnails import thumbnails_enabled  # noqa: E402
from app.thumbnail_pipeline import (  # noqa: E402
    build_backfill_query,
    count_backfill_candidates,
    generate_and_persist,
)

_DEFAULT_LOCK_PATH = "/srv/apps/wecom-archive-365/shared/run/wecom-thumbnail-backfill.lock"
_DEFAULT_LIMIT = 100
_DEFAULT_BATCH_SIZE = 25


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


def _acquire_lock(lock_path: str) -> Optional[int]:
    """Acquire a non-blocking exclusive file lock. Returns the open fd on
    success, or None if another process already holds it. Never blocks, never
    prints the lock path itself."""
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
    except Exception:
        pass
    try:
        os.close(lock_fd)
    except Exception:
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
        print(
            "[FAIL] No active tenant found for this corp. "
            "Run bootstrap_default_tenant.py first.",
            flush=True,
        )
        sys.exit(1)
    return row.tenant_id


def _select_candidates(
    session: Session, tenant_id: str, retry: bool, limit: int, batch_size: int
) -> List[MediaFile]:
    """Paginate through build_backfill_query in batch_size-sized pages until
    `limit` rows are collected or candidates are exhausted (same cursor-by-id
    pattern as the migration tool). Pagination keeps memory bounded; this
    script commits per row, so batch_size is only a fetch-chunk size."""
    selected: List[MediaFile] = []
    if limit <= 0:
        return selected

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
        for row in page:
            selected.append(row)
            if len(selected) >= limit:
                break
        last_id = page[-1].id
        if len(page) < batch_size:
            break
    return selected


def _provider_for_row(media_file: MediaFile):
    """Resolve the row's OWN backend provider so the thumbnail is written
    co-located with the original. Returns None (and records nothing) on a
    misconfigured/unknown backend — the caller counts it as a failure."""
    backend = getattr(media_file, "storage_backend", None)
    if not backend:
        return None
    try:
        return get_media_storage_provider_for_backend(backend)
    except (MediaStorageConfigurationError, UnsupportedMediaStorageProvider):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-shot, repeatable historical thumbnail backfill (RND-207)"
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Report candidate counts only; perform no reads or writes against storage",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read and decode candidates exactly as a live run would, but never "
            "upload a thumbnail and never write to the database"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"Max rows to process this run (default {_DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=(
            f"Candidate fetch page size (default {_DEFAULT_BATCH_SIZE}); does not "
            "change commit granularity — each row commits independently"
        ),
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Also retry media_files rows with thumbnail_status='failed'",
    )
    args = parser.parse_args()

    if args.limit <= 0:
        print("[FAIL] --limit must be a positive integer", flush=True)
        sys.exit(1)
    if args.batch_size <= 0:
        print("[FAIL] --batch-size must be a positive integer", flush=True)
        sys.exit(1)

    lock_path = (
        os.environ.get("MEDIA_THUMBNAIL_BACKFILL_LOCK_PATH", "").strip() or _DEFAULT_LOCK_PATH
    )
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
        print(
            "[PASS] another instance already holds the run lock — exiting without "
            "selecting candidates",
            flush=True,
        )
        sys.exit(0)

    try:
        _run(args)
    finally:
        _release_lock(lock_fd)


def _run(args: argparse.Namespace) -> None:
    database_url = _require_env("DATABASE_URL")
    corp_id = _require_env("WECOM_CORP_ID")

    if not thumbnails_enabled():
        print(
            "[PASS] MEDIA_THUMBNAIL_ENABLED is false — thumbnail backfill is a no-op",
            flush=True,
        )
        sys.exit(0)

    engine = create_engine(database_url)

    with Session(engine) as session:
        tenant_id = _require_tenant_id(session, corp_id)

        total_eligible = count_backfill_candidates(session, tenant_id, args.retry)
        print(f"[INFO] candidate_total: {total_eligible}", flush=True)

        if args.count_only:
            print("[PASS] count-only mode — no reads or writes performed", flush=True)
            sys.exit(0)

        candidates = _select_candidates(
            session, tenant_id, args.retry, args.limit, args.batch_size
        )
        print(f"[INFO] candidate_selected: {len(candidates)}", flush=True)

        if not candidates:
            print("[PASS] no candidates to thumbnail", flush=True)
            sys.exit(0)

        generated = 0
        failed = 0
        skipped = 0
        would_generate = 0
        processed = 0

        for media_file in candidates:
            provider = _provider_for_row(media_file)
            if provider is None:
                failed += 1
            else:
                status = generate_and_persist(
                    session, provider, media_file, force=args.retry, dry_run=args.dry_run
                )
                if status in ("generated",):
                    generated += 1
                elif status == "would_generate":
                    would_generate += 1
                elif status == "skipped":
                    skipped += 1
                elif status == "exists":
                    # Only reachable with --retry force on an already-done row;
                    # count as generated-equivalent (already present).
                    generated += 1
                else:
                    failed += 1

            processed += 1
            if processed % args.batch_size == 0:
                print(f"[INFO] progress: {processed}/{len(candidates)}", flush=True)

        if args.dry_run:
            print(f"[INFO] would_generate: {would_generate}", flush=True)
        else:
            print(f"[INFO] generated: {generated}", flush=True)
        print(f"[INFO] skipped: {skipped}", flush=True)
        print(f"[INFO] failed: {failed}", flush=True)
        print("[PASS] backfill_thumbnails_once completed", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
