#!/usr/bin/env python3
"""Backfill missing byte counts for already downloaded media (RND-331).

Usage (from ``backend/``)::

    DATABASE_URL=sqlite:///local.db python scripts/backfill_media_file_size_once.py --dry-run
    DATABASE_URL=... python scripts/backfill_media_file_size_once.py --batch-size 50

Only ``download_status='downloaded' AND file_size IS NULL`` rows are
candidates.  Each candidate is resolved through its own recorded storage
backend (falling back to legacy local_path only when necessary), then updated
and committed individually.  Therefore reruns are idempotent and a small
batch bounds both reads and write transactions.  Missing/unavailable objects
are reported by row ID and never assigned a fabricated zero size.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.models import MediaFile  # noqa: E402
from app.media_storage import (  # noqa: E402
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageUnavailable,
    UnsupportedMediaStorageProvider,
    get_media_storage_provider_for_backend,
    resolve_effective_storage_reference,
)


@dataclass
class BackfillResult:
    selected: int = 0
    backfilled: int = 0
    would_backfill: int = 0
    failed_ids: list[int] = None

    def __post_init__(self) -> None:
        if self.failed_ids is None:
            self.failed_ids = []


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable not set or empty: {name}")
    return value


def _candidate_pages(session: Session, batch_size: int) -> Iterable[list[MediaFile]]:
    """Yield bounded, ID-cursor pages without ever selecting a filled row."""
    last_id = 0
    while True:
        page = (
            session.query(MediaFile)
            .filter(
                MediaFile.id > last_id,
                MediaFile.download_status == "downloaded",
                MediaFile.file_size.is_(None),
            )
            .order_by(MediaFile.id)
            .limit(batch_size)
            .all()
        )
        if not page:
            return
        yield page
        last_id = page[-1].id


def _size_for_row(media_file: MediaFile) -> int:
    backend, storage_ref = resolve_effective_storage_reference(
        media_file.storage_backend, media_file.storage_ref, media_file.local_path
    )
    if backend is None or not storage_ref:
        raise MediaStorageConfigurationError("media row has no usable storage reference")
    provider = get_media_storage_provider_for_backend(backend)
    size = provider.size_bytes(storage_ref)
    if size < 0:
        raise MediaStorageOperationError("storage provider returned negative size")
    return size


def run_backfill(engine, batch_size: int, dry_run: bool) -> BackfillResult:
    """Run a bounded-page backfill; callers own engine lifecycle.

    Failed candidates remain NULL and are deliberately included in the result
    so an operator must resolve them before applying the CHECK migration.
    """
    result = BackfillResult()
    with Session(engine) as session:
        for page in _candidate_pages(session, batch_size):
            for media_file in page:
                result.selected += 1
                try:
                    size = _size_for_row(media_file)
                except (
                    OSError,
                    ValueError,
                    MediaStorageConfigurationError,
                    MediaStorageUnavailable,
                    UnsupportedMediaStorageProvider,
                ):
                    session.rollback()
                    result.failed_ids.append(media_file.id)
                    print(f"[FAIL] media_file_id={media_file.id}: unable to determine size", flush=True)
                    continue

                if dry_run:
                    result.would_backfill += 1
                    print(f"[INFO] media_file_id={media_file.id}: would set file_size={size}", flush=True)
                    continue

                media_file.file_size = size
                session.commit()
                result.backfilled += 1
                print(f"[INFO] media_file_id={media_file.id}: backfilled", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill downloaded media file_size values")
    parser.add_argument("--dry-run", action="store_true", help="Read sizes but never write the database")
    parser.add_argument(
        "--batch-size", type=int, default=100, help="Maximum candidates fetched per page (default: 100)"
    )
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be a positive integer")

    try:
        database_url = _require_env("DATABASE_URL")
        result = run_backfill(create_engine(database_url), args.batch_size, args.dry_run)
    except RuntimeError as exc:
        print(f"[FAIL] {exc}", flush=True)
        raise SystemExit(1)

    print(f"[INFO] candidate_selected: {result.selected}", flush=True)
    print(f"[INFO] {'would_backfill' if args.dry_run else 'backfilled'}: "
          f"{result.would_backfill if args.dry_run else result.backfilled}", flush=True)
    print(f"[INFO] failed: {len(result.failed_ids)}", flush=True)
    if result.failed_ids:
        print("[FAIL] unresolved downloaded media rows remain; do not run the migration", flush=True)
        raise SystemExit(1)
    print("[PASS] backfill_media_file_size_once completed", flush=True)


if __name__ == "__main__":
    main()
