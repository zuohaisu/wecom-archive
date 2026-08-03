"""Bounded, non-blocking dispatch for the shared archive worker.

Every trigger starts the existing ``run_archive_worker_once.py`` entrypoint.
That script owns the cross-process ``fcntl.flock`` shared with the systemd
timer and manual trigger, so this module never adds a second filesystem lock
or reimplements sync/decrypt orchestration.  Its process-local lock only
coalesces callback dispatches while one callback-launched child is pending,
which bounds thread creation during an event burst.
"""

from __future__ import annotations

from enum import Enum
import logging
from pathlib import Path
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_WORKER_SCRIPT = _BACKEND_DIR / "scripts" / "run_archive_worker_once.py"
_dispatch_lock = threading.Lock()


class ArchiveWorkerDispatch(str, Enum):
    """Safe outcomes visible to a callback without exposing worker internals."""

    ACCEPTED = "accepted"
    SKIPPED = "skipped"
    FAILED = "failed"


def run_archive_worker_once() -> bool:
    """Run the existing lock-owning worker and report only its exit outcome."""
    try:
        result = subprocess.run(
            [sys.executable, str(_WORKER_SCRIPT)],
            cwd=str(_BACKEND_DIR),
            check=False,
        )
    except Exception:
        logger.error("archive_worker result=failed")
        return False

    if result.returncode:
        logger.error("archive_worker result=failed")
        return False

    logger.info("archive_worker result=completed")
    return True


def _run_dispatched_worker() -> None:
    try:
        run_archive_worker_once()
    finally:
        _dispatch_lock.release()


def dispatch_archive_worker() -> ArchiveWorkerDispatch:
    """Request one worker cycle without waiting for it or queuing unbounded work."""
    if not _dispatch_lock.acquire(blocking=False):
        logger.info("archive_worker trigger=skipped reason=dispatch_in_progress")
        return ArchiveWorkerDispatch.SKIPPED

    try:
        thread = threading.Thread(
            target=_run_dispatched_worker,
            name="wecom-archive-worker-trigger",
            daemon=True,
        )
        thread.start()
    except Exception:
        _dispatch_lock.release()
        logger.error("archive_worker trigger=dispatch_failed")
        return ArchiveWorkerDispatch.FAILED

    logger.info("archive_worker trigger=accepted")
    return ArchiveWorkerDispatch.ACCEPTED
