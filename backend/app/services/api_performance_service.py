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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

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
_TELEMETRY_STATEMENT_TIMEOUT_MS = 10000
_TELEMETRY_LOCK_TIMEOUT_MS = 5000

_engine = None


def default_session_factory():
    """Session factory for the telemetry background loop, on a dedicated
    small engine with server-enforced statement/lock timeouts.

    Deliberately separate from app.db.session's request engine: the flush
    is a low-frequency background writer and must neither contend for the
    request pool nor inherit its unbounded statement duration."""
    global _engine
    if _engine is None:
        url = get_database_settings().database_url.strip()
        if not url:
            raise RuntimeError("DATABASE_URL environment variable is not set")
        kwargs = dict(pool_size=1, max_overflow=1, pool_pre_ping=True)
        if url.startswith("postgresql"):
            kwargs["connect_args"] = {
                "options": (
                    f"-c statement_timeout={_TELEMETRY_STATEMENT_TIMEOUT_MS}"
                    f" -c lock_timeout={_TELEMETRY_LOCK_TIMEOUT_MS}"
                )
            }
        _engine = create_engine(url, **kwargs)
    return Session(_engine)


def default_send_fn(to_email: str, subject: str, body: str, *, operation_id: str) -> bool:
    return send_api_performance_alert_email(to_email, subject, body, operation_id=operation_id)


def _flush_job(session_factory: Callable, batch_id: str, instance_id: str, deltas: list, config: ApiPerformanceConfig) -> str:
    """Blocking flush transaction; runs in a worker thread. Retention
    bounds are passed so increments for already-expired buckets are
    rejected (late retries must not resurrect cleaned-up rows)."""
    with session_factory() as session:  # type: ignore[operator]
        return flush_deltas(
            session,
            batch_id,
            instance_id,
            deltas,
            now=datetime.now(timezone.utc),
            retention_hours=config.retention_hours,
            retention_days=config.retention_days,
        )


def _cleanup_job(session_factory: Callable, config: ApiPerformanceConfig) -> dict:
    """Blocking retention cleanup; runs in a worker thread."""
    with session_factory() as session:  # type: ignore[operator]
        return run_retention(
            session,
            now=datetime.now(timezone.utc),
            retention_hours=config.retention_hours,
            retention_days=config.retention_days,
            batch_retention_hours=config.batch_retention_hours,
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


def _due_delay(next_mono: float) -> float:
    return max(next_mono - time.monotonic(), 0.05)


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
        # Graceful shutdown: one time-boxed final flush, but only when this
        # process actually observed traffic -- an idle app must not touch
        # the DB at all. The job runs off-loop with server-side timeouts;
        # if it outlives this wait the exit joins the worker thread,
        # bounded by the statement timeout.
        if runtime.collector.total_observations > 0:
            try:
                await asyncio.wait_for(
                    _flush_once(runtime, session_factory, final=True), timeout=_FINAL_FLUSH_TIMEOUT_SECONDS
                )
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - shutdown must proceed
                logger.warning("api performance final flush did not complete cleanly")


async def _flush_once(runtime: ApiPerformanceRuntime, session_factory: Callable, *, final: bool = False) -> None:
    now_mono = time.monotonic()
    if now_mono < runtime._next_flush_mono and not final:
        return
    batch_id, deltas = runtime.collector.drain_deltas()
    loop = asyncio.get_running_loop()
    outcome: str
    try:
        # The whole transaction runs in a worker thread: a slow or
        # lock-waiting DB must never block the event loop.
        outcome = await loop.run_in_executor(
            None, _flush_job, session_factory, batch_id, runtime.collector.instance_id, deltas, runtime.config
        )
        runtime.collector.mark_flushed(batch_id, outcome)
        runtime._flush_backoff_seconds = 60.0
        runtime._next_flush_mono = time.monotonic() + runtime.config.flush_interval_seconds
    except FlushConflictError:
        # Row inserted concurrently mid-batch: batch unapplied; the same
        # batch id retries after a short backoff.
        runtime.collector.mark_flushed(batch_id, "conflict")
        _schedule_flush_retry(runtime)
    except Exception as error:  # noqa: BLE001 - DB outages are expected, bounded, and retried
        runtime.collector.mark_flushed(batch_id, f"error:{type(error).__name__}")
        logger.warning("api performance flush outcome=error error_type=%s", type(error).__name__)
        _schedule_flush_retry(runtime)


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
        status = await loop.run_in_executor(
            None,
            _alert_email_job,
            runtime,
            anomalies,
            now,
            session_factory,
            send_fn,
        )
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
        deleted = await loop.run_in_executor(None, _cleanup_job, session_factory, runtime.config)
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
