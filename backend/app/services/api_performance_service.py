"""GH-186 background orchestration: the 15-minute flush loop, the
short-window detection/email loop, and bounded retention cleanup.

One asyncio task per app instance, started by the composition root's
lifespan. Every DB touch happens here (never on the request path) and
every failure is bounded: in-memory increments survive a failed flush and
retry under the SAME batch id; a graceful shutdown attempts one
time-boxed final flush; a crash simply loses the unflushed window, which
the status page reports as a possible gap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

from app.db.session import Session, get_engine
from app.email import send_api_performance_alert_email
from app.services.api_performance_collector import ApiPerformanceCollector, ApiPerformanceConfig
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

logger = logging.getLogger(__name__)

_RETENTION_INTERVAL_SECONDS = 6 * 3600
_FINAL_FLUSH_TIMEOUT_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 900.0


def default_session_factory():
    return Session(get_engine())


def default_send_fn(to_email: str, subject: str, body: str, *, operation_id: str) -> bool:
    return send_api_performance_alert_email(to_email, subject, body, operation_id=operation_id)


@dataclass
class AlertEmailStatus:
    """Honest, page-displayable email state; never fabricated."""

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
        # the DB at all.
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
    outcome: str
    try:
        with session_factory() as session:  # type: ignore[operator]
            outcome = flush_deltas(session, batch_id, runtime.collector.instance_id, deltas)
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
    status = await loop.run_in_executor(
        None,
        _alert_email_job,
        runtime,
        anomalies,
        now,
        session_factory,
        send_fn,
    )
    runtime.email.status = status
    runtime.email.updated_at = datetime.now(timezone.utc)
    runtime.email.anomaly_count = len(anomalies)


def _alert_email_job(runtime, anomalies, now, session_factory, send_fn) -> str:
    # Runs in the default executor: the provider call is blocking by design
    # (one bounded attempt inside app.email). Persist-before-send and the
    # daily quota live inside maybe_send_daily_alert.
    with session_factory() as session:  # type: ignore[operator]
        return maybe_send_daily_alert(
            session,
            anomalies,
            config=runtime.config,
            collector=runtime.collector,
            now=now,
            send_fn=send_fn,
        )


async def _cleanup_once(runtime: ApiPerformanceRuntime, session_factory: Callable) -> None:
    config = runtime.config
    try:
        with session_factory() as session:  # type: ignore[operator]
            deleted = run_retention(
                session,
                now=datetime.now(timezone.utc),
                retention_hours=config.retention_hours,
                retention_days=config.retention_days,
                batch_retention_hours=config.batch_retention_hours,
            )
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
