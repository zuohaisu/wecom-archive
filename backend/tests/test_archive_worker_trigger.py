"""RND-107 contracts for bounded dispatch of the shared archive worker."""

from __future__ import annotations

import logging
from threading import Event
from unittest.mock import MagicMock

from app.services import archive_worker_trigger as trigger


def test_manual_and_callback_paths_share_the_worker_entrypoint(monkeypatch) -> None:
    calls: list[tuple[object, object]] = []

    def _run(args, *, cwd, check):
        calls.append((args, cwd))
        assert check is False
        return MagicMock(returncode=0)

    monkeypatch.setattr(trigger.subprocess, "run", _run)

    assert trigger.run_archive_worker_once() is True
    assert calls == [
        (
            [trigger.sys.executable, str(trigger._WORKER_SCRIPT)],
            str(trigger._BACKEND_DIR),
        )
    ]


def test_worker_failure_is_observable_without_exception_text(monkeypatch, caplog) -> None:
    sentinel = "SENTINEL_TRACEBACK secret=/sentinel/fs/path"
    monkeypatch.setattr(
        trigger.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(sentinel)),
    )

    assert trigger.run_archive_worker_once() is False

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "archive_worker result=failed" in log_text
    assert sentinel not in log_text


def test_burst_is_coalesced_and_dispatch_returns_before_worker_finishes(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger=trigger.__name__)
    started = Event()
    release = Event()
    finished = Event()
    runs: list[object] = []

    def _blocking_worker() -> bool:
        runs.append(1)
        started.set()
        assert release.wait(timeout=1)
        finished.set()
        return True

    monkeypatch.setattr(trigger, "run_archive_worker_once", _blocking_worker)

    first = trigger.dispatch_archive_worker()
    assert first is trigger.ArchiveWorkerDispatch.ACCEPTED
    assert started.wait(timeout=1)
    assert not finished.is_set()

    burst = [trigger.dispatch_archive_worker() for _ in range(20)]
    assert burst == [trigger.ArchiveWorkerDispatch.SKIPPED] * 20
    assert runs == [1]
    assert "archive_worker trigger=skipped reason=dispatch_in_progress" in "\n".join(
        record.getMessage() for record in caplog.records
    )

    release.set()
    assert finished.wait(timeout=1)
    assert trigger._dispatch_lock.acquire(timeout=1)
    trigger._dispatch_lock.release()


def test_dispatch_start_failure_releases_the_coalescing_guard(monkeypatch, caplog) -> None:
    class _BrokenThread:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("SENTINEL_TRACEBACK")

    monkeypatch.setattr(trigger.threading, "Thread", _BrokenThread)

    assert trigger.dispatch_archive_worker() is trigger.ArchiveWorkerDispatch.FAILED
    assert trigger.dispatch_archive_worker() is trigger.ArchiveWorkerDispatch.FAILED
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "archive_worker trigger=dispatch_failed" in log_text
    assert "SENTINEL_TRACEBACK" not in log_text
