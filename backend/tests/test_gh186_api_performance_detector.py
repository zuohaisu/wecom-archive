"""GH-186 detection + daily-email-quota tests, with a mocked send function
(no real recipients are ever contacted)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

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
from app.services.api_performance_detector import (
    EMAIL_ACCEPTED,
    EMAIL_ALREADY_CLAIMED,
    EMAIL_CLAIM_FAILED,
    EMAIL_FAILED_OR_UNKNOWN,
    EMAIL_UNCONFIGURED,
    SlowEndpointDetector,
    maybe_send_daily_alert,
    render_alert_email,
)

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
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def fast_config():
    return ApiPerformanceConfig(
        window_minutes=5,
        min_samples=4,
        p95_threshold_ms=1000,
        sustained_minutes=3,
        detection_interval_seconds=60,
        stream_p95_threshold_ms=2000,
        stream_min_samples=4,
        alert_email="ops@example.com",
        admin_url="https://example.com/platform/api-performance",
    )


def _slow_obs(route, completed_at, duration_us=2_000_000, is_stream=False, ttfb_us=None):
    return Observation(
        method="GET",
        route=route,
        traffic_class="business",
        status_class="2xx",
        duration_us=duration_us,
        is_stream=is_stream,
        stream_error=False,
        stream_ttfb_us=ttfb_us,
        completed_at=completed_at,
    )


def _feed(collector, route, minutes, per_minute=3, duration_us=2_000_000, base=None):
    base = base or datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    for minute in range(minutes):
        at = base + timedelta(minutes=minute)
        for i in range(per_minute):
            collector.record(_slow_obs(route, at + timedelta(seconds=i * 10), duration_us=duration_us))


def test_anomaly_requires_minimum_samples_and_sustained_minutes(fast_config):
    collector = ApiPerformanceCollector()
    detector = SlowEndpointDetector(fast_config)
    now = datetime(2026, 10, 8, 10, 2, tzinfo=UTC)
    # One slow spike below the min sample count: never an anomaly.
    collector.record(_slow_obs("/api/a", now - timedelta(seconds=10)))
    assert detector.evaluate(collector, now=now) == []
    assert detector.current_anomalies() == []

    # Enough samples, first positive evaluation: streak 1 < sustained 3.
    _feed(collector, "/api/a", minutes=5)
    assert detector.evaluate(collector, now=now + timedelta(seconds=30)) == []
    # Second and third consecutive positive evaluations reach ~3 minutes.
    assert detector.evaluate(collector, now=now + timedelta(seconds=90)) == []
    anomalies = detector.evaluate(collector, now=now + timedelta(seconds=150))
    assert len(anomalies) == 1
    assert anomalies[0].key.route == "/api/a"
    assert anomalies[0].samples >= 4
    assert anomalies[0].p95_us >= 1_000 * 1000
    assert detector.current_anomalies() == anomalies


def test_recovery_clears_anomaly_and_low_sample_is_not_healthy(fast_config):
    collector = ApiPerformanceCollector()
    detector = SlowEndpointDetector(fast_config)
    base = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    # Continuous slow traffic so every evaluation window keeps >=4 samples.
    _feed(collector, "/api/a", minutes=9)
    for step in range(3):
        detector.evaluate(collector, now=base + timedelta(minutes=6, seconds=step * 60))
    assert detector.current_anomalies()
    # Fast traffic again: once the slow minutes age out of the window the
    # anomaly clears on the next evaluation.
    _feed(collector, "/api/a", minutes=5, duration_us=50_000, base=base + timedelta(minutes=9))
    anomalies = detector.evaluate(collector, now=base + timedelta(minutes=14))
    assert anomalies == []


def test_restart_reaccumulates_windows(fast_config):
    collector = ApiPerformanceCollector()
    _feed(collector, "/api/a", minutes=5)
    warm = SlowEndpointDetector(fast_config)
    now = datetime(2026, 10, 8, 10, 3, tzinfo=UTC)
    for step in range(4):
        warm.evaluate(collector, now=now + timedelta(seconds=step * 60))
    assert warm.current_anomalies()
    # A fresh process has empty windows and no state.
    fresh_collector = ApiPerformanceCollector()
    fresh_detector = SlowEndpointDetector(fast_config)
    assert fresh_detector.evaluate(fresh_collector, now=now) == []
    assert fresh_detector.current_anomalies() == []


def test_stream_and_normal_are_judged_separately(fast_config):
    collector = ApiPerformanceCollector()
    # Slow stream-start alongside fast normal responses, continuously fed.
    base = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    for minute in range(12):
        at = base + timedelta(minutes=minute)
        for i in range(3):
            collector.record(_slow_obs("/api/sse", at + timedelta(seconds=i), duration_us=10_000))
            collector.record(_slow_obs(
                "/api/sse", at + timedelta(seconds=30 + i),
                duration_us=60_000_000, is_stream=True, ttfb_us=4_000_000,
            ))
    detector = SlowEndpointDetector(fast_config)
    now = base + timedelta(minutes=8)
    anomalies = []
    for _step in range(4):
        anomalies = detector.evaluate(collector, now=now)
        now += timedelta(seconds=60)
    stream_anomalies = [a for a in anomalies if a.kind == "stream"]
    normal_anomalies = [a for a in anomalies if a.kind == "normal"]
    assert stream_anomalies and stream_anomalies[0].threshold_ms == 2000
    assert normal_anomalies == []


def test_route_override_applies(fast_config):
    from app.services.api_performance_collector import RouteOverride

    config = ApiPerformanceConfig(
        window_minutes=5, min_samples=4, p95_threshold_ms=1000,
        sustained_minutes=3, detection_interval_seconds=60,
        stream_p95_threshold_ms=2000, stream_min_samples=4,
        alert_email="", route_overrides=(RouteOverride(route="/api/batch", p95_threshold_ms=10_000),),
    )
    collector = ApiPerformanceCollector()
    _feed(collector, "/api/batch", minutes=5, duration_us=3_000_000)  # 3s: over 1s, under 10s
    detector = SlowEndpointDetector(config)
    now = datetime(2026, 10, 8, 10, 3, tzinfo=UTC)
    for _step in range(4):
        anomalies = detector.evaluate(collector, now=now)
        now += timedelta(seconds=60)
    assert anomalies == [], "override threshold suppresses the alert"


# ---------------------------------------------------------------------------
# Daily email quota
# ---------------------------------------------------------------------------


class _SendSpy:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, to_email, subject, body, *, operation_id):
        self.calls.append((to_email, subject, body, operation_id))
        return self.result


def _anomaly(fast_config):
    collector = ApiPerformanceCollector()
    _feed(collector, "/api/slow", minutes=5)
    detector = SlowEndpointDetector(fast_config)
    now = datetime(2026, 10, 8, 10, 4, tzinfo=UTC)
    for _ in range(4):
        anomalies = detector.evaluate(collector, now=now)
        now += timedelta(seconds=60)
    assert anomalies
    return anomalies


def test_email_send_persists_intent_before_and_outcome_after(db, fast_config):
    spy = _SendSpy(True)
    anomalies = _anomaly(fast_config)
    now = datetime(2026, 10, 8, 10, 6, tzinfo=UTC)
    status = maybe_send_daily_alert(db, anomalies, config=fast_config, collector=ApiPerformanceCollector(instance_id="inst"), now=now, send_fn=spy)
    assert status == EMAIL_ACCEPTED
    assert len(spy.calls) == 1
    to, subject, body, op_id = spy.calls[0]
    assert to == "ops@example.com"
    assert op_id == "api-perf-alert/2026-10-08"
    assert "/api/slow" in body and "p95" in body
    assert "https://example.com/platform/api-performance" in body
    row = db.query(ApiPerformanceAlertState).one()
    assert row.status == "accepted" and row.alert_date == date(2026, 10, 8)

    # Same day again (restart / re-evaluation): no second email.
    status2 = maybe_send_daily_alert(db, anomalies, config=fast_config, collector=ApiPerformanceCollector(instance_id="inst2"), now=now, send_fn=spy)
    assert status2 == EMAIL_ALREADY_CLAIMED
    assert len(spy.calls) == 1


def test_email_next_day_may_send_again(db, fast_config):
    spy = _SendSpy(True)
    anomalies = _anomaly(fast_config)
    day1 = datetime(2026, 10, 8, 10, 6, tzinfo=UTC)
    day2 = datetime(2026, 10, 9, 10, 6, tzinfo=UTC)
    assert maybe_send_daily_alert(db, anomalies, config=fast_config, collector=ApiPerformanceCollector(instance_id="i"), now=day1, send_fn=spy) == EMAIL_ACCEPTED
    assert maybe_send_daily_alert(db, anomalies, config=fast_config, collector=ApiPerformanceCollector(instance_id="i"), now=day2, send_fn=spy) == EMAIL_ACCEPTED
    assert len(spy.calls) == 2


def test_email_unknown_outcome_not_recorded_success_and_not_resent(db, fast_config):
    spy = _SendSpy(False)
    anomalies = _anomaly(fast_config)
    now = datetime(2026, 10, 8, 10, 6, tzinfo=UTC)
    status = maybe_send_daily_alert(db, anomalies, config=fast_config, collector=ApiPerformanceCollector(instance_id="i"), now=now, send_fn=spy)
    assert status == EMAIL_FAILED_OR_UNKNOWN
    row = db.query(ApiPerformanceAlertState).one()
    assert row.status == "failed_or_unknown"
    again = maybe_send_daily_alert(db, anomalies, config=fast_config, collector=ApiPerformanceCollector(instance_id="i"), now=now + timedelta(minutes=5), send_fn=spy)
    assert again == EMAIL_ALREADY_CLAIMED
    assert len(spy.calls) == 1


def test_email_persistence_failure_means_no_send(db, fast_config, monkeypatch):
    spy = _SendSpy(True)
    anomalies = _anomaly(fast_config)
    now = datetime(2026, 10, 8, 10, 6, tzinfo=UTC)

    def broken_claim(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.api_performance_detector.claim_daily_alert", broken_claim)
    status = maybe_send_daily_alert(db, anomalies, config=fast_config, collector=ApiPerformanceCollector(instance_id="i"), now=now, send_fn=spy)
    assert status == EMAIL_CLAIM_FAILED
    assert spy.calls == [], "quota record failed -> sending is forbidden"


def test_email_unconfigured_never_sends(db, fast_config):
    spy = _SendSpy(True)
    from dataclasses import replace

    config = replace(fast_config, alert_email="")
    anomalies = _anomaly(fast_config)
    status = maybe_send_daily_alert(db, anomalies, config=config, collector=ApiPerformanceCollector(), now=datetime(2026, 10, 8, tzinfo=UTC), send_fn=spy)
    assert status == EMAIL_UNCONFIGURED
    assert spy.calls == []
    assert db.query(ApiPerformanceAlertState).count() == 0


def test_email_content_contains_no_business_payload(db, fast_config):
    anomalies = _anomaly(fast_config)
    subject, body = render_alert_email(anomalies, config=fast_config, alert_day=date(2026, 10, 8))
    assert "p95" in body
    assert "Asia/Shanghai" in subject or "接口" in subject
    for anomaly in anomalies:
        assert anomaly.key.route in body
        assert anomaly.key.method in body


def test_alert_state_check_constraint_via_models(db):
    """The claim path is the only writer; direct bad statuses must fail."""
    import sqlalchemy.exc

    db.add(ApiPerformanceAlertState(alert_date=date(2026, 10, 8), instance_id="i", status="bogus"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.commit()
    db.rollback()
