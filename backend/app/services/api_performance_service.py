"""GH-186 background orchestration: the 15-minute flush loop, the
short-window detection/email loop, and bounded retention cleanup.

One asyncio task per app instance, started by the composition root's
lifespan. Every DB touch runs in a worker THREAD via run_in_executor --
never on the event loop -- so a slow or lock-waiting database cannot
stall request handling. The telemetry writer uses its own tiny engine
with real server-side statement/lock timeouts, isolated from the request
pool. Every failure is bounded: in-memory increments survive a failed
flush and retry under the SAME batch id (inside the dedup retention
window); a graceful shutdown attempts one time-boxed final flush; a
crash simply loses the unflushed window, which the status page reports
as a possible gap.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Set

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.email import send_api_performance_alert_email
from app.services.api_performance_collector import (
    ApiPerformanceCollector,
    ApiPerformanceConfig,
)
from app.services.api_performance_detector import (
    EMAIL_UNCONFIGURED,
    SlowEndpointDetector,
    maybe_send_daily_alert,
)
from app.services.api_performance_store import (
    FlushConflictError,
    flush_deltas,
    run_retention,
)
from app.settings import get_database_settings

logger = logging.getLogger(__name__)

_RETENTION_INTERVAL_SECONDS = 6 * 3600
_FINAL_FLUSH_TIMEOUT_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 900.0
# Real server-side bounds for the telemetry writer: a stuck flush must
# release the worker thread (and the loop awaiting it) instead of hanging
# on an ambient default. SQLite (tests) skips these options.
_TELEMETRY_STATEMENT_TIMEOUT_MS = 5000
_TELEMETRY_LOCK_TIMEOUT_MS = 3000
_TELEMETRY_CONNECT_TIMEOUT_SECONDS = 5
_TELEMETRY_POOL_TIMEOUT_SECONDS = 5
_TELEMETRY_JOB_TIMEOUT_SECONDS = 30.0

_engine = None
_engine_lock = threading.Lock()
_engine_users = 0


def default_session_factory():
    """Session factory for the telemetry background loop, on a dedicated
    small engine with server-enforced connection/statement/lock bounds.

    Deliberately separate from app.db.session's request engine: the flush
    is a low-frequency background writer and must neither contend for the
    request pool nor inherit its unbounded statement duration. Engine
    creation is serialized because several worker threads may initialize it
    concurrently on a cold start."""
    global _engine
    with _engine_lock:
        if _engine is None:
            url = get_database_settings().database_url.strip()
            if not url:
                raise RuntimeError("DATABASE_URL environment variable is not set")
            kwargs = dict(
                pool_size=1,
                max_overflow=1,
                pool_pre_ping=True,
                pool_timeout=_TELEMETRY_POOL_TIMEOUT_SECONDS,
            )
            if url.startswith("postgresql"):
                kwargs["connect_args"] = {
                    "connect_timeout": _TELEMETRY_CONNECT_TIMEOUT_SECONDS,
                    "options": (
                        f"-c statement_timeout={_TELEMETRY_STATEMENT_TIMEOUT_MS}"
                        f" -c lock_timeout={_TELEMETRY_LOCK_TIMEOUT_MS}"
                    ),
                }
            _engine = create_engine(url, **kwargs)
        return Session(_engine)


def _acquire_default_engine_lifecycle() -> None:
    global _engine_users
    with _engine_lock:
        _engine_users += 1


def _release_default_engine_lifecycle() -> None:
    global _engine, _engine_users
    engine_to_dispose = None
    with _engine_lock:
        if _engine_users > 0:
            _engine_users -= 1
        if _engine_users == 0 and _engine is not None:
            engine_to_dispose = _engine
            _engine = None
    if engine_to_dispose is not None:
        engine_to_dispose.dispose()


def default_send_fn(to_email: str, subject: str, body: str, *, operation_id: str) -> bool:
    return send_api_performance_alert_email(to_email, subject, body, operation_id=operation_id)


def _flush_job(session_factory: Callable, batch_id: str, instance_id: str, deltas: list, config: ApiPerformanceConfig) -> str:
    """Blocking flush transaction; runs in a worker thread under a full
    monotonic job deadline. Retention bounds reject already-expired rows."""
    deadline = time.monotonic() + _TELEMETRY_JOB_TIMEOUT_SECONDS
    with session_factory() as session:  # type: ignore[operator]
        return flush_deltas(
            session,
            batch_id,
            instance_id,
            deltas,
            now=datetime.now(timezone.utc),
            retention_hours=config.retention_hours,
            retention_days=config.retention_days,
            job_deadline_monotonic=deadline,
        )


def _cleanup_job(session_factory: Callable, config: ApiPerformanceConfig) -> dict:
    """Blocking retention cleanup; runs in a worker thread under the same
    full-job budget as the batched flush."""
    deadline = time.monotonic() + _TELEMETRY_JOB_TIMEOUT_SECONDS
    with session_factory() as session:  # type: ignore[operator]
        return run_retention(
            session,
            now=datetime.now(timezone.utc),
            retention_hours=config.retention_hours,
            retention_days=config.retention_days,
            batch_retention_hours=config.batch_retention_hours,
            job_deadline_monotonic=deadline,
        )


def _alert_email_job(runtime, anomalies, now, session_factory, send_fn) -> str:
    """Blocking email quota/send job; runs in a worker thread. Persist-
    before-send and the daily quota live inside maybe_send_daily_alert."""
    with session_factory() as session:  # type: ignore[operator]
        return maybe_send_daily_alert(
            session,
            anomalies,
            config=runtime.config,
            collector=runtime.collector,
            now=now,
            send_fn=send_fn,
        )


@dataclass
class AlertEmailStatus:
    """Honest, page-displayable email state; never fabricated. The
    authoritative per-day state is the persisted alert row (the status
    API reads it); this field tracks this process's own transitions."""

    status: str = "not_attempted"
    updated_at: Optional[datetime] = None
    anomaly_count: int = 0


