"""Bounded archive-complete wake-up for the unified media worker.

``media_files`` plus normalised archive messages remain the durable task
source. This module only checks for newly actionable media after a completed
archive run and touches one coalescing signal. A systemd path unit or the
self-host container scheduler may consume it; this module never downloads
bytes, initialises the SDK, or implements a second media pipeline.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session

from app.db.models import Tenant
from app.db.session import get_engine
from app.media_download import (
    GENERIC_DOWNLOAD_MSGTYPES,
    build_candidate_query,
    select_nested_media_candidates,
)
from app.services.service_access import WORKER_MEDIA, tenant_service_denial
from app.services.tenant_credentials import tenant_log_tag as _tenant_tag
from app.settings import get_event_media_download_settings

logger = logging.getLogger(__name__)

_DEFAULT_SIGNAL_PATH = "/srv/apps/wecom-archive-365/shared/run/wecom-media-dispatch.trigger"
_TRIGGER_SOURCES = frozenset({"archive-complete", "manual", "timer"})


class MediaWorkerDispatch(str, Enum):
    """Safe aggregate outcomes for a media-worker wake-up request."""

    ACCEPTED = "accepted"
    NO_WORK = "no-work"
    # RND-402: the tenant's authoritative service projection denies the
    # media capability (frozen / suspended) — no signal is emitted.
    SKIPPED = "skipped"
    FAILED = "dispatch-failed"


@dataclass(frozen=True)
class PendingMediaCheck:
    """Bounded preflight result; counts never include media identifiers."""

    selected_top_level: int = 0
    selected_nested: int = 0

    @property
    def selected_total(self) -> int:
        return self.selected_top_level + self.selected_nested


def _safe_source(raw: str) -> str:
    return raw if raw in _TRIGGER_SOURCES else "archive-complete"


def _enabled() -> bool:
    """Event dispatch is on by default; only an explicit false disables it."""
    raw = get_event_media_download_settings().event_media_download_enabled.strip().lower()
    return raw not in {"false", "0", "no", "off"}


def _positive(raw: str, default: int) -> int:
    try:
        return max(1, int(raw.strip()))
    except (AttributeError, ValueError):
        return default


def _pending_media_check(tenant_id: str) -> PendingMediaCheck:
    """Return a bounded count of fresh/pending generic media candidates.

    ``retry=False`` is intentional: archive completion only wakes the worker
    for media made actionable by a successful sync/decrypt commit. Failed
    media is reconciled by the low-frequency media timer under its existing
    retry policy.
    """
    settings = get_event_media_download_settings()
    limit = _positive(settings.event_media_download_batch_limit, 20)
    window_hours = _positive(settings.event_media_download_recent_window_hours, 24)
    since_ms = int(time.time() * 1000) - window_hours * 3600 * 1000

    with Session(get_engine()) as session:
        top_level = (
            build_candidate_query(
                session,
                tenant_id,
                GENERIC_DOWNLOAD_MSGTYPES,
                retry=False,
                since_ms=since_ms,
                newest_first=True,
            )
            .limit(limit)
            .all()
        )
        remaining = limit - len(top_level)
        nested = []
        if remaining:
            nested, _scanned = select_nested_media_candidates(
                session,
                tenant_id,
                remaining,
                retry=False,
                since_ms=since_ms,
            )

    return PendingMediaCheck(
        selected_top_level=len(top_level),
        selected_nested=len(nested),
    )


def _signal_media_worker() -> bool:
    """Touch one coalescing signal consumed by the configured worker runner."""
    path = os.environ.get("MEDIA_EVENT_SIGNAL_PATH", "").strip() or _DEFAULT_SIGNAL_PATH
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o750, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o640)
        try:
            os.utime(path, None)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _tenant_service_gate(tenant_id: str) -> str | None:
    """Stable denial code for the media capability, or None when allowed."""
    try:
        with Session(get_engine()) as session:
            status = (
                session.query(Tenant.lifecycle_status)
                .filter(Tenant.id == tenant_id)
                .scalar()
            )
    except Exception:  # noqa: BLE001 -- dispatch must fail closed without DB detail
        logger.error(
            "media_worker tenant=%s trigger=dispatch-failed error_class=tenant_unavailable",
            _tenant_tag(tenant_id),
        )
        return "service_unavailable"
    return tenant_service_denial(status, WORKER_MEDIA)


def dispatch_media_worker(
    trigger_source: str = "archive-complete", tenant_id: str | None = None
) -> MediaWorkerDispatch:
    """Request a non-blocking generic media-worker run after archive commit.

    The signal carries no task data and cannot grow: it is one mtime update.
    The configured worker runner starts the existing generic media CLI, which
    owns candidate selection, the media lock, retry state, SDK lifecycle, and
    persistence. This read-only preflight prevents no-media archive runs from
    emitting a meaningless worker wake-up. A caller that already resolved a
    tenant passes ``tenant_id``; missing tenant context fails closed rather
    than deriving archive ownership from ambient process configuration.
    """
    source = _safe_source(trigger_source)
    if not _enabled():
        logger.info("media_worker trigger_source=%s trigger=no-work reason=disabled", source)
        return MediaWorkerDispatch.NO_WORK

    if tenant_id is None:
        logger.error("media_worker trigger_source=%s trigger=dispatch-failed error_class=tenant_unavailable", source)
        return MediaWorkerDispatch.FAILED

    # RND-402: gate at tenant resolution — a frozen/suspended tenant must
    # never wake the media worker (authoritative gate stays in the worker
    # CLI itself; this only avoids emitting a pointless signal).
    denial = _tenant_service_gate(tenant_id)
    if denial is not None:
        logger.info(
            "media_worker tenant=%s trigger_source=%s trigger=skipped error_code=%s",
            _tenant_tag(tenant_id),
            source,
            denial,
        )
        return MediaWorkerDispatch.SKIPPED

    try:
        pending = _pending_media_check(tenant_id)
    except Exception:  # noqa: BLE001 -- preflight must never affect committed archive data
        logger.error("media_worker trigger_source=%s trigger=dispatch-failed error_class=preflight_failed", source)
        return MediaWorkerDispatch.FAILED

    if not pending.selected_total:
        logger.info(
            "media_worker tenant=%s trigger_source=%s trigger=no-work pending_media_selected=0",
            _tenant_tag(tenant_id),
            source,
        )
        return MediaWorkerDispatch.NO_WORK

    try:
        signal_written = _signal_media_worker()
    except Exception:  # noqa: BLE001 -- wake-up failure must not affect committed archive data
        signal_written = False
    if not signal_written:
        logger.error("media_worker trigger_source=%s trigger=dispatch-failed error_class=signal_failed", source)
        return MediaWorkerDispatch.FAILED

    logger.info(
        "media_worker tenant=%s trigger_source=%s trigger=accepted pending_media_selected=%d",
        _tenant_tag(tenant_id),
        source,
        pending.selected_total,
    )
    return MediaWorkerDispatch.ACCEPTED


def shutdown() -> None:
    """Backward-compatible no-op; dispatch owns no in-process worker thread."""
