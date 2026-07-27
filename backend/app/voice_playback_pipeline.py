"""Generate browser-playable variants for already-archived voice media."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Query, Session

from app.db.models import MediaFile
from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageOperationError,
    MediaStorageProvider,
    MediaStorageUnavailable,
    build_voice_playback_storage_ref,
    resolve_effective_storage_reference,
)
from app.settings import get_voice_transcode_settings
from app.voice_transcode import transcode_voice_to_playable

_PLAYBACK_FILE_TYPES = frozenset({"voice", "audio_archive"})


def voice_transcode_enabled() -> bool:
    """True by default; false-y config leaves originals in legacy behaviour."""
    raw = (get_voice_transcode_settings().voice_transcode_enabled or "").strip().lower()
    return not raw or raw not in {"false", "0", "no", "off"}


def build_backfill_query(session: Session, tenant_id: str, retry: bool) -> Query:
    """Tenant-scoped downloaded voice/audio-archive rows eligible for work."""
    statuses = ["not_applicable", "pending"]
    if retry:
        statuses.extend(["failed", "unsupported_format"])
    return (
        session.query(MediaFile)
        .filter(
            MediaFile.tenant_id == tenant_id,
            MediaFile.file_type.in_(sorted(_PLAYBACK_FILE_TYPES)),
            MediaFile.download_status == "downloaded",
            (MediaFile.playback_status.in_(statuses))
            | (MediaFile.playback_status.is_(None)),
        )
        .order_by(MediaFile.id)
    )


def count_backfill_candidates(session: Session, tenant_id: str, retry: bool) -> int:
    return build_backfill_query(session, tenant_id, retry).count()


def _record_status(session: Session, media_file, status: str) -> str:
    """Persist a fixed, non-sensitive status; never propagate a failure."""
    try:
        media_file.playback_ref = None
        media_file.playback_status = status
        session.commit()
    except Exception:  # noqa: BLE001 - conversion must not break archival
        session.rollback()
    return status


def generate_and_persist(
    session: Session,
    provider: MediaStorageProvider,
    media_file,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Create one co-located MP3/WAV variant without risking the original."""
    if getattr(media_file, "file_type", None) not in _PLAYBACK_FILE_TYPES:
        return "skipped"
    if not voice_transcode_enabled():
        return "skipped"
    if not force and getattr(media_file, "playback_status", None) == "generated":
        return "exists"

    tenant_id = getattr(media_file, "tenant_id", None)
    _backend, original_ref = resolve_effective_storage_reference(
        getattr(media_file, "storage_backend", None),
        getattr(media_file, "storage_ref", None),
        getattr(media_file, "local_path", None),
    )
    if not tenant_id or not original_ref:
        return "failed" if dry_run else _record_status(session, media_file, "failed")

    try:
        data = provider.read_bytes(original_ref)
    except (MediaObjectNotFound, MediaStorageUnavailable, MediaStorageOperationError):
        return "failed" if dry_run else _record_status(session, media_file, "failed")
    except Exception:  # noqa: BLE001
        return "failed" if dry_run else _record_status(session, media_file, "failed")

    result = transcode_voice_to_playable(data)
    if result is None:
        return "unsupported_format" if dry_run else _record_status(
            session, media_file, "unsupported_format"
        )
    if dry_run:
        return "would_generate"

    try:
        playback_ref = build_voice_playback_storage_ref(
            original_ref, tenant_id, result.extension
        )
        stored_ref = provider.save_bytes(playback_ref, result.data)
    except (MediaStorageUnavailable, MediaStorageOperationError, OSError, ValueError):
        return _record_status(session, media_file, "failed")
    except Exception:  # noqa: BLE001
        return _record_status(session, media_file, "failed")

    try:
        media_file.playback_ref = stored_ref
        media_file.playback_status = "generated"
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        return _record_status(session, media_file, "failed")
    return "generated"


def maybe_generate_after_download(
    session: Session, provider: MediaStorageProvider, media_file
) -> Optional[str]:
    """Worker wrapper: original download success never depends on ffmpeg."""
    if getattr(media_file, "file_type", None) not in _PLAYBACK_FILE_TYPES:
        return None
    if not voice_transcode_enabled():
        return None
    try:
        media_file.playback_ref = None
        media_file.playback_status = "pending"
        session.commit()
        return generate_and_persist(session, provider, media_file)
    except Exception:  # noqa: BLE001
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