class ApiPerformanceRuntime:
    """Per-app wiring object: collector + config + detector + loop state."""

    def __init__(self, collector: ApiPerformanceCollector, config: ApiPerformanceConfig) -> None:
        self.collector = collector
        self.config = config
        self.detector = SlowEndpointDetector(config) if config.detection_enabled else None
        self.email = AlertEmailStatus(
            status=EMAIL_UNCONFIGURED if not config.alert_email else "not_attempted"
        )
        self.last_anomalies: List = field(default_factory=list)
        self.last_retention: Optional[dict] = None
        self.last_retention_at: Optional[datetime] = None
        self.loop_started_at: Optional[datetime] = None
        self.stop_event: Optional[asyncio.Event] = None
        self._next_flush_mono: float = 0.0
        self._flush_backoff_seconds: float = 60.0
        self._flush_job_future: Optional[asyncio.Future] = None
        self._flush_job_batch_id: Optional[str] = None
        self._worker_jobs: Set[asyncio.Future] = set()


def _due_delay(next_mono: float) -> float:
    return max(next_mono - time.monotonic(), 0.05)


def _submit_worker(runtime: ApiPerformanceRuntime, loop, function: Callable, *args):
    future = loop.run_in_executor(None, function, *args)
    runtime._worker_jobs.add(future)

    def _forget(completed):
        runtime._worker_jobs.discard(completed)
        if not completed.cancelled():
            # A caller can be cancelled while the thread continues. Consume
            # its eventual exception even if no coroutine remains awaiting it.
            completed.exception()

    future.add_done_callback(_forget)
    return future


async def _join_worker_jobs(runtime: ApiPerformanceRuntime) -> None:
    """Wait for every owned executor job before its engine can be disposed."""
    while runtime._worker_jobs:
        jobs = tuple(runtime._worker_jobs)
        await asyncio.gather(*(asyncio.shield(job) for job in jobs), return_exceptions=True)


def _finish_flush_job(runtime: ApiPerformanceRuntime, batch_id: str, future) -> None:
    if runtime._flush_job_future is not future:
        return
    try:
        outcome = future.result()
    except FlushConflictError:
        runtime.collector.mark_flushed(batch_id, "conflict")
        _schedule_flush_retry(runtime)
    except Exception as error:  # noqa: BLE001 - DB outages are expected and retried
        runtime.collector.mark_flushed(batch_id, f"error:{type(error).__name__}")
        logger.warning("api performance flush outcome=error error_type=%s", type(error).__name__)
        _schedule_flush_retry(runtime)
    else:
        runtime.collector.mark_flushed(batch_id, outcome)
        runtime._flush_backoff_seconds = 60.0
        runtime._next_flush_mono = time.monotonic() + runtime.config.flush_interval_seconds
    finally:
        runtime._flush_job_future = None
        runtime._flush_job_batch_id = None


async def run_background_loop(
    runtime: ApiPerformanceRuntime,
    *,
    session_factory: Callable[[], object] = default_session_factory,
    send_fn: Callable = default_send_fn,
) -> None:
    """Runs until stop_event is set or the task is cancelled. On any stop,
    attempts one time-boxed final flush so a graceful deploy keeps at most
    the in-flight seconds, not the full 15 minutes, of unflushed stats."""
    config = runtime.config
    runtime.loop_started_at = datetime.now(timezone.utc)
    owns_default_engine = session_factory is default_session_factory
    if owns_default_engine:
        _acquire_default_engine_lifecycle()
    stop_event = asyncio.Event()
    runtime.stop_event = stop_event
    runtime._next_flush_mono = time.monotonic() + config.flush_interval_seconds
    next_detection_mono = time.monotonic() + config.detection_interval_seconds
    next_cleanup_mono = time.monotonic() + _RETENTION_INTERVAL_SECONDS
    try:
        while not stop_event.is_set():
            soonest = min(runtime._next_flush_mono, next_detection_mono, next_cleanup_mono)
            # Cap the wait so externally-adjusted schedules (and the stop
            # event path) are noticed promptly; the wakeup cost is trivial.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=min(_due_delay(soonest), 1.0))
                break
            except asyncio.TimeoutError:
                pass
            now_mono = time.monotonic()
            if now_mono >= runtime._next_flush_mono:
                await _flush_once(runtime, session_factory)
            if config.detection_enabled and runtime.detector is not None and now_mono >= next_detection_mono:
                await _detect_once(runtime, session_factory, send_fn)
                next_detection_mono = time.monotonic() + config.detection_interval_seconds
            if now_mono >= next_cleanup_mono:
                await _cleanup_once(runtime, session_factory)
                next_cleanup_mono = time.monotonic() + _RETENTION_INTERVAL_SECONDS
    except asyncio.CancelledError:
        raise
    finally:
        # Graceful shutdown attempts a time-boxed final flush only when this
        # process observed traffic. If an earlier worker still owns the
        # frozen batch, _flush_once joins that same job instead of submitting
        # a competing transaction. Executor workers are joined before engine
        # disposal; PostgreSQL connect, statement, lock and whole-job bounds
        # keep this wait finite for the production factory.
        try:
            if runtime.collector.total_observations > 0:
                try:
                    await asyncio.wait_for(
                        _flush_once(runtime, session_factory, final=True), timeout=_FINAL_FLUSH_TIMEOUT_SECONDS
                    )
                except Exception:  # noqa: BLE001 - shutdown must proceed to joining its owned worker
                    logger.warning("api performance final flush did not complete cleanly")
            await _join_worker_jobs(runtime)
        finally:
            if owns_default_engine:
                _release_default_engine_lifecycle()


