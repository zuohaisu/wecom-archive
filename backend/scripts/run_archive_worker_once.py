#!/usr/bin/env python3
"""
Shared archive worker entrypoint with file lock.

Acquires a process-level file lock so that at most one worker runs at a time
(regardless of whether it was triggered by a systemd timer, an event, or a
manual CLI invocation).  Runs the sync script first, then the decrypt script.

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
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: str):
    """Acquire an exclusive fcntl.flock on *lock_path*.

    Creates the parent directory if it is missing and the process has
    permission to do so.  Returns the open file descriptor.  Exits 0 if the
    lock is already held by another process.
    """
    lock_dir = os.path.dirname(lock_path)

    if not os.path.isdir(lock_dir):
        try:
            os.makedirs(lock_dir, mode=0o750, exist_ok=True)
        except OSError as exc:
            print(
                f"[FAIL] Cannot create lock directory {lock_dir}: {exc}",
                flush=True,
            )
            sys.exit(1)

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        print("archive_worker lock already held; exiting", flush=True)
        sys.exit(0)

    return lock_fd


def _release_lock(lock_fd: int) -> None:
    """Release the flock and close the file descriptor."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(lock_fd)
    except Exception:
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
    except Exception:
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
    )
    if proc.returncode != 0:
        print(
            f"[FAIL] archive_worker subprocess {label} exited with code {proc.returncode}",
            flush=True,
        )
        sys.exit(1)
    print(f"[INFO] archive_worker finished: {label}", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_DEFAULT_LOCK_PATH = (
    "/srv/apps/wecom-archive-365/shared/run/wecom-archive-worker.lock"
)


def main() -> None:
    lock_path = os.environ.get("WORKER_LOCK_PATH", _DEFAULT_LOCK_PATH).strip()
    if not lock_path:
        print("[FAIL] WORKER_LOCK_PATH is empty", flush=True)
        sys.exit(1)

    lock_fd = _acquire_lock(lock_path)

    try:
        _run_script(_SYNC_SCRIPT, "sync_wecom_archive_once.py")
        _run_script(_DECRYPT_SCRIPT, "decrypt_wecom_messages_once.py")
        _run_best_effort_reachability_automation()
    finally:
        _release_lock(lock_fd)

    print("[PASS] run_archive_worker_once completed successfully", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
