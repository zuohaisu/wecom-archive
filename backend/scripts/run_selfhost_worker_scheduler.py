"""Run the self-host worker fallback schedule without external cron software.

The web process remains responsible for callback-triggered archive work. This
container worker runs the existing one-shot commands as bounded fallbacks; it
does not implement new business processing or run cloud billing jobs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

BACKEND_DIR = Path(__file__).resolve().parents[1]
SIGNAL_POLL_SECONDS = 1.0
PROCESS_STOP_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger("selfhost_worker_scheduler")


@dataclass(frozen=True)
class ScheduledJob:
    """One existing one-shot command and its minimum retry cadence."""

    name: str
    interval_seconds: int
    arguments: tuple[str, ...]
    signal_path_env: str | None = None


SELFHOST_JOBS = (
    ScheduledJob("archive-sync", 5 * 60, ("scripts/run_archive_worker_once.py",)),
    ScheduledJob(
        "external-contact-refresh",
        15 * 60,
        ("scripts/refresh_external_contacts_once.py",),
        "EXTERNAL_CONTACT_REFRESH_SIGNAL_PATH",
    ),
    ScheduledJob(
        "external-contact-reconcile",
        24 * 60 * 60,
        ("-m", "app.services.external_contact_sync"),
    ),
    ScheduledJob(
        "media-download",
        30 * 60,
        (
            "scripts/download_wecom_media_once.py",
            "--since-hours",
            "72",
            "--newest-first",
            "--limit",
            "20",
            "--retry",
            "--trigger-source",
            "timer",
        ),
        "MEDIA_EVENT_SIGNAL_PATH",
    ),
    ScheduledJob("export-jobs", 5 * 60, ("scripts/process_export_jobs_once.py",)),
    ScheduledJob("ai-kb-reindex", 60 * 60, ("scripts/run_ai_kb_reindex.py",)),
    ScheduledJob(
        "ai-retention-sweep",
        24 * 60 * 60,
        ("scripts/run_ai_retention_sweep_once.py",),
    ),
)


async def _stop_process(
    process: asyncio.subprocess.Process, wait_task: asyncio.Task[int]
) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(wait_task, timeout=PROCESS_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await wait_task


async def _run_command(job: ScheduledJob, stop_event: asyncio.Event) -> int:
    """Run one command with inherited configuration and clean shutdown."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *job.arguments,
        cwd=BACKEND_DIR,
    )
    wait_task = asyncio.create_task(process.wait())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _pending = await asyncio.wait(
            (wait_task, stop_task), return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done and not wait_task.done():
            await _stop_process(process, wait_task)
        return await wait_task
    except asyncio.CancelledError:
        await _stop_process(process, wait_task)
        raise
    finally:
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)


def _signal_signature(job: ScheduledJob) -> tuple[str, int] | None:
    if job.signal_path_env is None:
        return None
    raw_path = os.environ.get(job.signal_path_env, "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        # A missing/unreadable signal must not disable its periodic fallback.
        return None


async def _run_job(
    job: ScheduledJob,
    stop_event: asyncio.Event,
    *,
    run_once: Callable[[ScheduledJob, asyncio.Event], Awaitable[int]] = _run_command,
) -> None:
    """Run serially on startup, at the fallback cadence, or after a signal."""
    last_signal = _signal_signature(job)
    next_run = time.monotonic()
    while not stop_event.is_set():
        signature = _signal_signature(job)
        signal_changed = signature is not None and signature != last_signal
        last_signal = signature
        now = time.monotonic()
        if now >= next_run or signal_changed:
            try:
                result = await run_once(job, stop_event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- never log command/environment data
                logger.error(
                    "job=%s status=failed error_class=%s", job.name, type(exc).__name__
                )
                result = 1
            if result:
                logger.error("job=%s status=failed exit_code=%d", job.name, result)
            else:
                logger.info("job=%s status=completed", job.name)
            next_run = time.monotonic() + job.interval_seconds
            continue

        timeout = next_run - now
        if job.signal_path_env is not None:
            timeout = min(timeout, SIGNAL_POLL_SECONDS)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(timeout, 0.01))
        except TimeoutError:
            pass


async def run_scheduler(
    stop_event: asyncio.Event,
    jobs: tuple[ScheduledJob, ...] = SELFHOST_JOBS,
    *,
    run_once: Callable[[ScheduledJob, asyncio.Event], Awaitable[int]] = _run_command,
) -> None:
    """Supervise independent jobs; one slow task cannot delay another."""
    tasks = [
        asyncio.create_task(_run_job(job, stop_event, run_once=run_once), name=job.name)
        for job in jobs
    ]
    try:
        await stop_event.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)
    logger.info("started edition=selfhost job_count=%d", len(SELFHOST_JOBS))
    await run_scheduler(stop_event)
    logger.info("stopped")


def main() -> int:
    asyncio.run(_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
