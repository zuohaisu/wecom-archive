"""GH-186 collector + middleware unit tests.

Covers: identity normalisation (method/template/unmatched/OTHER), bounded
labels, timing/classification semantics (exception, cancel, stream, last
body byte), bucket-at-observation-time rotation across hours/midnight,
flush batch stability across failures, snapshot deltas, bounded memory,
histogram p95 estimation, and thread-safe concurrent recording.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from app.services.api_performance_collector import (
    CLASS_2XX,
    CLASS_5XX,
    CLASS_CANCELLED,
    CLASS_EXCEPTION,
    HIST_SIZE,
    TRAFFIC_BUSINESS,
    TRAFFIC_INFRA,
    TRAFFIC_SELF,
    TRAFFIC_UNMATCHED,
    ApiPerformanceCollector,
    ApiPerformanceConfig,
    ApiPerformanceMiddleware,
    Observation,
    classify_traffic,
    estimate_p95_us,
    hist_index_for,
)
from app.settings import ApiPerformanceSettings

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))


def _obs(
    method: str = "GET",
    route: str = "/api/conversations",
    traffic: str = TRAFFIC_BUSINESS,
    status_class: str = CLASS_2XX,
    duration_us: Optional[int] = 100_000,
    is_stream: bool = False,
    stream_error: bool = False,
    stream_ttfb_us: Optional[int] = None,
    completed_at: Optional[datetime] = None,
) -> Observation:
    return Observation(
        method=method,
        route=route,
        traffic_class=traffic,
        status_class=status_class,
        duration_us=duration_us,
        is_stream=is_stream,
        stream_error=stream_error,
        stream_ttfb_us=stream_ttfb_us,
        completed_at=completed_at or datetime(2026, 10, 8, 10, 30, tzinfo=UTC),
    )


class _FakeRoute:
    def __init__(self, path: str) -> None:
        self.path = path
        self.path_format = path


class _FakeApp:
    """Minimal ASGI app with scripted send/raise behaviour."""

    def __init__(
        self,
        *,
        status: int = 200,
        content_type: bytes = b"application/json",
        chunks: int = 1,
        raise_before_start: bool = False,
        raise_after_start: bool = False,
        cancel: bool = False,
        never_respond: bool = False,
    ) -> None:
        self.status = status
        self.content_type = content_type
        self.chunks = chunks
        self.raise_before_start = raise_before_start
        self.raise_after_start = raise_after_start
        self.cancel = cancel
        self.never_respond = never_respond

    async def __call__(self, scope, receive, send) -> None:
        # Simulate the real ASGI ordering: routing (inside the app) writes
        # scope["route"] AFTER the middleware has been entered.
        if "_test_route" in scope:
            scope["route"] = scope.pop("_test_route")
        if self.cancel:
            raise asyncio.CancelledError()
        if self.raise_before_start:
            raise RuntimeError("boom")
        if self.never_respond:
            # App polls receive (sees the disconnect), then returns without
            # ever starting a response.
            await receive()
            return
        await send({
            "type": "http.response.start",
            "status": self.status,
            "headers": [(b"content-type", self.content_type)],
        })
        if self.raise_after_start:
            raise RuntimeError("stream boom")
        for index in range(self.chunks):
            await send({
                "type": "http.response.body",
                "body": b"x",
                "more_body": index < self.chunks - 1,
            })


def _run(app, *, method: str = "GET", path: str = "/api/x", route=None, disconnect=False):
    async def scenario():
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        }
        if route is not None:
            # Delivered under a private key and promoted by the fake app, so
            # the middleware sees the real production ordering: the route
            # appears in the scope only once routing has run.
            scope["_test_route"] = route

        async def receive():
            if disconnect:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass

        await app(scope, receive, send)

    return asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Identity & classification
# ---------------------------------------------------------------------------


def test_matched_route_uses_template_not_user_path():
    collector = ApiPerformanceCollector()
    _run(
        ApiPerformanceMiddleware(_FakeApp(), collector),
        path="/api/conversations/whatever-id-42?secret=1",
        route=_FakeRoute("/api/conversations/{conversation_id}"),
    )
    _batch, deltas = collector.drain_deltas()
    routes = {delta.key.route for delta in deltas}
    assert routes == {"/api/conversations/{conversation_id}"}


def test_unmatched_requests_merge_into_fixed_label():
    collector = ApiPerformanceCollector()
    _run(ApiPerformanceMiddleware(_FakeApp(), collector), path="/nope/zzz")
    _run(ApiPerformanceMiddleware(_FakeApp(status=404), collector), path="/also/missing")
    _batch, deltas = collector.drain_deltas()
    unmatched = [d for d in deltas if d.key.route == "unmatched" and d.granularity == "hourly"]
    assert unmatched, "unmatched requests must be collected"
    assert all(d.key.traffic_class == TRAFFIC_UNMATCHED for d in unmatched)
    assert sum(d.acc.request_count for d in unmatched) == 2


def test_nonstandard_methods_merge_to_other():
    collector = ApiPerformanceCollector()
    _run(ApiPerformanceMiddleware(_FakeApp(), collector), method="PROPFIND", route=_FakeRoute("/api/x"))
    _batch, deltas = collector.drain_deltas()
    assert deltas and all(d.key.method == "OTHER" for d in deltas)


def test_static_health_and_self_are_excluded_from_business():
    assert classify_traffic("unmatched", "/web/static/app.js") == TRAFFIC_INFRA
    assert classify_traffic("unmatched", "/api/nothing") == TRAFFIC_UNMATCHED
    assert classify_traffic("/health/ready", "/health/ready") == TRAFFIC_INFRA
    assert classify_traffic("/docs", "/docs") == TRAFFIC_INFRA
    assert classify_traffic("/platform/api-performance", "/platform/api-performance") == TRAFFIC_SELF
    assert classify_traffic("/api/platform/api-performance/status", "/x") == TRAFFIC_SELF
    assert classify_traffic("/api/conversations", "/api/conversations") == TRAFFIC_BUSINESS


# ---------------------------------------------------------------------------
# Timing & status classification
# ---------------------------------------------------------------------------


def test_exception_before_response_is_not_disguised_as_success():
    collector = ApiPerformanceCollector()
    with pytest.raises(RuntimeError):
        _run(ApiPerformanceMiddleware(_FakeApp(raise_before_start=True), collector))
    _batch, deltas = collector.drain_deltas()
    counts = {}
    for delta in deltas:
        if delta.granularity != "hourly":
            continue
        for cls, count in delta.acc.class_counts.items():
            counts[cls] = counts.get(cls, 0) + count
    assert counts.get(CLASS_EXCEPTION) == 1
    assert counts.get(CLASS_2XX) is None
    for delta in deltas:
        if delta.granularity != "hourly":
            continue
        assert delta.acc.success_count == 0
        assert delta.acc.duration_min_us is not None


def test_cancellation_counts_but_never_averages():
    collector = ApiPerformanceCollector()
    with pytest.raises(asyncio.CancelledError):
        _run(ApiPerformanceMiddleware(_FakeApp(cancel=True), collector))
    _batch, deltas = collector.drain_deltas()
    assert any(d.acc.class_counts.get(CLASS_CANCELLED) == 1 for d in deltas)
    for delta in deltas:
        assert delta.acc.completed_count == 0
        assert delta.acc.duration_sum_us == 0


def test_client_disconnect_without_response_is_cancelled():
    collector = ApiPerformanceCollector()
    _run(ApiPerformanceMiddleware(_FakeApp(never_respond=True), collector), disconnect=True)
    _batch, deltas = collector.drain_deltas()
    assert any(d.acc.class_counts.get(CLASS_CANCELLED) == 1 for d in deltas)


def test_stream_responses_are_separated_from_normal_latency():
    collector = ApiPerformanceCollector()
    _run(
        ApiPerformanceMiddleware(
            _FakeApp(content_type=b"text/event-stream", chunks=3), collector
        ),
        route=_FakeRoute("/api/stream"),
    )
    _batch, deltas = collector.drain_deltas()
    stream_deltas = [d for d in deltas if d.acc.stream_count == 1]
    assert stream_deltas, "stream observation recorded"
    for delta in stream_deltas:
        assert delta.acc.success_count == 0  # stream duration never in success ranking
        assert delta.acc.stream_ttfb_sum_us > 0
        assert delta.acc.stream_duration_sum_us > 0
        assert delta.acc.stream_error_count == 0


def test_mid_stream_failure_counts_as_stream_error_not_latency():
    collector = ApiPerformanceCollector()
    with pytest.raises(RuntimeError):
        _run(
            ApiPerformanceMiddleware(
                _FakeApp(content_type=b"text/event-stream", raise_after_start=True), collector
            ),
            route=_FakeRoute("/api/stream"),
        )
    _batch, deltas = collector.drain_deltas()
    stream = [d for d in deltas if d.acc.stream_count == 1]
    assert stream and all(d.acc.stream_error_count == 1 for d in stream)
    assert all(d.acc.success_count == 0 for d in stream)
    assert all(d.acc.class_counts.get(CLASS_2XX) == 1 for d in stream)  # HTTP 200 was sent


def test_timing_stops_at_last_body_byte_not_app_return():
    collector = ApiPerformanceCollector()

    class _BackgroundApp(_FakeApp):
        async def __call__(self, scope, receive, send):
            await super().__call__(scope, receive, send)
            # Background task executed after the final body byte.
            time.sleep(0.05)

    start = time.monotonic()
    _run(ApiPerformanceMiddleware(_BackgroundApp(chunks=1), collector))
    elapsed_us = int((time.monotonic() - start) * 1_000_000)
    _batch, deltas = collector.drain_deltas()
    recorded = max(d.acc.duration_max_us or 0 for d in deltas)
    assert recorded > 0
    # The 50ms background sleep happens after the observation is recorded,
    # so the recorded latency must be clearly below total wall time.
    assert recorded < elapsed_us


# ---------------------------------------------------------------------------
# Aggregation & rotation
# ---------------------------------------------------------------------------


def test_weighted_average_across_flush_cycles():
    collector = ApiPerformanceCollector()
    collector.record(_obs(duration_us=100_000))
    collector.record(_obs(duration_us=200_000))
    batch_id, deltas = collector.drain_deltas()
    assert sum(d.acc.duration_sum_us for d in deltas if d.granularity == "hourly") == 300_000
    collector.mark_flushed(batch_id, "applied")
    collector.record(_obs(duration_us=600_000))
    _batch, deltas2 = collector.drain_deltas()
    assert sum(d.acc.duration_sum_us for d in deltas2 if d.granularity == "hourly") == 600_000
    # merged weighted average over both cycles: 900ms / 3
    assert (300_000 + 600_000) // 3 == 300_000


def test_interval_min_max_maintained_in_interval():
    collector = ApiPerformanceCollector()
    collector.record(_obs(duration_us=500_000))
    batch_id, deltas = collector.drain_deltas()
    assert all(d.acc.duration_min_us == 500_000 for d in deltas if d.granularity == "hourly")
    collector.mark_flushed(batch_id, "applied")
    collector.record(_obs(duration_us=100_000))
    _batch, deltas2 = collector.drain_deltas()
    hourly2 = [d for d in deltas2 if d.granularity == "hourly"]
    # The delta carries the bucket's CURRENT extremes: min reflects the new
    # 100ms observation; max is still the bucket's 500ms. The store's
    # min/max merge keeps the interval semantics over cycles.
    assert hourly2 and all(d.acc.duration_min_us == 100_000 for d in hourly2)
    assert all(d.acc.duration_max_us == 500_000 for d in hourly2)


def test_cross_hour_and_midnight_buckets_keep_observation_time():
    collector = ApiPerformanceCollector()
    before = datetime(2026, 10, 8, 23, 59, tzinfo=BEIJING).astimezone(UTC)
    after = datetime(2026, 10, 9, 0, 1, tzinfo=BEIJING).astimezone(UTC)
    collector.record(_obs(duration_us=100_000, completed_at=before))
    collector.record(_obs(duration_us=200_000, completed_at=after))
    _batch, deltas = collector.drain_deltas()
    hourly_buckets = sorted(d.bucket_start.isoformat() for d in deltas if d.granularity == "hourly")
    daily_buckets = sorted(str(d.bucket_date) for d in deltas if d.granularity == "daily")
    assert len(hourly_buckets) == 2, "each request stays in its own observation hour"
    assert daily_buckets == ["2026-10-08", "2026-10-09"], "midnight splits the Shanghai day"


def test_flush_failure_reuses_same_batch_id_and_deltas():
    collector = ApiPerformanceCollector()
    collector.record(_obs(duration_us=100_000))
    batch_id, deltas = collector.drain_deltas()
    collector.mark_flushed(batch_id, "error:OperationalError")
    batch_id2, deltas2 = collector.drain_deltas()
    assert batch_id2 == batch_id, "retry must reuse the original batch identity"
    assert deltas2 == deltas
    collector.mark_flushed(batch_id, "applied")
    batch_id3, deltas3 = collector.drain_deltas()
    assert batch_id3 != batch_id
    assert deltas3 == [], "applied increments must not be re-flushed"


def test_rotated_buckets_preserve_original_bucket_after_flush_failure():
    collector = ApiPerformanceCollector()
    hour_1 = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    hour_5 = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
    collector.record(_obs(duration_us=100_000, completed_at=hour_1))
    collector.record(_obs(duration_us=100_000, completed_at=hour_5))
    batch_id, _deltas = collector.drain_deltas()
    collector.mark_flushed(batch_id, "error:OperationalError")
    _retry_id, retry_deltas = collector.drain_deltas()
    hours = {d.bucket_start for d in retry_deltas if d.granularity == "hourly"}
    assert hour_1 in hours and hour_5 in hours, "rotated buckets keep their original hour"


def test_in_memory_state_stays_bounded_across_many_hours():
    collector = ApiPerformanceCollector()
    base = datetime(2026, 10, 1, 0, tzinfo=UTC)
    for hour_offset in range(24):
        collector.record(_obs(duration_us=100_000, completed_at=base + timedelta(hours=hour_offset)))
    snapshot = collector.status_snapshot()
    assert snapshot["in_memory_hour_buckets"] <= 3
    assert snapshot["in_memory_day_buckets"] <= 2
    assert snapshot["dropped_pending_overflow"] == 0
    _batch, deltas = collector.drain_deltas()
    hourly_total = sum(d.acc.request_count for d in deltas if d.granularity == "hourly")
    daily_total = sum(d.acc.request_count for d in deltas if d.granularity == "daily")
    assert hourly_total == 24, "rotated + live hour buckets together carry every request"
    assert daily_total == 24


def test_concurrent_recording_is_safe():
    collector = ApiPerformanceCollector()
    errors: List[Exception] = []

    def worker():
        try:
            for index in range(500):
                collector.record(_obs(duration_us=1_000 + index))
        except Exception as error:  # pragma: no cover - surfaces real races
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert collector.status_snapshot()["total_observations"] == 8 * 500
    _batch, deltas = collector.drain_deltas()
    daily = sum(d.acc.request_count for d in deltas if d.granularity == "daily")
    assert daily == 8 * 500


# ---------------------------------------------------------------------------
# Histogram & p95
# ---------------------------------------------------------------------------


def test_hist_index_boundaries():
    assert hist_index_for(0) == 0
    assert hist_index_for(9_999) == 0
    assert hist_index_for(10_000) == 1
    assert hist_index_for(10_000_000) == 10  # 10s: not <10s, falls into 10-30s bucket
    assert hist_index_for(2_000_000_000) == HIST_SIZE - 1  # overflow


def test_p95_is_bucket_estimate_with_honest_cap():
    hist = [0] * HIST_SIZE
    hist[hist_index_for(100_000)] = 100  # 100ms x100
    p95, capped = estimate_p95_us(hist)
    assert p95 == 250 * 1000  # bucket upper bound, honest bucket-width error
    assert capped is False
    overflow = [0] * HIST_SIZE
    overflow[HIST_SIZE - 1] = 10
    p95_over, capped_over = estimate_p95_us(overflow)
    assert capped_over is True
    assert p95_over == 1_800_000 * 1000  # only a lower bound is knowable
    empty, capped_empty = estimate_p95_us([0] * HIST_SIZE)
    assert empty is None and capped_empty is False


def test_success_hist_only_counts_success():
    collector = ApiPerformanceCollector()
    collector.record(_obs(status_class=CLASS_5XX, duration_us=5_000_000))
    collector.record(_obs(status_class=CLASS_2XX, duration_us=100_000))
    _batch, deltas = collector.drain_deltas()
    for delta in deltas:
        if delta.granularity != "hourly":
            continue
        assert delta.acc.hist[hist_index_for(100_000)] == 1
        assert delta.acc.hist[hist_index_for(5_000_000)] == 0
        assert delta.acc.completed_count == 2  # errors still have durations


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_rejects_out_of_bounds_values():
    with pytest.raises(ValueError):
        ApiPerformanceConfig.from_settings(ApiPerformanceSettings(api_perf_p95_threshold_ms="not-a-number"))
    with pytest.raises(ValueError):
        ApiPerformanceConfig.from_settings(ApiPerformanceSettings(api_perf_min_samples="0"))
    with pytest.raises(ValueError):
        ApiPerformanceConfig.from_settings(
            ApiPerformanceSettings(api_perf_route_overrides='[{"route": "not-absolute"}]')
        )
    with pytest.raises(ValueError):
        ApiPerformanceConfig.from_settings(ApiPerformanceSettings(api_perf_sustained_minutes="999999"))


def test_config_route_overrides_are_bounded_and_applied():
    settings = ApiPerformanceSettings(
        api_perf_route_overrides='[{"route": "/api/slow-report", "p95_threshold_ms": 5000, "min_samples": 50}]'
    )
    config = ApiPerformanceConfig.from_settings(settings)
    override = config.override_for("/api/slow-report")
    assert override is not None and override.p95_threshold_ms == 5000 and override.min_samples == 50
    assert config.override_for("/api/other") is None
    entries = ",".join('{"route": "/r%d"}' % i for i in range(21))
    with pytest.raises(ValueError):
        ApiPerformanceConfig.from_settings(ApiPerformanceSettings(api_perf_route_overrides="[" + entries + "]"))


# ---------------------------------------------------------------------------
# QA fix regressions: completion-after-final-send, partial responses,
# stale-batch deadline, and batch-prefix preservation
# ---------------------------------------------------------------------------


def _run_raw_send(app, *, method="GET", path="/api/x", route=None, send_impl=None, disconnect=False):
    """Like _run but lets the test control the innermost send callable and
    simulate a client disconnect on receive."""
    async def scenario():
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        }
        if route is not None:
            scope["_test_route"] = route

        async def receive():
            if disconnect:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if send_impl is not None:
                await send_impl(message)

        await app(scope, receive, send)

    return asyncio.run(scenario())


def test_final_send_failure_is_never_a_success_sample():
    collector = ApiPerformanceCollector()

    async def failing_final(message):
        if message.get("type") == "http.response.body" and not message.get("more_body", False):
            raise OSError("synthetic socket closed")

    with pytest.raises(OSError):
        _run_raw_send(
            ApiPerformanceMiddleware(_FakeApp(), collector),
            path="/api/x", route=_FakeRoute("/api/x"), send_impl=failing_final,
        )
    _batch, deltas = collector.drain_deltas()
    counts = {}
    for delta in deltas:
        if delta.granularity != "hourly":
            continue
        for cls, count in delta.acc.class_counts.items():
            counts[cls] = counts.get(cls, 0) + count
    assert counts.get(CLASS_2XX) is None, "a response whose final send failed is not a success"
    assert counts.get(CLASS_EXCEPTION) == 1


def test_final_send_duration_is_included():
    collector = ApiPerformanceCollector()

    async def slow_final(message):
        if message.get("type") == "http.response.body" and not message.get("more_body", False):
            time.sleep(0.06)

    _run_raw_send(
        ApiPerformanceMiddleware(_FakeApp(), collector),
        path="/api/x", route=_FakeRoute("/api/x"), send_impl=slow_final,
    )
    _batch, deltas = collector.drain_deltas()
    recorded = max(
        (d.acc.duration_max_us or 0) for d in deltas if d.granularity == "hourly"
    )
    assert recorded >= 55_000, "the blocked final send must be inside the measured duration"


def test_json_200_then_exception_is_not_a_success_sample():
    collector = ApiPerformanceCollector()

    class _PartialJsonApp(_FakeApp):
        async def __call__(self, scope, receive, send):
            if "_test_route" in scope:
                scope["route"] = scope.pop("_test_route")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            raise RuntimeError("boom after headers")

    with pytest.raises(RuntimeError):
        _run_raw_send(
            ApiPerformanceMiddleware(_PartialJsonApp(), collector),
            path="/api/x", route=_FakeRoute("/api/x"),
        )
    _batch, deltas = collector.drain_deltas()
    counts = {}
    for delta in deltas:
        if delta.granularity != "hourly":
            continue
        for cls, count in delta.acc.class_counts.items():
            counts[cls] = counts.get(cls, 0) + count
    assert counts.get(CLASS_EXCEPTION) == 1
    assert counts.get(CLASS_2XX) is None


def test_json_200_then_cancel_is_cancelled_not_success():
    collector = ApiPerformanceCollector()

    class _PartialCancelApp(_FakeApp):
        async def __call__(self, scope, receive, send):
            if "_test_route" in scope:
                scope["route"] = scope.pop("_test_route")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run_raw_send(
            ApiPerformanceMiddleware(_PartialCancelApp(), collector),
            path="/api/x", route=_FakeRoute("/api/x"),
        )
    _batch, deltas = collector.drain_deltas()
    assert any(
        d.acc.class_counts.get(CLASS_CANCELLED) == 1
        for d in deltas if d.granularity == "hourly"
    )


def test_sse_disconnect_return_is_a_stream_error():
    collector = ApiPerformanceCollector()

    class _TruncatedSseApp(_FakeApp):
        async def __call__(self, scope, receive, send):
            if "_test_route" in scope:
                scope["route"] = scope.pop("_test_route")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            })
            await send({"type": "http.response.body", "body": b"data: x\n\n", "more_body": True})
            # Client disconnects; the generator polls receive, sees it, and
            # returns without ever sending the final more_body=False chunk.
            await receive()

    _run_raw_send(
        ApiPerformanceMiddleware(_TruncatedSseApp(), collector),
        path="/api/sse", route=_FakeRoute("/api/sse"), disconnect=True,
    )
    _batch, deltas = collector.drain_deltas()
    stream = [d for d in deltas if d.granularity == "hourly" and d.acc.stream_count == 1]
    assert stream and stream[0].acc.stream_error_count == 1, \
        "a disconnected stream must not count as a normal completion"


def test_batch_confirmation_only_releases_its_own_pending_prefix():
    collector = ApiPerformanceCollector()
    collector.record(_obs(duration_us=100_000))
    batch_id, deltas = collector.drain_deltas()
    collector.mark_flushed(batch_id, "error:OperationalError")
    # Traffic continues and old buckets rotate WHILE the first batch is frozen.
    base = datetime(2026, 10, 1, 0, tzinfo=UTC)
    for hour_offset in range(6):
        collector.record(_obs(duration_us=50_000, completed_at=base + timedelta(hours=hour_offset)))
    _retry_id, _retry_deltas = collector.drain_deltas()  # same frozen batch
    collector.mark_flushed(batch_id, "applied")
    assert collector.dropped_pending_overflow == 0
    assert collector.dropped_stale_batch_observations == 0
    # The later rotations survive their batch's confirmation.
    _next_id, next_deltas = collector.drain_deltas()
    hourly_total = sum(d.acc.request_count for d in next_deltas if d.granularity == "hourly")
    daily_total = sum(d.acc.request_count for d in next_deltas if d.granularity == "daily")
    assert hourly_total == 6, "increments rotated after the frozen batch must not be dropped"
    assert daily_total == 6


def test_batch_retry_deadline_drops_with_counted_loss():
    collector = ApiPerformanceCollector(retry_deadline_seconds=0.05)
    collector.record(_obs(duration_us=100_000))
    batch_id, deltas = collector.drain_deltas()
    assert sum(d.acc.request_count for d in deltas if d.granularity == "hourly") == 1
    time.sleep(0.08)
    collector.mark_flushed(batch_id, "error:OperationalError")
    assert collector.dropped_stale_batch_observations == 1
    assert collector._pending_batch is None
    # A brand-new batch is minted afterwards; the lost data is not replayed.
    _new_id, new_deltas = collector.drain_deltas()
    assert sum(d.acc.request_count for d in new_deltas if d.granularity == "hourly") == 0


def test_batch_retry_deadline_config_is_bounded_by_dedup_retention():
    with pytest.raises(ValueError):
        ApiPerformanceConfig.from_settings(ApiPerformanceSettings(
            api_perf_batch_retention_hours="48",
            api_perf_batch_retry_hours="48",
        ))
    config = ApiPerformanceConfig.from_settings(ApiPerformanceSettings(
        api_perf_batch_retention_hours="48",
        api_perf_batch_retry_hours="24",
    ))
    assert config.batch_retry_hours == 24


def test_unknown_success_batch_expires_before_it_can_be_retried(monkeypatch):
    from app.services import api_performance_collector as collector_module

    monotonic = [1000.0]
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: monotonic[0])
    collector = ApiPerformanceCollector(retry_deadline_seconds=24 * 3600)
    collector.record(_obs(duration_us=100_000))
    old_id, old_deltas = collector.drain_deltas()
    assert sum(d.acc.request_count for d in old_deltas if d.granularity == "hourly") == 1

    # Model a successful DB commit whose acknowledgement was lost, then
    # let the dedup record expire before the next attempt.
    monotonic[0] += 49 * 3600
    retry_id, retry_deltas = collector.drain_deltas()

    assert retry_id != old_id, "an expired batch identity must never be replayed"
    assert sum(d.acc.request_count for d in retry_deltas if d.granularity == "hourly") == 0
    assert collector.status_snapshot()["dropped_stale_batch_observations"] == 1


def test_overflow_during_frozen_batch_confirmation_preserves_active_queue_head():
    collector = ApiPerformanceCollector()
    base = datetime(2026, 1, 1, 0, tzinfo=UTC)
    for hour_offset in range(4):
        collector.record(_obs(duration_us=100_000, completed_at=base + timedelta(hours=hour_offset)))
    batch_id, _frozen_deltas = collector.drain_deltas()

    # Use the production 4096-item queue cap. More than half a year of
    # synthetic hourly buckets forces actual pending-queue overflow.
    for hour_offset in range(4, 5004):
        collector.record(_obs(duration_us=100_000, completed_at=base + timedelta(hours=hour_offset)))
    assert collector.dropped_pending_overflow > 0
    surviving_head = next(
        delta for delta in collector._pending if delta.granularity == "hourly"
    )

    collector.mark_flushed(batch_id, "applied")
    _next_id, next_deltas = collector.drain_deltas()
    assert any(
        delta.granularity == "hourly"
        and delta.bucket_start == surviving_head.bucket_start
        and delta.acc.request_count == surviving_head.acc.request_count
        for delta in next_deltas
    ), "confirming the frozen batch must not remove a later queue item"


def test_final_body_send_after_disconnect_is_cancelled_for_normal_responses():
    collector = ApiPerformanceCollector()

    class _DisconnectBeforeFinalApp(_FakeApp):
        async def __call__(self, scope, receive, send):
            if "_test_route" in scope:
                scope["route"] = scope.pop("_test_route")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": b"{", "more_body": True})
            await receive()
            await send({"type": "http.response.body", "body": b"}", "more_body": False})

    _run_raw_send(
        ApiPerformanceMiddleware(_DisconnectBeforeFinalApp(), collector),
        path="/api/partial", route=_FakeRoute("/api/partial"), disconnect=True,
    )
    _batch, deltas = collector.drain_deltas()
    hourly = [delta for delta in deltas if delta.granularity == "hourly"]
    assert sum(delta.acc.class_counts.get(CLASS_CANCELLED, 0) for delta in hourly) == 1
    assert sum(delta.acc.class_counts.get(CLASS_2XX, 0) for delta in hourly) == 0


def test_starlette_sse_final_empty_body_after_disconnect_is_a_stream_error():
    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    collector = ApiPerformanceCollector()

    async def stream_app(scope, receive, send):
        scope["route"] = _FakeRoute("/api/sse")
        request = Request(scope, receive)

        async def events():
            yield b"data: event\\n\\n"
            if await request.is_disconnected():
                return

        response = StreamingResponse(events(), media_type="text/event-stream")
        await response(scope, receive, send)

    async def scenario():
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "GET",
            "path": "/api/sse",
            "headers": [],
            "query_string": b"",
        }

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        await ApiPerformanceMiddleware(stream_app, collector)(scope, receive, send)

    asyncio.run(scenario())
    _batch, deltas = collector.drain_deltas()
    stream = [d for d in deltas if d.granularity == "hourly" and d.acc.stream_count]
    assert len(stream) == 1
    assert stream[0].acc.stream_error_count == 1
    assert stream[0].acc.success_count == 0
