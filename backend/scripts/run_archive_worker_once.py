"""
Shared archive worker entrypoint with file lock.

Acquires a process-level file lock so that at most one worker runs at a time
(regardless of whether it was triggered by a systemd timer, an event, or a
manual CLI invocation). Runs the sync script first, then the decrypt script;
after their successful commits it non-blockingly requests the existing generic
media worker only when fresh/pending media exists.

Usage (from backend/):
    python scripts/run_archive_worker_once.py

Required environment variables:
    WORKER_LOCK_PATH    Absolute path to shared lock file
                        (default: /srv/apps/wecom-archive-365/shared/run/wecom-archive-worker.lock)

    All env vars required by sync_wecom_archive_once.py and
    decrypt_wecom_messages_once.py (DATABASE_URL, WECOM_SDK_LIB_PATH, …).

Exit codes:
    0  Success, or lock already held (safe no-op).
    1  Fatal failure (env missing, lock directory creation failed,
       or sync/decrypt subprocess failed).

Safety constraints:
    - No message content, secrets, private keys, or raw customer data is printed.
    - Subprocess stdout/stderr is not captured or logged (the child scripts
      handle their own safe logging).
    - The archive-complete media wake-up never changes sync/decrypt success or
      failure truth; the generic media worker owns a distinct shared lock.
    - Every exit emits one aggregate lifecycle line with result, safe error
      class, completed_at, duration, and child CPU/RSS; exception/configuration
      text is never echoed into that line or a failure line.
"""

from __future__ import annotations

import fcntl
import os
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone

# Allow running this shared entrypoint directly from backend/ without an
# installed package, matching the other worker shells.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.media_event_dispatch import MediaWorkerDispatch, dispatch_media_worker


class ArchiveWorkerExit(SystemExit):
    """A safe, classified expected archive-worker exit."""

    def __init__(self, code: int, error_class: str, result: str = "failed") -> None:
        super().__init__(code)
        self.error_class = error_class
        self.result = result


