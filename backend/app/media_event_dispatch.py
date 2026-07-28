"""Lightweight, fail-closed event dispatch for recent WeCom images (RND-172).

The durable queue is ``media_files`` itself.  This module only coalesces
in-process notifications; it deliberately owns no new database queue or SDK
abstraction.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import fcntl
import hashlib
import logging
import os
import resource
import threading
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import MediaFile
from app.db.session import get_engine
from app.media_download import select_candidates
from app.media_storage import (
    LocalStorageProvider,
    UnsupportedMediaStorageProvider,
    get_configured_write_backend_name,
    get_media_storage_provider,
)
from app.sdk import wecom_sdk
from app.services.media_worker import MediaDownloadSummary, download_media_candidates
from app.settings import get_event_media_download_settings

logger = logging.getLogger(__name__)
_DEFAULT_LOCK_PATH = "/srv/apps/wecom-archive-365/shared/run/wecom-media-download.lock"
_DEFAULT_TIMEOUT = 30

_pending_tenants: set[str] = set()
_pending_origins: dict[str, set[str]] = defaultdict(set)
_condition = threading.Condition()
_thread: Optional[threading.Thread] = None
_stopping = False


def _enabled() -> bool:
    return get_event_media_download_settings().event_media_download_enabled.strip().lower() == "true"


def _positive(raw: str, default: int) -> int:
    try:
        return max(1, int(raw.strip()))
    except (AttributeError, ValueError):
        return default


def _lock() -> Optional[int]:
    path = os.environ.get("MEDIA_DOWNLOAD_LOCK_PATH", "").strip() or _DEFAULT_LOCK_PATH
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o750, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except OSError:
        logger.warning("event_media_download_lock_unavailable")
        return None


def _unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _safe_reason_counts(reasons: dict) -> dict:
    # download_one returns short reason tags. Do not ever propagate an
    # unexpected detail string into operational logs.
    return {
        key: value
        for key, value in reasons.items()
        if isinstance(key, str) and key.replace("_", "").isalnum() and len(key) <= 64
    }


def _eligible_after_backoff(session: Session, tenant_id: str, candidates: list, retry_count: int, backoff: int) -> list:
    now = datetime.now(timezone.utc)
    eligible = []
    for message in candidates:
        media_file = (
            session.query(MediaFile)
            .filter(MediaFile.tenant_id == tenant_id, MediaFile.archive_message_id == message.id)
            .first()
        )
        if media_file is None or media_file.download_status != "failed":
            eligible.append(message)
            continue
        if media_file.download_attempts >= retry_count:
            continue
        attempted_at = media_file.updated_at
        if attempted_at is not None:
            if attempted_at.tzinfo is None:
                attempted_at = attempted_at.replace(tzinfo=timezone.utc)
            delay = backoff * min(2 ** max(media_file.download_attempts - 1, 0), 8)
            if (now - attempted_at).total_seconds() < delay:
                continue
        eligible.append(message)
    return eligible


def _increment_attempt(media_file: MediaFile, session: Session) -> None:
    media_file.download_attempts += 1
    session.commit()


def run_recent_image_sweep(tenant_id: str, triggered_by: str = "event") -> MediaDownloadSummary:
    """Synchronously process a bounded, newest-first recent image sweep."""
    started = time.monotonic()
    summary = MediaDownloadSummary()
    targeted = 0
    if not _enabled():
        return summary

    lock_fd = _lock()
    if lock_fd is None:
        return summary
    try:
        settings = get_event_media_download_settings()
        limit = _positive(settings.event_media_download_batch_limit, 20)
        retry_count = _positive(settings.event_media_download_retry_count, 3)
        backoff = _positive(settings.event_media_download_backoff_seconds, 30)
        window_hours = _positive(settings.event_media_download_recent_window_hours, 24)
        since_ms = int(time.time() * 1000) - window_hours * 3600 * 1000

        with Session(get_engine()) as session:
            actionable, stale_repairs, _total = select_candidates(
                session, tenant_id, {"image"}, retry=True, limit=limit,
                since_ms=since_ms, newest_first=True,
            )
            selected = list(actionable) + [message for message, _media_file in stale_repairs]
            candidates = _eligible_after_backoff(session, tenant_id, selected, retry_count, backoff)
            targeted = len(candidates)
            if not candidates:
                return summary

            try:
                backend = get_configured_write_backend_name()
                provider = get_media_storage_provider(backend)
                if isinstance(provider, LocalStorageProvider) and provider.root is None:
                    raise UnsupportedMediaStorageProvider("local storage unavailable")
                lib = wecom_sdk.load_sdk(os.environ.get("WECOM_SDK_LIB_PATH", "").strip())
                wecom_sdk.configure_sdk(lib)
                wecom_sdk.configure_sdk_media_data(lib)
                handle = wecom_sdk.new_sdk(lib)
                if not handle:
                    raise RuntimeError("sdk_new_failed")
                try:
                    result = wecom_sdk.init_sdk(
                        lib, handle, os.environ.get("WECOM_CORP_ID", "").strip(),
                        os.environ.get("WECOM_ARCHIVE_SECRET", "").strip(),
                    )
                    if result != 0:
                        raise RuntimeError("sdk_init_failed")
                    summary = download_media_candidates(
                        session, tenant_id, lib, handle, provider, backend, _DEFAULT_TIMEOUT,
                        candidates, [], before_attempt=lambda media_file: _increment_attempt(media_file, session),
                    )
                finally:
                    if handle:
                        try:
                            wecom_sdk.destroy_sdk(lib, handle)
                        except Exception:
                            pass
            except Exception:
                # SDK/storage setup failures are isolated from sync/callback.
                summary.failed += targeted
                summary.reason_counts["event_setup_error"] = targeted
    finally:
        _unlock(lock_fd)
        duration_ms = int((time.monotonic() - started) * 1000)
        tenant_tag = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12]
        logger.info(
            "event_media_download_sweep tenant=%s triggered_by=%s targeted=%d downloaded=%d "
            "failed=%d failed_reasons=%s bytes_downloaded=%d duration_ms=%d peak_rss_kb=%d",
            tenant_tag, triggered_by, targeted, summary.downloaded, summary.failed,
            _safe_reason_counts(summary.reason_counts), 0, duration_ms, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
    return summary


def _run_dispatcher() -> None:
    global _thread
    while True:
        with _condition:
            interval = _positive(get_event_media_download_settings().event_media_download_sweep_interval_seconds, 15)
            _condition.wait(timeout=interval)
            if _stopping:
                _thread = None
                return
            tenants = list(_pending_tenants)
            _pending_tenants.clear()
            origins = {tenant: ",".join(sorted(_pending_origins.pop(tenant, {"event"}))) for tenant in tenants}
        for tenant_id in tenants:
            run_recent_image_sweep(tenant_id, origins[tenant_id])


def trigger_recent_image_download(tenant_id: str, triggered_by: str = "event") -> None:
    """Coalesce an event signal and return immediately."""
    global _thread, _stopping
    if not _enabled():
        return
    with _condition:
        _pending_tenants.add(tenant_id)
        _pending_origins[tenant_id].add(triggered_by)
        if _thread is None or not _thread.is_alive():
            _stopping = False
            _thread = threading.Thread(target=_run_dispatcher, name="event-media-download", daemon=True)
            _thread.start()
        _condition.notify()


def shutdown() -> None:
    """Stop the daemon thread; intended for deterministic tests."""
    global _stopping
    with _condition:
        _stopping = True
        _pending_tenants.clear()
        _pending_origins.clear()
        _condition.notify_all()
    if _thread is not None:
        _thread.join(timeout=2)
