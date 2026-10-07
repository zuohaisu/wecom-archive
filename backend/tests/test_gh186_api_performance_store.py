"""GH-186 persistence tests: atomic batch dedup, increment merge, retention
cleanup, daily independence from hourly rows, hist version mixing, and the
read-side queries. Runs on in-memory SQLite (JSONB compiled to JSON)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    ApiPerformanceAlertState,
    ApiPerformanceEndpointDaily,
    ApiPerformanceEndpointHourly,
    ApiPerformanceFlushBatch,
)
from app.services.api_performance_collector import (
    HIST_SIZE,
    ApiPerformanceCollector,
    Observation,
)
from app.services.api_performance_store import (
    FLUSH_ALREADY_APPLIED,
    FLUSH_APPLIED,
    flush_deltas,
    query_bucket_series,
    query_coverage,
    query_endpoint_rows,
    query_endpoint_summaries,
    run_retention,
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


def _collector_obs(duration_us, completed_at=None, route="/api/x", status_class="2xx"):
    return Observation(
        method="GET",
        route=route,
        traffic_class="business",
        status_class=status_class,
        duration_us=duration_us,
        is_stream=False,
        stream_error=False,
        stream_ttfb_us=None,
        completed_at=completed_at or datetime(2026, 10, 8, 10, 30, tzinfo=UTC),
    )


def _drain_hourly(collector: ApiPerformanceCollector):
    batch_id, deltas = collector.drain_deltas()
    collector.mark_flushed(batch_id, FLUSH_APPLIED)
    return [d for d in deltas if d.granularity == "hourly"]


# ---------------------------------------------------------------------------
# Flush: apply, merge, dedup
# ---------------------------------------------------------------------------


def test_flush_applies_increments_and_merges(db: Session):
    collector = ApiPerformanceCollector(instance_id="inst-a")
    collector.record(_collector_obs(100_000))
    collector.record(_collector_obs(300_000))
    hourly = _drain_hourly(collector)
    assert flush_deltas(db, "b1", "inst-a", hourly) == FLUSH_APPLIED

    row = db.query(ApiPerformanceEndpointHourly).one()
    assert row.request_count == 2
    assert row.duration_sum_us == 400_000
    assert row.duration_min_us == 100_000
    assert row.duration_max_us == 300_000
    assert row.success_count == 2
    assert row.hist is not None and sum(row.hist) == 2

    # Second cycle in the same hour merges into the same row.
    collector.record(_collector_obs(600_000))
    hourly2 = _drain_hourly(collector)
    assert flush_deltas(db, "b2", "inst-a", hourly2) == FLUSH_APPLIED
    db.expire_all()
    row = db.query(ApiPerformanceEndpointHourly).one()
    assert row.request_count == 3
    assert row.duration_sum_us == 1_000_000
    assert row.duration_min_us == 100_000, "interval min kept across merges"
    assert row.duration_max_us == 600_000
    assert sum(row.hist) == 3
    assert row.hist_version == 1


def test_same_batch_id_is_applied_exactly_once(db: Session):
    collector = ApiPerformanceCollector(instance_id="inst-a")
    collector.record(_collector_obs(100_000))
    hourly = _drain_hourly(collector)
    assert flush_deltas(db, "inst-a:1", "inst-a", hourly) == FLUSH_APPLIED
    # Unknown commit outcome -> the SAME batch id is retried.
    assert flush_deltas(db, "inst-a:1", "inst-a", hourly) == FLUSH_ALREADY_APPLIED
    db.expire_all()
    row = db.query(ApiPerformanceEndpointHourly).one()
    assert row.request_count == 1, "dedup must prevent double apply"
    assert row.duration_sum_us == 100_000
    assert db.query(ApiPerformanceFlushBatch).count() == 1


def test_empty_deltas_skip_rows_but_batch_row_lands(db: Session):
    collector = ApiPerformanceCollector(instance_id="inst-a")
    collector.record(_collector_obs(100_000))
    hourly = _drain_hourly(collector)
    flush_deltas(db, "b1", "inst-a", hourly)
    # Nothing new: drain yields no deltas at all.
    batch_id, deltas = collector.drain_deltas()
    assert deltas == []
    assert flush_deltas(db, batch_id, "inst-a", deltas) == FLUSH_APPLIED
    assert db.query(ApiPerformanceEndpointHourly).count() == 1


# ---------------------------------------------------------------------------
# Retention & daily independence
# ---------------------------------------------------------------------------


def _insert_row(model, bucket, *, age_days=0, route="/api/old"):
    row = model(
        method="GET", route=route, traffic_class="business",
        request_count=1, class_counts={}, stats_version=1, hist_version=1,
        **bucket,
    )
    return row


def test_retention_deletes_only_expired_rows_of_this_module(db: Session):
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    db.add(ApiPerformanceEndpointHourly(
        method="GET", route="/api/old", traffic_class="business",
        bucket_start=now - timedelta(hours=721),
        request_count=1, class_counts={}, stats_version=1, hist_version=1,
    ))
    db.add(ApiPerformanceEndpointHourly(
        method="GET", route="/api/new", traffic_class="business",
        bucket_start=now - timedelta(hours=1),
        request_count=1, class_counts={}, stats_version=1, hist_version=1,
    ))
    db.add(ApiPerformanceEndpointDaily(
        method="GET", route="/api/oldday", traffic_class="business",
        bucket_date=(now - timedelta(days=181)).date(),
        request_count=1, class_counts={}, stats_version=1, hist_version=1,
    ))
    db.add(ApiPerformanceEndpointDaily(
        method="GET", route="/api/newday", traffic_class="business",
        bucket_date=(now - timedelta(days=1)).date(),
        request_count=1, class_counts={}, stats_version=1, hist_version=1,
    ))
    db.add(ApiPerformanceFlushBatch(batch_id="old:1", instance_id="old", flush_seq=1, created_at=now - timedelta(hours=72)))
    db.add(ApiPerformanceFlushBatch(batch_id="new:1", instance_id="new", flush_seq=1, created_at=now - timedelta(hours=1)))
    db.commit()

    deleted = run_retention(db, now=now, retention_hours=720, retention_days=180, batch_retention_hours=48)
    assert deleted["hourly"] == 1
    assert deleted["daily"] == 1
    assert deleted["batches"] == 1
    remaining_hourly = {r.route for r in db.query(ApiPerformanceEndpointHourly).all()}
    assert remaining_hourly == {"/api/new"}
    remaining_daily = {r.route for r in db.query(ApiPerformanceEndpointDaily).all()}
    assert remaining_daily == {"/api/newday"}
    assert {b.batch_id for b in db.query(ApiPerformanceFlushBatch).all()} == {"new:1"}


def test_daily_trend_survives_hourly_cleanup(db: Session):
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    day = (now - timedelta(days=100)).date()
    db.add(ApiPerformanceEndpointDaily(
        method="GET", route="/api/survivor", traffic_class="business",
        bucket_date=day, request_count=7, duration_sum_us=7_000,
        completed_count=7, success_count=7, class_counts={"2xx": 7},
        stats_version=1, hist_version=1, hist=[0] * HIST_SIZE,
    ))
    db.commit()
    run_retention(db, now=now, retention_hours=720, retention_days=180, batch_retention_hours=48)
    db.expire_all()
    survivor = db.query(ApiPerformanceEndpointDaily).one()
    assert survivor.bucket_date == day and survivor.request_count == 7
    points = query_bucket_series(
        db, ApiPerformanceEndpointDaily,
        since=day - timedelta(days=1), until=day + timedelta(days=1),
    )
    assert len(points) == 1 and points[0]["stats"].request_count == 7


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def test_summaries_include_registered_no_sample_and_retired_routes(db: Session):
    collector = ApiPerformanceCollector()
    collector.register_route("/api/live", {"GET"})
    collector.register_route("/api/never-called", {"GET"})
    collector.record(_collector_obs(100_000, route="/api/live"))
    flush_deltas(db, "b1", collector.instance_id, _drain_hourly(collector))
    collector2 = ApiPerformanceCollector()
    collector2.register_route("/api/live", {"GET"})
    collector2.register_route("/api/never-called", {"GET"})
    summaries = query_endpoint_summaries(
        db, collector2,
        since_hour=datetime(2026, 10, 8, 10, 0, tzinfo=UTC),
        until_hour=datetime(2026, 10, 8, 11, 0, tzinfo=UTC),
    )
    by_route = {s["route"]: s for s in summaries}
    assert by_route["/api/live"]["stats"].request_count == 1
    assert by_route["/api/live"]["registered"] is True
    assert by_route["/api/never-called"]["stats"].request_count == 0, "no-sample route still listed"
    # A route with rows but no longer registered = retired; history kept.
    summaries_full = query_endpoint_summaries(
        db, ApiPerformanceCollector(),
        since_hour=datetime(2026, 10, 8, 10, 0, tzinfo=UTC),
        until_hour=datetime(2026, 10, 8, 11, 0, tzinfo=UTC),
    )
    retired = {s["route"]: s for s in summaries_full}
    assert retired["/api/live"]["registered"] is False


def test_site_wide_series_merges_endpoints_weighted(db: Session):
    collector = ApiPerformanceCollector()
    collector.record(_collector_obs(100_000, route="/api/a"))
    collector.record(_collector_obs(300_000, route="/api/b"))
    flush_deltas(db, "b1", collector.instance_id, _drain_hourly(collector))
    points = query_bucket_series(
        db, ApiPerformanceEndpointHourly,
        since=datetime(2026, 10, 8, 10, 0, tzinfo=UTC),
        until=datetime(2026, 10, 8, 11, 0, tzinfo=UTC),
    )
    assert len(points) == 1
    stats = points[0]["stats"]
    assert stats.request_count == 2
    assert stats.avg_us == 200_000, "site average is weighted by count, not averaged of averages"
    assert points[0]["endpoint_count"] == 2


def test_hist_version_mixed_rows_make_p95_unavailable(db: Session):
    db.add(ApiPerformanceEndpointHourly(
        method="GET", route="/api/mixed", traffic_class="business",
        bucket_start=datetime(2026, 10, 8, 10, 0, tzinfo=UTC),
        request_count=1, success_count=1, completed_count=1,
        duration_sum_us=100_000, class_counts={"2xx": 1},
        stats_version=1, hist_version=1, hist=[0] * HIST_SIZE,
    ))
    db.add(ApiPerformanceEndpointHourly(
        method="GET", route="/api/mixed", traffic_class="business",
        bucket_start=datetime(2026, 10, 8, 11, 0, tzinfo=UTC),
        request_count=1, success_count=1, completed_count=1,
        duration_sum_us=200_000, class_counts={"2xx": 1},
        stats_version=1, hist_version=99, hist=[1] + [0] * (HIST_SIZE - 1),
    ))
    db.commit()
    points = query_bucket_series(
        db, ApiPerformanceEndpointHourly,
        since=datetime(2026, 10, 8, 0, tzinfo=UTC),
        until=datetime(2026, 10, 9, 0, tzinfo=UTC),
        route="/api/mixed",
    )
    per_hour = [p["stats"] for p in points]
    assert all(stats.hist is not None for stats in per_hour)
    merged = per_hour[0]
    for stats in per_hour[1:]:
        merged.hist = [a + b for a, b in zip(merged.hist, stats.hist)]
    # Direct cross-version addition is forbidden: the merged-series helper
    # must report unavailability instead.
    summary = query_endpoint_summaries(
        db, ApiPerformanceCollector(),
        since_hour=datetime(2026, 10, 8, 0, 0, tzinfo=UTC),
        until_hour=datetime(2026, 10, 9, 0, 0, tzinfo=UTC),
    )
    target = next(s for s in summary if s["route"] == "/api/mixed")
    assert target["stats"].hist is None and target["stats"].hist_unavailable is True
    assert target["stats"].p95_estimate() == (None, False)


def test_query_row_cap_bounds_fetch(db: Session):
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    for index in range(20):
        db.add(ApiPerformanceEndpointHourly(
            method="GET", route=f"/api/r{index}", traffic_class="business",
            bucket_start=now - timedelta(hours=index),
            request_count=1, class_counts={}, stats_version=1, hist_version=1,
        ))
    db.commit()
    rows = query_endpoint_rows(
        db, ApiPerformanceEndpointHourly,
        since=now - timedelta(hours=48), until=now + timedelta(hours=1),
    )
    assert len(rows) == 20


def test_coverage_reports_persisted_bounds(db: Session):
    coverage = query_coverage(db)
    assert coverage["hourly"]["rows"] == 0
    db.add(ApiPerformanceEndpointHourly(
        method="GET", route="/api/c", traffic_class="business",
        bucket_start=datetime(2026, 10, 1, 0, tzinfo=UTC),
        request_count=1, class_counts={}, stats_version=1, hist_version=1,
    ))
    db.commit()
    coverage = query_coverage(db)
    assert coverage["hourly"]["rows"] == 1
    assert coverage["hourly"]["earliest"] is not None


def test_rows_without_success_do_not_poison_merged_p95(db: Session):
    """A row with only errors (hist_version=0, hist=None) must not turn a
    later successful row's histogram into 'unavailable' (regression)."""
    bucket = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    db.add(ApiPerformanceEndpointHourly(
        method="GET", route="/api/err-only", traffic_class="business",
        bucket_start=bucket, request_count=5, completed_count=5,
        class_counts={"4xx": 5}, stats_version=1, hist_version=0, hist=None,
    ))
    db.add(ApiPerformanceEndpointHourly(
        method="GET", route="/api/with-success", traffic_class="business",
        bucket_start=bucket, request_count=20, success_count=20,
        completed_count=20, success_duration_sum_us=20 * 100_000,
        duration_sum_us=20 * 100_000, class_counts={"2xx": 20},
        stats_version=1, hist_version=1,
        hist=[0, 0, 0, 0, 20] + [0] * (HIST_SIZE - 5),
    ))
    db.commit()
    points = query_bucket_series(
        db, ApiPerformanceEndpointHourly,
        since=datetime(2026, 10, 8, 0, tzinfo=UTC),
        until=datetime(2026, 10, 9, 0, tzinfo=UTC),
    )
    assert len(points) == 1
    stats = points[0]["stats"]
    assert stats.request_count == 25
    assert stats.hist is not None and stats.hist_unavailable is False
    p95, capped = stats.p95_estimate()
    assert p95 == 250 * 1000 and capped is False
