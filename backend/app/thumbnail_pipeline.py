"""Thumbnail generation + persistence for one media_files row (RND-207).

Shared by both callers that need to attach a list/timeline thumbnail to an
image row:

- the media-download worker (scripts/download_wecom_media_once.py), right
  after a new image is successfully downloaded/published, and
- the historical backfill (scripts/backfill_thumbnails_once.py).

Both need identical, failure-isolated semantics, so the logic lives here once
rather than in two copies that could drift.

Hard guarantees:

- **Never raises.** A thumbnail is an optimization; its generation must never
  affect the original media's archival or serving. generate_and_persist()
  catches everything and records a "failed" bookkeeping status instead of
  propagating.
- **Idempotent.** A row already marked thumbnail_status="generated" is a
  zero-op (returns "exists") unless force=True — so a repeated backfill run
  does real no-op work, mirroring the RND-186 migration's storage_backend
  idempotency.
- **Co-located.** The thumbnail object is written through the SAME provider
  (backend) as the original and under the same tenants/{tenant}/ prefix, so
  serving resolves it with the row's own storage_backend and the existing
  tenant-prefix defense applies unchanged.
- **No secrets in the recorded error.** thumbnail_error is a short, fixed tag
  (never a path, object key, or exception string).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Query, Session

from app.db.models import MediaFile
from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageOperationError,
    MediaStorageProvider,
    MediaStorageUnavailable,
    build_thumbnail_storage_ref,
    resolve_effective_storage_reference,
)
from app.media_thumbnails import generate_thumbnail, thumbnails_enabled

logger = logging.getLogger(__name__)

# file_type values whose bytes are ordinary images and therefore
# thumbnailable. "emotion" (stickers/animated emoji) is included — its bytes
# are images (see media_download._SIGNATURE_CATEGORY_BY_MSGTYPE) and it is
# rendered inline like an image; media_thumbnails collapses an animated GIF to
# a static first frame for the list while the original is untouched.
_THUMBNAILABLE_FILE_TYPES = {"image", "emotion"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_backfill_query(session: Session, tenant_id: str, retry: bool) -> Query:
    """Tenant-scoped query for image/emotion rows eligible for a thumbnail
    backfill attempt: a thumbnailable type, fully downloaded (has bytes),
    and either never attempted (thumbnail_status IS NULL) or — only with
    --retry — previously failed (thumbnail_status == "failed").

    Rows already thumbnailed (thumbnail_status == "generated") or skipped
    (non-image) are excluded by construction, so re-running with no flags is
    a true no-op for them — zero reads, zero uploads."""
    query = session.query(MediaFile).filter(
        MediaFile.tenant_id == tenant_id,
        MediaFile.file_type.in_(sorted(_THUMBNAILABLE_FILE_TYPES)),
        MediaFile.download_status == "downloaded",
    )
    if retry:
        query = query.filter(
            (MediaFile.thumbnail_status.is_(None))
            | (MediaFile.thumbnail_status == "failed")
        )
    else:
        query = query.filter(MediaFile.thumbnail_status.is_(None))
    return query.order_by(MediaFile.id)


def count_backfill_candidates(session: Session, tenant_id: str, retry: bool) -> int:
    """Count of all eligible thumbnail-backfill candidates, ignoring --limit
    (for --count-only reporting)."""
    return build_backfill_query(session, tenant_id, retry).count()


def _record_failure(session: Session, media_file, tag: str) -> str:
    """Stamp a sanitized failure without ever raising. tag is a short fixed
    diagnostic ("decode_failed"/"read_failed"/"upload_failed"/"error"), never
    a path/key/exception text."""
    try:
        media_file.thumbnail_status = "failed"
        media_file.thumbnail_attempted_at = _utcnow()
        media_file.thumbnail_error = tag
        session.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must never mask the outcome
        session.rollback()
    return "failed"


def generate_and_persist(
    session: Session,
    provider: MediaStorageProvider,
    media_file,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Generate, upload, and record a thumbnail for one image media_file row.

    provider must be the row's OWN backend provider (so the thumbnail is
    co-located). Returns one of: "generated", "exists" (already done, no-op),
    "skipped" (not a thumbnailable type, or thumbnails disabled), "failed".
    With dry_run=True nothing is uploaded or committed and the return is
    "would_generate" / "exists" / "skipped" / "failed" (validation only).
    Never raises.
    """
    file_type = getattr(media_file, "file_type", None)
    if file_type not in _THUMBNAILABLE_FILE_TYPES:
        # Not an image row — mark it skipped so a backfill scan can exclude it
        # on future passes rather than re-examining it every run.
        if not dry_run and getattr(media_file, "thumbnail_status", None) is None:
            try:
                media_file.thumbnail_status = "skipped"
                media_file.thumbnail_attempted_at = _utcnow()
                session.commit()
            except Exception:  # noqa: BLE001
                session.rollback()
        return "skipped"

    if not thumbnails_enabled():
        return "skipped"

    if not force and getattr(media_file, "thumbnail_status", None) == "generated":
        return "exists"

    tenant_id = getattr(media_file, "tenant_id", None)
    _backend, effective_ref = resolve_effective_storage_reference(
        getattr(media_file, "storage_backend", None),
        getattr(media_file, "storage_ref", None),
        getattr(media_file, "local_path", None),
    )
    if not effective_ref or not tenant_id:
        return "failed" if dry_run else _record_failure(session, media_file, "no_source")

    try:
        data = provider.read_bytes(effective_ref)
    except (MediaObjectNotFound, MediaStorageUnavailable, MediaStorageOperationError):
        return "failed" if dry_run else _record_failure(session, media_file, "read_failed")
    except Exception:  # noqa: BLE001
        return "failed" if dry_run else _record_failure(session, media_file, "read_failed")

    result = generate_thumbnail(data)
    if result is None:
        return "failed" if dry_run else _record_failure(session, media_file, "decode_failed")

    if dry_run:
        # Read + decode validated; no upload, no DB write.
        return "would_generate"

    thumb_ref = build_thumbnail_storage_ref(effective_ref, tenant_id, result.extension)
    try:
        stored_ref = provider.save_bytes(thumb_ref, result.data)
    except (MediaStorageUnavailable, MediaStorageOperationError, OSError, ValueError):
        return _record_failure(session, media_file, "upload_failed")
    except Exception:  # noqa: BLE001
        return _record_failure(session, media_file, "upload_failed")

    try:
        media_file.thumbnail_ref = stored_ref
        media_file.image_width = result.source_width
        media_file.image_height = result.source_height
        media_file.thumbnail_status = "generated"
        media_file.thumbnail_attempted_at = _utcnow()
        media_file.thumbnail_error = None
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        # The thumbnail object is now an orphan (its row wasn't updated), but
        # the deterministic key means a later run overwrites it in place —
        # never an accumulating leak. Record the failure so --retry revisits.
        return _record_failure(session, media_file, "commit_failed")

    return "generated"


def maybe_generate_after_download(
    session: Session,
    provider: MediaStorageProvider,
    media_file,
) -> Optional[str]:
    """Convenience wrapper for the download worker: generate a thumbnail for a
    freshly-downloaded image row, swallowing everything. Returns the status
    string (or None if the row/config disabled it), for optional counting."""
    try:
        return generate_and_persist(session, provider, media_file)
    except Exception:  # noqa: BLE001 - defense in depth; generate_and_persist already guards
        logger.warning("thumbnail generation raised unexpectedly; original archival unaffected")
        return None