async def _flush_once(runtime: ApiPerformanceRuntime, session_factory: Callable, *, final: bool = False) -> None:
    future = runtime._flush_job_future
    batch_id = runtime._flush_job_batch_id
    if future is None:
        if time.monotonic() < runtime._next_flush_mono and not final:
            return
        batch_id, deltas = runtime.collector.drain_deltas()
        loop = asyncio.get_running_loop()
        future = _submit_worker(
            runtime, loop, _flush_job, session_factory, batch_id,
            runtime.collector.instance_id, deltas, runtime.config,
        )
        runtime._flush_job_future = future
        runtime._flush_job_batch_id = batch_id
        future.add_done_callback(lambda completed: _finish_flush_job(runtime, batch_id, completed))
    elif future.done():
        _finish_flush_job(runtime, batch_id, future)
        return

    try:
        # Shielding preserves ownership when an awaiter is cancelled: the
        # DB thread continues, and shutdown joins this exact Future rather
        # than draining and submitting the same batch a second time.
        await asyncio.shield(future)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The done callback records the result and schedules retry policy.
        pass
    finally:
        if future.done():
            _finish_flush_job(runtime, batch_id, future)


async def _detect_once(runtime: ApiPerformanceRuntime, session_factory: Callable, send_fn: Callable) -> None:
    detector = runtime.detector
    if detector is None:
        return
    anomalies = detector.evaluate(runtime.collector)
    runtime.last_anomalies = anomalies
    if not anomalies or not runtime.config.alert_email:
        if runtime.email.status == "not_attempted" and not anomalies:
            runtime.email.status = "not_triggered"
            runtime.email.updated_at = datetime.now(timezone.utc)
        return
    now = datetime.now(timezone.utc)
    loop = asyncio.get_running_loop()
    try:
        future = _submit_worker(
            runtime, loop, _alert_email_job,
            runtime, anomalies, now, session_factory, send_fn,
        )
        status = await asyncio.shield(future)
    except Exception as error:  # noqa: BLE001 - a quota/DB failure inside the
        # email job must never kill the telemetry loop (QA finding 2): the
        # daily intent stays unclaimed and the next evaluation retries.
        logger.warning("api performance alert job outcome=error error_type=%s", type(error).__name__)
        runtime.email.status = "job_failed"
        runtime.email.updated_at = datetime.now(timezone.utc)
        return
    # Only real send outcomes update the in-process status. The persisted
    # per-day row (read by the status API) stays authoritative;
    # ``already_claimed`` must never overwrite an earlier result.
    if status != "already_claimed":
        runtime.email.status = status
        runtime.email.updated_at = datetime.now(timezone.utc)
        runtime.email.anomaly_count = len(anomalies)


async def _cleanup_once(runtime: ApiPerformanceRuntime, session_factory: Callable) -> None:
    loop = asyncio.get_running_loop()
    try:
        future = _submit_worker(runtime, loop, _cleanup_job, session_factory, runtime.config)
        deleted = await asyncio.shield(future)
        runtime.last_retention = deleted
        runtime.last_retention_at = datetime.now(timezone.utc)
    except Exception as error:  # noqa: BLE001 - cleanup retries next cycle
        logger.warning("api performance retention outcome=error error_type=%s", type(error).__name__)


def _schedule_flush_retry(runtime: ApiPerformanceRuntime) -> None:
    """Exponential backoff with a hard ceiling; never longer than a
    normal flush interval would wait, so the 15-minute cadence remains
    the upper bound even while the DB is down."""
    backoff = min(runtime._flush_backoff_seconds, _MAX_BACKOFF_SECONDS)
    runtime._flush_backoff_seconds = min(runtime._flush_backoff_seconds * 2, _MAX_BACKOFF_SECONDS)
    runtime._next_flush_mono = time.monotonic() + backoff
