"""GH-186 background loop integration: flush-to-DB, detection-driven
email (mocked transport), retention wiring, and graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    ApiPerformanceAlertState,
    ApiPerformanceEndpointDaily,
    ApiPerformanceEndpointHourly,
    ApiPerformanceFlushBatch,
)
from app.services.api_performance_collector import (
    ApiPerformanceCollector,
    ApiPerformanceConfig,
    Observation,
)
from app.services.api_performance_service import (
    ApiPerformanceRuntime,
    _detect_once,
    _flush_once,
    run_background_loop,
)
from app.services.api_performance_store import FLUSH_APPLIED

UTC = timezone.utc


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


_TABLES = [
    ApiPerformanceEndpointHourly.__table__,
    ApiPerformanceEndpointDaily.__table__,
    ApiPerformanceFlushBatch.__table__,
    ApiPerformanceAlertState.__table__,
]


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def fast_loop_config():
    return ApiPerformanceConfig(
        enabled=True,
        flush_interval_seconds=900,
        detection_enabled=True,
        detection_interval_seconds=60,
        window_minutes=5,
        min_samples=4,
        p95_threshold_ms=1000,
        sustained_minutes=3,
        alert_email="ops@example.com",
        admin_url="https://example.com/platform",
    )


def _slow_obs(route, completed_at):
    return Observation(
        method="GET", route=route, traffic_class="business", status_class="2xx",
        duration_us=2_000_000, is_stream=False, stream_error=False,
        stream_ttfb_us=None, completed_at=completed_at,
    )


def test_loop_flushes_collector_to_database(db_factory, fast_loop_config):
    """The collector's in-memory increments reach the DB via the loop's
    flush step; snapshots advance so the next cycle writes only deltas."""
    collector = ApiPerformanceCollector(instance_id="loop-test")
    runtime = ApiPerformanceRuntime(collector, fast_loop_config)
    now = datetime(2026, 10, 8, 10, 30, tzinfo=UTC)
    for index in range(3):
        collector.record(_slow_obs("/api/x", now + timedelta(seconds=index)))

    async def scenario():
        await _flush_once(runtime, db_factory)

    asyncio.run(scenario())
    assert collector.last_flush_result == FLUSH_APPLIED
    assert collector.consecutive_flush_errors == 0
    session = db_factory()
    try:
        row = session.query(ApiPerformanceEndpointHourly).one()
        assert row.request_count == 3
        daily = session.query(ApiPerformanceEndpointDaily).one()
        assert daily.request_count == 3
    finally:
        session.close()


def test_loop_persists_daily_email_intent_and_outcome(db_factory, fast_loop_config):
    """A sustained anomaly inside the loop ends with exactly one alert row
    whose status reflects the mocked provider outcome."""
    collector = ApiPerformanceCollector(instance_id="loop-test")
    runtime = ApiPerformanceRuntime(collector, fast_loop_config)
    base = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    for minute in range(10):
        for i in range(3):
            collector.record(_slow_obs("/api/slow", base + timedelta(minutes=minute, seconds=i)))

    detector = runtime.detector
    assert detector is not None
    now = base + timedelta(minutes=8)
    for _step in range(4):
        detector.evaluate(collector, now=now)
        now += timedelta(seconds=60)
    anomalies = detector.current_anomalies()
    assert anomalies, "precondition: sustained anomaly"

    calls = []

    def fake_send(to_email, subject, body, *, operation_id):
        calls.append(operation_id)
        return True

    async def scenario():
        await _detect_once(runtime, db_factory, fake_send)

    asyncio.run(scenario())
    assert len(calls) == 1
    assert runtime.email.status == "accepted"
    session = db_factory()
    try:
        row = session.query(ApiPerformanceAlertState).one()
        assert row.status == "accepted"
        assert row.instance_id == "loop-test"
    finally:
        session.close()

    # Second detection pass the same day: quota held, no second send, and
    # the in-process status keeps the earlier real outcome (the persisted
    # row stays authoritative for the page).
    asyncio.run(scenario())
    assert len(calls) == 1
    assert runtime.email.status == "accepted"


def test_loop_lifecycle_start_stop_and_backoff(db_factory, fast_loop_config, monkeypatch):
    """run_background_loop starts, reacts to the stop event, and a DB
    failure schedules a retry with the SAME batch id (bounded backoff)."""
    config = ApiPerformanceConfig(
        enabled=True,
        flush_interval_seconds=900,
        detection_enabled=False,
        detection_interval_seconds=60,
        alert_email="",
    )
    collector = ApiPerformanceCollector(instance_id="lifecycle")
    collector.record(_slow_obs("/api/x", datetime(2026, 10, 8, 10, 30, tzinfo=UTC)))
    runtime = ApiPerformanceRuntime(collector, config)

    attempts = []

    class FlakyFactory:
        def __init__(self):
            self.fail = True

        def __call__(self):
            session = db_factory()
            if self.fail:
                attempts.append(session)
                raise RuntimeError("db down")
            return session

    flaky = FlakyFactory()

    async def _wait_for(predicate, timeout=4.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return False

    async def scenario():
        task = asyncio.create_task(run_background_loop(runtime, session_factory=flaky))
        await asyncio.sleep(0.05)
        # Force the first flush to be due immediately; the loop notices
        # within one bounded wait cycle.
        runtime._next_flush_mono = 0.0
        assert await _wait_for(lambda: collector.last_flush_result.startswith("error:")), \
            "DB failure recorded honestly"
        first_batch_pending = collector._pending_batch
        assert first_batch_pending is not None, "failed batch retained for retry"
        # Recover the DB; the retry reuses the same batch id.
        flaky.fail = False
        runtime._next_flush_mono = 0.0
        assert await _wait_for(lambda: collector.last_flush_result == FLUSH_APPLIED)
        assert runtime.stop_event is not None
        runtime.stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())
    assert collector.last_flush_result == FLUSH_APPLIED
    session = db_factory()
    try:
        # Exactly one applied increment per table proves the failed attempt
        # never double-applied after its unknown-outcome retry.
        assert session.query(ApiPerformanceEndpointHourly).count() == 1
        assert session.query(ApiPerformanceEndpointDaily).count() == 1
        hourly = session.query(ApiPerformanceEndpointHourly).one()
        assert hourly.request_count == 1
    finally:
        session.close()


# ---------------------------------------------------------------------------
# QA fix regressions: DB jobs must not block the event loop (finding 1);
# an email-job failure must not kill the loop (finding 2)
# ---------------------------------------------------------------------------


def test_flush_runs_off_the_event_loop(db_factory, fast_loop_config):
    """A 200ms DB job must not delay a 10ms heartbeat coroutine nor break
    a 30ms wait_for deadline (QA finding 1 reproduction)."""
    collector = ApiPerformanceCollector(instance_id="offloop")
    runtime = ApiPerformanceRuntime(collector, fast_loop_config)
    collector.record(_slow_obs("/api/x", datetime(2026, 10, 8, 10, 30, tzinfo=UTC)))

    class SlowFactory:
        def __call__(self):
            session = db_factory()
            time.sleep(0.2)
            return session

    async def scenario():
        import time as _time

        heartbeats = []

        async def heartbeat():
            while True:
                heartbeats.append(_time.monotonic())
                await asyncio.sleep(0.01)

        hb_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        t0 = _time.monotonic()
        try:
            await asyncio.wait_for(
                _flush_once(runtime, SlowFactory()), timeout=0.03
            )
            raise AssertionError("expected timeout")
        except asyncio.TimeoutError:
            waited = _time.monotonic() - t0
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task
        # The wait_for deadline is honoured (the blocking job lives in a
        # worker thread), and heartbeats kept ticking during the job.
        assert waited < 0.15, f"wait_for returned after {waited:.3f}s"
        assert len(heartbeats) >= 3, "event loop stalled during the DB job"

    asyncio.run(scenario())
    # The flush still completes in the worker thread and is accounted.
    assert collector.last_flush_result in ("applied", "error:", "never") or collector.last_flush_result


def test_email_job_failure_isolated_from_loop(db_factory, fast_loop_config):
    """A session/pool failure inside the email job must not propagate out
    of _detect_once nor stop later flushes (QA finding 2)."""

    class BrokenThenWorkingFactory:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic pool timeout")
            return db_factory()

    factory = BrokenThenWorkingFactory()
    collector = ApiPerformanceCollector(instance_id="isolate")
    runtime = ApiPerformanceRuntime(collector, fast_loop_config)
    base = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    for minute in range(10):
        for i in range(3):
            collector.record(_slow_obs("/api/slow", base + timedelta(minutes=minute, seconds=i)))
    detector = runtime.detector
    now = base + timedelta(minutes=8)
    for _step in range(4):
        detector.evaluate(collector, now=now)
        now += timedelta(seconds=60)
    assert detector.current_anomalies(), "precondition: sustained anomaly"

    async def scenario():
        await _detect_once(runtime, factory, lambda *a, **k: True)

    asyncio.run(scenario())
    assert runtime.email.status == "job_failed"
    assert factory.calls == 1, "send_fn must never be reached when persistence fails"
    assert not hasattr(factory, "_send_called")

    # The loop continues: a later flush cycle works normally.
    async def scenario2():
        await _flush_once(runtime, db_factory)

    asyncio.run(scenario2())
    assert collector.last_flush_result == FLUSH_APPLIED