def _fail(error_class: str, message: str) -> None:
    """Emit only fixed safe failure text before the final lifecycle line."""
    print(f"[FAIL] archive_worker error_class={error_class} {message}", flush=True)
    raise ArchiveWorkerExit(1, error_class)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        _fail("configuration_missing", f"Environment variable not set or empty: {name}")
    return value


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: str):
    """Acquire an exclusive fcntl.flock on *lock_path*.

    Creates the parent directory if it is missing and the process has
    permission to do so. Returns the open file descriptor. A held lock is a
    classified, successful no-op; lock paths and OS exception text never
    reach the journal.
    """
    lock_dir = os.path.dirname(lock_path)

    try:
        if lock_dir and not os.path.isdir(lock_dir):
            os.makedirs(lock_dir, mode=0o750, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    except OSError:
        _fail("lock_unavailable", "Cannot create or open archive worker lock")

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        source = _trigger_source()
        print(
            f"[INFO] archive_worker trigger_source={source} trigger=skipped-locked",
            flush=True,
        )
        print("archive_worker lock already held; exiting", flush=True)
        raise ArchiveWorkerExit(0, "none", result="skipped")
    except OSError:
        os.close(lock_fd)
        _fail("lock_unavailable", "Cannot acquire archive worker lock")

    return lock_fd


def _release_lock(lock_fd: int) -> None:
    """Release the flock and close the file descriptor."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Subprocess runners
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)

_SYNC_SCRIPT = os.path.join(_SCRIPTS_DIR, "sync_wecom_archive_once.py")
_DECRYPT_SCRIPT = os.path.join(_SCRIPTS_DIR, "decrypt_wecom_messages_once.py")
_REACHABILITY_AUTOMATION_SCRIPT = os.path.join(
    _SCRIPTS_DIR, "run_reachability_automation_once.py"
)


def _run_best_effort_reachability_automation() -> None:
    """Run incremental diagnosis without changing archive-worker truth."""
    try:
        proc = subprocess.run(
            [sys.executable, _REACHABILITY_AUTOMATION_SCRIPT, "incremental"],
            cwd=_BACKEND_DIR,
            capture_output=False,
            check=False,
        )
        if proc.returncode:
            print(
                "[WARN] archive_worker reachability mode=incremental status=failed count=0",
                flush=True,
            )
        else:
            print(
                "[INFO] archive_worker reachability mode=incremental status=finished count=0",
                flush=True,
            )
    except Exception:  # noqa: BLE001 -- best-effort diagnostic must never expose a traceback
        # No traceback or child exception is safe to expose from this
        # best-effort ancillary task.
        print(
            "[WARN] archive_worker reachability mode=incremental status=error count=0",
            flush=True,
        )


def _run_script(script_path: str, label: str) -> None:
    """Run *script_path* as a subprocess; exits the current process with
    code 1 if it returns non-zero."""
    print(f"[INFO] archive_worker starting: {label}", flush=True)
    # Inherit the current environment so the child sees all env vars
    proc = subprocess.run(
        [sys.executable, script_path],
        cwd=_BACKEND_DIR,
        capture_output=False,   # child writes to our stdout/stderr directly
        check=False,
    )
    if proc.returncode != 0:
        print(
            "[FAIL] archive_worker error_class=child_worker_failed "
            f"child_exit_code={proc.returncode}",
            flush=True,
        )
        raise ArchiveWorkerExit(1, "child_worker_failed")
    print(f"[INFO] archive_worker finished: {label}", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_DEFAULT_LOCK_PATH = (
    "/srv/apps/wecom-archive-365/shared/run/wecom-archive-worker.lock"
)
_TRIGGER_SOURCES = frozenset({"callback", "manual", "timer"})


def _trigger_source() -> str:
    raw = os.environ.get("ARCHIVE_WORKER_TRIGGER_SOURCE", "manual").strip()
    return raw if raw in _TRIGGER_SOURCES else "manual"


def _request_media_worker_after_archive() -> MediaWorkerDispatch:
    """Wake the existing generic media worker without changing archive truth."""
    try:
        return dispatch_media_worker(trigger_source="archive-complete")
    except Exception:  # noqa: BLE001 -- archive success must not depend on media wake-up
        # The dispatcher itself handles expected DB/spawn failures.  This
        # final isolation guard ensures a future dispatcher regression can
        # never turn an already-committed archive run into a failure.
        return MediaWorkerDispatch.FAILED


def main() -> None:
    source = _trigger_source()
    started = time.monotonic()
    children_started = resource.getrusage(resource.RUSAGE_CHILDREN)
    started_child_cpu = children_started.ru_utime + children_started.ru_stime
    lock_fd = None
    completed = False
    result = "failed"
    error_class = "unexpected_failure"
    exit_code = 1

    try:
        lock_path = os.environ.get("WORKER_LOCK_PATH", _DEFAULT_LOCK_PATH).strip()
        if not lock_path:
            _fail("configuration_missing", "WORKER_LOCK_PATH is empty")

        try:
            lock_fd = _acquire_lock(lock_path)
            print(f"[INFO] archive_worker trigger_source={source} trigger=accepted", flush=True)
            _run_script(_SYNC_SCRIPT, "sync_wecom_archive_once.py")
            _run_script(_DECRYPT_SCRIPT, "decrypt_wecom_messages_once.py")
            _run_best_effort_reachability_automation()
            completed = True
        finally:
            if lock_fd is not None:
                _release_lock(lock_fd)
                lock_fd = None

        # The archive lock is intentionally released before this request. The
        # bounded signal is consumed by a separate systemd event service,
        # whose existing generic media worker owns its own shared media lock.
        if completed:
            media_dispatch = _request_media_worker_after_archive()
            print(
                f"[INFO] archive_worker media_trigger_source=archive-complete "
                f"media_trigger={media_dispatch.value}",
                flush=True,
            )

        result = "completed"
        error_class = "none"
        exit_code = 0
    except ArchiveWorkerExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        result = exc.result
        error_class = exc.error_class
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        result = "completed" if exit_code == 0 else "failed"
        error_class = "none" if exit_code == 0 else "unclassified_exit"
    except Exception:  # noqa: BLE001 -- safe worker boundary intentionally hides exception detail
        # Never expose a child/process exception or traceback from this
        # orchestration wrapper. Sync/decrypt scripts retain their existing
        # explicit safe result lines and the reconciliation timer will retry.
        print(
            f"[FAIL] archive_worker trigger_source={source} "
            "error_class=unexpected_failure",
            flush=True,
        )
        exit_code = 1
        result = "failed"
        error_class = "unexpected_failure"
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd)
        duration_ms = int((time.monotonic() - started) * 1000)
        children_finished = resource.getrusage(resource.RUSAGE_CHILDREN)
        child_cpu_ms = int(
            ((children_finished.ru_utime + children_finished.ru_stime) - started_child_cpu) * 1000
        )
        completed_at = datetime.now(timezone.utc).isoformat()
        print(
            f"[INFO] archive_worker trigger_source={source} lifecycle=ended "
            f"result={result} exit_code={exit_code} error_class={error_class} "
            f"completed_at={completed_at} duration_ms={duration_ms} "
            f"child_cpu_ms={child_cpu_ms} peak_rss_kb={children_finished.ru_maxrss}",
            flush=True,
        )

    if result == "completed":
        print("[PASS] run_archive_worker_once completed successfully", flush=True)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
