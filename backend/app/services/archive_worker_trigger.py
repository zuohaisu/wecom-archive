"""Bounded, non-blocking dispatch for the shared archive worker.

Every trigger starts the existing ``run_archive_worker_once.py`` entrypoint.
That script owns the cross-process ``fcntl.flock`` shared with the systemd
timer and manual trigger, so this module never adds a second filesystem lock
or reimplements sync/decrypt orchestration.  Its process-local lock only
coalesces callback dispatches while one callback-launched child is pending,
which bounds thread creation during an event burst.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_WORKER_SCRIPT = _BACKEND_DIR / "scripts" / "run_archive_worker_once.py"
_dispatch_lock = threading.Lock()


class ArchiveWorkerDispatch(str, Enum):
    """Safe outcomes visible to a callback without exposing worker internals."""

    ACCEPTED = "accepted"
    SKIPPED_LOCKED = "skipped-locked"
    FAILED = "dispatch-failed"


_TRIGGER_SOURCES = frozenset({"activation", "callback", "manual", "timer"})


def _safe_trigger_source(raw: str) -> str:
    return raw if raw in _TRIGGER_SOURCES else "manual"


def run_archive_worker_once(
    trigger_source: str = "manual", tenant_id: str | None = None
) -> bool:
    """Run the existing lock-owning worker and report only its exit outcome.

    ``tenant_id`` selects the per-tenant worker chain via child env; the raw
    id never appears in logs (the worker script logs its digest tag only).
    """
    source = _safe_trigger_source(trigger_source)
    environment = os.environ.copy()
    environment["ARCHIVE_WORKER_TRIGGER_SOURCE"] = source
    if tenant_id is not None:
        environment["WECOM_TENANT_ID"] = tenant_id
    try:
        result = subprocess.run(
            [sys.executable, str(_WORKER_SCRIPT)],
            cwd=str(_BACKEND_DIR),
            check=False,
            env=environment,
        )
    except Exception:  # noqa: BLE001 -- callback dispatch must not expose subprocess detail
        logger.error("archive_worker trigger_source=%s trigger=dispatch-failed", source)
        return False

    if result.returncode:
        logger.error("archive_worker trigger_source=%s trigger=worker-failed", source)
        return False

    logger.info("archive_worker trigger_source=%s trigger=completed", source)
    return True


def _run_dispatched_worker(trigger_source: str, tenant_id: str | None) -> None:
    try:
        run_archive_worker_once(trigger_source=trigger_source, tenant_id=tenant_id)
    finally:
        _dispatch_lock.release()


def dispatch_archive_worker(
    trigger_source: str = "callback", tenant_id: str | None = None
) -> ArchiveWorkerDispatch:
    """Request one worker cycle without waiting for it or queuing unbounded work."""
    source = _safe_trigger_source(trigger_source)
    if not _dispatch_lock.acquire(blocking=False):
        logger.info(
            "archive_worker trigger_source=%s trigger=skipped-locked reason=dispatch_in_progress",
            source,
        )
        return ArchiveWorkerDispatch.SKIPPED_LOCKED

    try:
        thread = threading.Thread(
            target=_run_dispatched_worker,
            args=(source, tenant_id),
            name="wecom-archive-worker-trigger",
            daemon=True,
        )
        thread.start()
    except Exception:  # noqa: BLE001 -- a thread-start failure is intentionally isolated
        _dispatch_lock.release()
        logger.error("archive_worker trigger_source=%s trigger=dispatch-failed", source)
        return ArchiveWorkerDispatch.FAILED

    logger.info("archive_worker trigger_source=%s trigger=accepted", source)
    return ArchiveWorkerDispatch.ACCEPTED
