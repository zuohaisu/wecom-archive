"""GH-186 super-admin API performance telemetry: in-process collector.

Pure-ASGI entry collection with a bounded, per-app-instance in-memory
aggregator. See docs/adr/0007-api-performance-telemetry.md for the decision
record (why not prometheus_client, single-process scope, loss tolerance).

Hard boundaries (AGENTS.md + GH-186):
- The request path only ever touches bounded in-memory state -- never the DB.
- Labels are route templates from the route table plus the fixed literals
  "unmatched"/"OTHER" -- never user-controlled input, never truncated user
  paths, never query strings, headers, bodies or identifiers.
- Each app instance owns its own collector (no module-level registry), so
  tests and app factories stay independent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from app.settings import ApiPerformanceSettings

logger = logging.getLogger(__name__)

# Bumped when aggregation semantics change in a way that makes buckets
# written before/after the change incommensurable (bucket layout, meaning
# of a counter). Rows carry hist_version/stats_version so queries never
# merge across versions silently (see api_performance_store).
HIST_VERSION = 1
STATS_VERSION = 1

# 15 finite upper bounds in milliseconds -> 16 finite buckets plus the
# overflow bucket (index 16). Buckets are over SUCCESS durations only.
HIST_BOUNDS_MS: Tuple[int, ...] = (
    10, 25, 50, 100, 250, 500, 1000, 2000, 5000,
    10000, 30000, 60000, 180000, 600000, 1800000,
)
HIST_SIZE = len(HIST_BOUNDS_MS) + 1

# Bounded status classification -- never raw status codes as labels.
CLASS_1XX = "1xx"
CLASS_2XX = "2xx"
CLASS_3XX = "3xx"
CLASS_4XX = "4xx"
CLASS_5XX = "5xx"
CLASS_EXCEPTION = "exception"    # unhandled server-side exception, no completed response
CLASS_CANCELLED = "cancelled"    # client disconnect / task cancel before completion
STATUS_CLASSES = (
    CLASS_1XX, CLASS_2XX, CLASS_3XX, CLASS_4XX, CLASS_5XX,
    CLASS_EXCEPTION, CLASS_CANCELLED,
)
ERROR_CLASSES = frozenset({CLASS_4XX, CLASS_5XX, CLASS_EXCEPTION})

TRAFFIC_BUSINESS = "business"
TRAFFIC_INFRA = "infra"          # health probes, docs, static files
TRAFFIC_SELF = "self"            # this module's own page/API polling
TRAFFIC_UNMATCHED = "unmatched"  # no route matched (404 / redirect-slash)

UNMATCHED_ROUTE = "unmatched"
OTHER_METHOD = "OTHER"
_STANDARD_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

_INFRA_ROUTES = frozenset({
    "/health", "/health/live", "/health/ready",
    "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect",
})
_STATIC_PREFIX = "/web/static"
_SELF_ROUTES = frozenset({
    "/platform/api-performance",
    "/api/platform/api-performance/endpoints",
    "/api/platform/api-performance/series",
    "/api/platform/api-performance/status",
    "/api/platform/api-performance/anomalies",
})

_BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")

# Per-endpoint retention of in-memory buckets. A flush every 15 minutes
# drains these well inside the caps; the caps only bound pathological
# clock/loop stalls so memory stays bounded (GH-186 bounded-state rule).
_HOUR_BUCKETS_KEPT = 3
_DAY_BUCKETS_KEPT = 2
_PENDING_CAP = 4096
_MINUTE_WINDOW_MINUTES = 15
_STREAM_CONTENT_TYPE = "text/event-stream"
_ROUTE_LABEL_MAX = 500

GRANULARITY_HOURLY = "hourly"
GRANULARITY_DAILY = "daily"


def classify_traffic(route_template: str, raw_path: str) -> str:
    """Bounded, eq/prefix-only classification. raw_path is never stored."""
    if route_template == UNMATCHED_ROUTE:
        if raw_path.startswith(_STATIC_PREFIX):
            return TRAFFIC_INFRA
        return TRAFFIC_UNMATCHED
    if route_template in _SELF_ROUTES:
        return TRAFFIC_SELF
    if route_template in _INFRA_ROUTES:
        return TRAFFIC_INFRA
    return TRAFFIC_BUSINESS


def normalize_method(method: str) -> str:
    method = (method or "").upper()
    return method if method in _STANDARD_METHODS else OTHER_METHOD


def hour_bucket_start(at: datetime) -> datetime:
    """UTC instant of the containing Asia/Shanghai natural hour."""
    local = at.astimezone(_BEIJING)
    return local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def day_bucket_date(at: datetime) -> date:
    return at.astimezone(_BEIJING).date()


def daily_bucket_start(bucket_day: date) -> datetime:
    """UTC instant of Asia/Shanghai midnight for a bucket day."""
    return datetime.combine(bucket_day, datetime.min.time(), tzinfo=_BEIJING)


def hist_index_for(duration_us: int) -> int:
    ms = duration_us / 1000.0
    for index, bound in enumerate(HIST_BOUNDS_MS):
        if ms < bound:
            return index
    return HIST_SIZE - 1


def estimate_p95_us(hist: List[int]) -> Tuple[Optional[int], bool]:
    """Histogram p95 estimate.

    Returns (value_us, capped). The value is the upper bound of the first
    bucket whose cumulative count reaches 95% of the total -- an in-bucket
    estimate with bucket-width error, never invented precision. ``capped``
    means the estimate fell into the overflow bucket: only a lower bound
    is known.
    """
    total = sum(hist)
    if total <= 0:
        return None, False
    threshold = -(-95 * total // 100)  # ceil(0.95 * total)
    cumulative = 0
    for index, count in enumerate(hist):
        cumulative += count
        if cumulative >= threshold:
            if index >= HIST_SIZE - 1:
                return HIST_BOUNDS_MS[-1] * 1000, True
            return HIST_BOUNDS_MS[index] * 1000, False
    return HIST_BOUNDS_MS[-1] * 1000, True


@dataclass(frozen=True)
class RouteOverride:
    route: str
    p95_threshold_ms: Optional[int] = None
    min_samples: Optional[int] = None


@dataclass(frozen=True)
class ApiPerformanceConfig:
    """Validated runtime configuration. Construction fails loudly on bad
    values (GH-186: threshold config validation) -- a misconfigured deploy
    must not silently disable or distort telemetry."""

    enabled: bool = True
    flush_interval_seconds: int = 900
    retention_hours: int = 720
    retention_days: int = 180
    batch_retention_hours: int = 48
    detection_enabled: bool = True
    detection_interval_seconds: int = 60
    window_minutes: int = 5
    min_samples: int = 20
    p95_threshold_ms: int = 1000
    sustained_minutes: int = 5
    stream_p95_threshold_ms: int = 3000
    stream_min_samples: int = 20
    route_overrides: Tuple[RouteOverride, ...] = ()
    alert_email: str = ""
    admin_url: str = ""

    @classmethod
    def from_settings(cls, settings: Optional[ApiPerformanceSettings] = None) -> "ApiPerformanceConfig":
        s = settings or ApiPerformanceSettings()
        config = cls(
            enabled=_parse_bool(s.api_perf_enabled, "API_PERF_ENABLED", default=True),
            flush_interval_seconds=_parse_int(s.api_perf_flush_interval_seconds, "API_PERF_FLUSH_INTERVAL_SECONDS", 60, 3600, 900),
            retention_hours=_parse_int(s.api_perf_retention_hours, "API_PERF_RETENTION_HOURS", 24, 8760, 720),
            retention_days=_parse_int(s.api_perf_retention_days, "API_PERF_RETENTION_DAYS", 7, 3650, 180),
            batch_retention_hours=_parse_int(s.api_perf_batch_retention_hours, "API_PERF_BATCH_RETENTION_HOURS", 2, 336, 48),
            detection_enabled=_parse_bool(s.api_perf_detection_enabled, "API_PERF_DETECTION_ENABLED", default=True),
            detection_interval_seconds=_parse_int(s.api_perf_detection_interval_seconds, "API_PERF_DETECTION_INTERVAL_SECONDS", 15, 3600, 60),
            window_minutes=_parse_int(s.api_perf_window_minutes, "API_PERF_WINDOW_MINUTES", 1, 15, 5),
            min_samples=_parse_int(s.api_perf_min_samples, "API_PERF_MIN_SAMPLES", 1, 100000, 20),
            p95_threshold_ms=_parse_int(s.api_perf_p95_threshold_ms, "API_PERF_P95_THRESHOLD_MS", 1, 3600000, 1000),
            sustained_minutes=_parse_int(s.api_perf_sustained_minutes, "API_PERF_SUSTAINED_MINUTES", 1, 1440, 5),
            stream_p95_threshold_ms=_parse_int(s.api_perf_stream_p95_threshold_ms, "API_PERF_STREAM_P95_THRESHOLD_MS", 1, 3600000, 3000),
            stream_min_samples=_parse_int(s.api_perf_stream_min_samples, "API_PERF_STREAM_MIN_SAMPLES", 1, 100000, 20),
            route_overrides=_parse_overrides(s.api_perf_route_overrides),
            alert_email=s.api_perf_alert_email.strip(),
            admin_url=s.api_perf_admin_url.strip(),
        )
        if config.sustained_minutes > config.window_minutes * 60:
            raise ValueError("API_PERF_SUSTAINED_MINUTES cannot exceed the window span in minutes")
        return config

    def override_for(self, route_template: str) -> Optional[RouteOverride]:
        for override in self.route_overrides:
            if override.route == route_template:
                return override
        return None


def _parse_bool(raw: str, name: str, *, default: bool) -> bool:
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean, got {value!r}")


def _parse_int(raw: str, name: str, low: int, high: int, default: int) -> int:
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {value!r}") from error
    if not low <= parsed <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {parsed}")
    return parsed


def _parse_overrides(raw: str) -> Tuple[RouteOverride, ...]:
    value = (raw or "").strip()
    if not value:
        return ()
    try:
        entries = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("API_PERF_ROUTE_OVERRIDES must be a JSON array") from error
    if not isinstance(entries, list):
        raise ValueError("API_PERF_ROUTE_OVERRIDES must be a JSON array")
    if len(entries) > 20:
        raise ValueError("API_PERF_ROUTE_OVERRIDES supports at most 20 entries")
    overrides = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("API_PERF_ROUTE_OVERRIDES entries must be objects")
        route = str(entry.get("route", "")).strip()
        if not route.startswith("/") or len(route) > _ROUTE_LABEL_MAX:
            raise ValueError("API_PERF_ROUTE_OVERRIDES route must be an absolute route template")
        p95 = entry.get("p95_threshold_ms")
        samples = entry.get("min_samples")
        overrides.append(RouteOverride(
            route=route,
            p95_threshold_ms=None if p95 is None else _parse_int(str(p95), "override p95_threshold_ms", 1, 3600000, 1000),
            min_samples=None if samples is None else _parse_int(str(samples), "override min_samples", 1, 100000, 20),
        ))
    return tuple(overrides)


@dataclass
class Accumulator:
    """One (endpoint, bucket) aggregate. Bounded fields only; no samples."""

    request_count: int = 0
    class_counts: Dict[str, int] = field(default_factory=dict)
    completed_count: int = 0
    duration_sum_us: int = 0
    duration_min_us: Optional[int] = None
    duration_max_us: Optional[int] = None
    success_count: int = 0
    success_duration_sum_us: int = 0
    success_min_us: Optional[int] = None
    success_max_us: Optional[int] = None
    hist: List[int] = field(default_factory=lambda: [0] * HIST_SIZE)
    stream_count: int = 0
    stream_error_count: int = 0
    stream_duration_sum_us: int = 0
    stream_min_us: Optional[int] = None
    stream_max_us: Optional[int] = None
    stream_ttfb_sum_us: int = 0
    stream_ttfb_min_us: Optional[int] = None
    stream_ttfb_max_us: Optional[int] = None

    def observe(
        self,
        *,
        status_class: str,
        duration_us: Optional[int],
        is_stream: bool,
        stream_error: bool,
        stream_ttfb_us: Optional[int],
    ) -> None:
        self.request_count += 1
        self.class_counts[status_class] = self.class_counts.get(status_class, 0) + 1
        if is_stream:
            self.stream_count += 1
            if stream_error:
                self.stream_error_count += 1
            if duration_us is not None:
                self.stream_duration_sum_us += duration_us
                self.stream_min_us = duration_us if self.stream_min_us is None else min(self.stream_min_us, duration_us)
                self.stream_max_us = duration_us if self.stream_max_us is None else max(self.stream_max_us, duration_us)
            if stream_ttfb_us is not None:
                self.stream_ttfb_sum_us += stream_ttfb_us
                self.stream_ttfb_min_us = stream_ttfb_us if self.stream_ttfb_min_us is None else min(self.stream_ttfb_min_us, stream_ttfb_us)
                self.stream_ttfb_max_us = stream_ttfb_us if self.stream_ttfb_max_us is None else max(self.stream_ttfb_max_us, stream_ttfb_us)
            return
        if duration_us is None:
            # Cancelled before any response: counted, never averaged in.
            return
        self.completed_count += 1
        self.duration_sum_us += duration_us
        self.duration_min_us = duration_us if self.duration_min_us is None else min(self.duration_min_us, duration_us)
        self.duration_max_us = duration_us if self.duration_max_us is None else max(self.duration_max_us, duration_us)
        if status_class == CLASS_2XX:
            self.success_count += 1
            self.success_duration_sum_us += duration_us
            self.success_min_us = duration_us if self.success_min_us is None else min(self.success_min_us, duration_us)
            self.success_max_us = duration_us if self.success_max_us is None else max(self.success_max_us, duration_us)
            self.hist[hist_index_for(duration_us)] += 1

    def snapshot(self) -> "Accumulator":
        clone = Accumulator()
        return _accumulate_into(clone, self)


def _accumulate_into(target: Accumulator, source: Accumulator) -> Accumulator:
    target.request_count = source.request_count
    target.class_counts = dict(source.class_counts)
    target.completed_count = source.completed_count
    target.duration_sum_us = source.duration_sum_us
    target.duration_min_us = source.duration_min_us
    target.duration_max_us = source.duration_max_us
    target.success_count = source.success_count
    target.success_duration_sum_us = source.success_duration_sum_us
    target.success_min_us = source.success_min_us
    target.success_max_us = source.success_max_us
    target.hist = list(source.hist)
    target.stream_count = source.stream_count
    target.stream_error_count = source.stream_error_count
    target.stream_duration_sum_us = source.stream_duration_sum_us
    target.stream_min_us = source.stream_min_us
    target.stream_max_us = source.stream_max_us
    target.stream_ttfb_sum_us = source.stream_ttfb_sum_us
    target.stream_ttfb_min_us = source.stream_ttfb_min_us
    target.stream_ttfb_max_us = source.stream_ttfb_max_us
    return target


@dataclass(frozen=True)
class EndpointKey:
    method: str
    route: str
    traffic_class: str


@dataclass
class Observation:
    """A completed (or failed/cancelled) request, ready to aggregate."""

    method: str
    route: str
    traffic_class: str
    status_class: str
    duration_us: Optional[int]
    is_stream: bool
    stream_error: bool
    stream_ttfb_us: Optional[int]
    completed_at: datetime


@dataclass
class BucketDelta:
    """Increment for one (endpoint, bucket) since the last flush snapshot."""

    key: EndpointKey
    granularity: str  # "hourly" | "daily"
    bucket_start: datetime  # UTC instant of bucket start (hour / Shanghai midnight)
    bucket_date: Optional[date]
    acc: Accumulator


@dataclass
class WindowSample:
    """Merged recent-minute observations for one endpoint (detector input)."""

    key: EndpointKey
    success_count: int
    success_hist: List[int]
    stream_count: int
    stream_hist: List[int]
    minutes_covered: int


class ApiPerformanceCollector:
    """Per-app-instance telemetry state. All mutation is under one lock;
    locked sections are bounded dict/list arithmetic so the event loop is
    never blocked meaningfully."""

    def __init__(self, *, instance_id: Optional[str] = None, now: Optional[datetime] = None) -> None:
        self.instance_id = instance_id or uuid.uuid4().hex
        self.started_at = now or datetime.now(timezone.utc)
        self._lock = threading.RLock()
        self._routes: "OrderedDict[str, Set[str]]" = OrderedDict()
        # (method, route, traffic_class) -> OrderedDict[bucket_key -> [live, snapshot]]
        self._hours: Dict[EndpointKey, "OrderedDict[datetime, List[Accumulator]]"] = {}
        self._days: Dict[EndpointKey, "OrderedDict[date, List[Accumulator]]"] = {}
        self._pending: List[BucketDelta] = []
        self._windows: Dict[Tuple[str, str], "OrderedDict[int, List[MinuteWindow]]"] = {}
        self._pending_batch: Optional[Tuple[str, List[BucketDelta]]] = None
        # Honest diagnostics: anything dropped is counted, never hidden.
        self.dropped_late_observations = 0
        self.dropped_pending_overflow = 0
        self.total_observations = 0
        self.flush_seq = 0
        self.last_flush_at: Optional[datetime] = None
        self.last_flush_result: str = "never"
        self.consecutive_flush_errors = 0
        self.last_persist_error: str = ""

    # ------------------------------------------------------------------
    # Route registry (assembly-time)
    # ------------------------------------------------------------------

    def register_route(self, path_format: str, methods: Optional[Set[str]]) -> None:
        if not path_format or len(path_format) > _ROUTE_LABEL_MAX:
            return
        with self._lock:
            self._routes[path_format] = {m for m in (methods or ()) if m} or {"-"}

    def register_app_routes(self, routes: List[Any]) -> None:
        for route in routes:
            path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
            if path_format is None or not hasattr(route, "methods"):
                continue  # Mounts carry no method set and no template identity.
            self.register_route(path_format, getattr(route, "methods", None))

    def known_routes(self) -> List[Tuple[str, Tuple[str, ...], str]]:
        with self._lock:
            return [
                (template, tuple(sorted(methods)), classify_traffic(template, template))
                for template, methods in self._routes.items()
            ]

    # ------------------------------------------------------------------
    # Request-path recording (the only hot path)
    # ------------------------------------------------------------------

    def record(self, observation: Observation) -> None:
        hour_key = hour_bucket_start(observation.completed_at)
        day_key = day_bucket_date(observation.completed_at)
        minute_key = int(observation.completed_at.timestamp()) // 60
        with self._lock:
            self.total_observations += 1
            if observation.traffic_class == TRAFFIC_BUSINESS:
                self._record_window(observation, minute_key)
            key = EndpointKey(observation.method, observation.route, observation.traffic_class)
            self._record_bucket(self._hours, key, hour_key, None, observation, _HOUR_BUCKETS_KEPT, GRANULARITY_HOURLY)
            self._record_bucket(self._days, key, day_key, day_key, observation, _DAY_BUCKETS_KEPT, GRANULARITY_DAILY)

    def _record_bucket(
        self,
        table: Dict[EndpointKey, "OrderedDict[Any, List[Accumulator]]"],
        key: EndpointKey,
        bucket_key: Any,
        bucket_date: Optional[date],
        observation: Observation,
        keep: int,
        granularity: str,
    ) -> None:
        buckets = table.get(key)
        if buckets is None:
            buckets = OrderedDict()
            table[key] = buckets
        entry = buckets.get(bucket_key)
        if entry is None:
            if len(buckets) >= keep:
                evicted_key, (evicted_acc, evicted_snapshot) = buckets.popitem(last=False)
                # The evicted bucket's snapshot dies with the entry; the
                # pending delta carries everything not yet persisted. An
                # already-fully-flushed bucket is simply released.
                evicted_delta = _delta_of(evicted_acc, evicted_snapshot)
                if evicted_delta is not None:
                    self._pending.append(BucketDelta(
                        key=key,
                        granularity=granularity,
                        bucket_start=evicted_key if granularity == GRANULARITY_HOURLY else daily_bucket_start(evicted_key),
                        bucket_date=evicted_key if granularity == GRANULARITY_DAILY else None,
                        acc=evicted_delta,
                    ))
                if len(self._pending) > _PENDING_CAP:
                    self._pending.pop(0)
                    self.dropped_pending_overflow += 1
            entry = [Accumulator(), Accumulator()]
            buckets[bucket_key] = entry
        entry[0].observe(
            status_class=observation.status_class,
            duration_us=observation.duration_us,
            is_stream=observation.is_stream,
            stream_error=observation.stream_error,
            stream_ttfb_us=observation.stream_ttfb_us,
        )

    def _record_window(self, observation: Observation, minute_key: int) -> None:
        if observation.status_class != CLASS_2XX or observation.duration_us is None:
            return
        key = (observation.method, observation.route)
        windows = self._windows.get(key)
        if windows is None:
            windows = OrderedDict()
            self._windows[key] = windows
        entry = windows.get(minute_key)
        if entry is None:
            if len(windows) >= _MINUTE_WINDOW_MINUTES:
                windows.popitem(last=False)
            entry = [MinuteWindow(), MinuteWindow()]
            windows[minute_key] = entry
        window = entry[1] if observation.is_stream else entry[0]
        window.count += 1
        window.hist[hist_index_for(observation.duration_us)] += 1

    # ------------------------------------------------------------------
    # Flush support
    # ------------------------------------------------------------------

    def drain_deltas(self) -> Tuple[str, List[BucketDelta]]:
        """Compute this flush cycle's increments and rotate the snapshots.

        Returns (batch_id, deltas). The batch id stays stable until the
        caller confirms the flush outcome -- a retry after an unknown
        commit result reuses the same id and the same deltas, so DB-side
        dedup makes double-apply impossible. Call ``mark_flushed`` with
        the outcome; only a committed result advances the snapshots.
        """
        with self._lock:
            if self._pending_batch is not None:
                return self._pending_batch
            deltas: List[BucketDelta] = []
            for key, buckets in self._hours.items():
                for bucket_key, (acc, snapshot) in buckets.items():
                    delta = _delta_of(acc, snapshot)
                    if delta is not None:
                        deltas.append(BucketDelta(
                            key=key, granularity=GRANULARITY_HOURLY,
                            bucket_start=bucket_key, bucket_date=None, acc=delta,
                        ))
            for key, buckets in self._days.items():
                for bucket_day, (acc, snapshot) in buckets.items():
                    delta = _delta_of(acc, snapshot)
                    if delta is not None:
                        deltas.append(BucketDelta(
                            key=key, granularity=GRANULARITY_DAILY,
                            bucket_start=daily_bucket_start(bucket_day), bucket_date=bucket_day, acc=delta,
                        ))
            deltas.extend(self._pending)
            self.flush_seq += 1
            batch_id = f"{self.instance_id}:{self.flush_seq}"
            self._pending_batch = (batch_id, deltas)
            return batch_id, deltas

    def mark_flushed(self, batch_id: str, result: str, *, at: Optional[datetime] = None) -> None:
        """Record a flush outcome. ``applied``/``already_applied`` are both
        terminal (the increments are in the DB exactly once): snapshots
        advance and rotated buckets are released. Anything else KEEPS the
        pending batch so the next drain reuses the same batch id and the
        same deltas -- a retry after an unknown commit result must never
        mint a new identity, or DB-side dedup could not stop a double
        apply. New observations observed meanwhile simply land in the
        following batch."""
        at = at or datetime.now(timezone.utc)
        with self._lock:
            if self._pending_batch is None or self._pending_batch[0] != batch_id:
                return
            _, deltas = self._pending_batch
            self.last_flush_at = at
            self.last_flush_result = result
            if result in ("applied", "already_applied"):
                self.consecutive_flush_errors = 0
                self.last_persist_error = ""
                self._advance_snapshots(deltas)
                self._pending.clear()
                self._pending_batch = None
            else:
                self.consecutive_flush_errors += 1
                self.last_persist_error = result

    def _advance_snapshots(self, deltas: List[BucketDelta]) -> None:
        by_key = {
            (d.key, d.granularity, d.bucket_start): d.acc
            for d in deltas
        }
        for table, granularity in ((self._hours, GRANULARITY_HOURLY), (self._days, GRANULARITY_DAILY)):
            for key, buckets in table.items():
                for bucket_key, (acc, snapshot) in buckets.items():
                    bucket_start = bucket_key if granularity == GRANULARITY_HOURLY else daily_bucket_start(bucket_key)
                    delta = by_key.get((key, granularity, bucket_start))
                    if delta is not None:
                        _advance_snapshot(snapshot, delta)

    # ------------------------------------------------------------------
    # Read side (status page, detector)
    # ------------------------------------------------------------------

    def status_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "instance_id": self.instance_id,
                "started_at": self.started_at,
                "total_observations": self.total_observations,
                "in_memory_hour_buckets": sum(len(b) for b in self._hours.values()),
                "in_memory_day_buckets": sum(len(b) for b in self._days.values()),
                "pending_rotated_buckets": len(self._pending),
                "dropped_late_observations": self.dropped_late_observations,
                "dropped_pending_overflow": self.dropped_pending_overflow,
                "flush_seq": self.flush_seq,
                "last_flush_at": self.last_flush_at,
                "last_flush_result": self.last_flush_result,
                "consecutive_flush_errors": self.consecutive_flush_errors,
                "last_persist_error": self.last_persist_error,
            }

    def window_observations(self, window_minutes: int, *, now: Optional[datetime] = None) -> List[WindowSample]:
        """Merged minute-window observations for the detector. Copies only
        -- the detector never mutates collector state. Windows restart
        empty after a process restart by construction."""
        now = now or datetime.now(timezone.utc)
        cutoff_minute = int(now.timestamp()) // 60 - window_minutes + 1
        samples: List[WindowSample] = []
        with self._lock:
            for (method, route), windows in self._windows.items():
                success_hist = [0] * HIST_SIZE
                stream_hist = [0] * HIST_SIZE
                success_count = 0
                stream_count = 0
                minutes_covered = 0
                for minute_key, (success_window, stream_window) in windows.items():
                    if minute_key < cutoff_minute:
                        continue
                    minutes_covered += 1
                    if success_window.count:
                        success_count += success_window.count
                        for index, count in enumerate(success_window.hist):
                            success_hist[index] += count
                    if stream_window.count:
                        stream_count += stream_window.count
                        for index, count in enumerate(stream_window.hist):
                            stream_hist[index] += count
                if success_count or stream_count:
                    samples.append(WindowSample(
                        key=EndpointKey(method, route, TRAFFIC_BUSINESS),
                        success_count=success_count,
                        success_hist=success_hist,
                        stream_count=stream_count,
                        stream_hist=stream_hist,
                        minutes_covered=minutes_covered,
                    ))
        return samples


class MinuteWindow:
    """One minute of success observations for one endpoint (bounded)."""

    __slots__ = ("count", "hist")

    def __init__(self) -> None:
        self.count = 0
        self.hist = [0] * HIST_SIZE


def _delta_of(acc: Accumulator, snapshot: Accumulator) -> Optional[Accumulator]:
    delta = Accumulator()
    delta.request_count = acc.request_count - snapshot.request_count
    delta.class_counts = {
        cls: acc.class_counts.get(cls, 0) - snapshot.class_counts.get(cls, 0)
        for cls in set(acc.class_counts) | set(snapshot.class_counts)
        if acc.class_counts.get(cls, 0) - snapshot.class_counts.get(cls, 0) != 0
    }
    delta.completed_count = acc.completed_count - snapshot.completed_count
    delta.duration_sum_us = acc.duration_sum_us - snapshot.duration_sum_us
    delta.success_count = acc.success_count - snapshot.success_count
    delta.success_duration_sum_us = acc.success_duration_sum_us - snapshot.success_duration_sum_us
    delta.hist = [a - b for a, b in zip(acc.hist, snapshot.hist)]
    delta.stream_count = acc.stream_count - snapshot.stream_count
    delta.stream_error_count = acc.stream_error_count - snapshot.stream_error_count
    delta.stream_duration_sum_us = acc.stream_duration_sum_us - snapshot.stream_duration_sum_us
    delta.stream_ttfb_sum_us = acc.stream_ttfb_sum_us - snapshot.stream_ttfb_sum_us
    # Interval min/max are maintained in-interval: a delta carries the
    # bucket's current extremes only when this flush actually observed
    # completions of the respective kind.
    if delta.completed_count > 0:
        delta.duration_min_us = acc.duration_min_us
        delta.duration_max_us = acc.duration_max_us
    if delta.success_count > 0:
        delta.success_min_us = acc.success_min_us
        delta.success_max_us = acc.success_max_us
    if delta.stream_count > 0:
        delta.stream_min_us = acc.stream_min_us
        delta.stream_max_us = acc.stream_max_us
        delta.stream_ttfb_min_us = acc.stream_ttfb_min_us
        delta.stream_ttfb_max_us = acc.stream_ttfb_max_us
    if _is_empty(delta):
        return None
    return delta


def _is_empty(delta: Accumulator) -> bool:
    return (
        delta.request_count == 0
        and not delta.class_counts
        and not any(delta.hist)
        and delta.completed_count == 0
        and delta.success_count == 0
        and delta.stream_count == 0
    )


def _advance_snapshot(snapshot: Accumulator, delta: Accumulator) -> None:
    """snapshot := snapshot + delta -- the acc values this batch covered."""
    snapshot.request_count += delta.request_count
    for cls, count in delta.class_counts.items():
        snapshot.class_counts[cls] = snapshot.class_counts.get(cls, 0) + count
    snapshot.completed_count += delta.completed_count
    snapshot.duration_sum_us += delta.duration_sum_us
    snapshot.success_count += delta.success_count
    snapshot.success_duration_sum_us += delta.success_duration_sum_us
    for index, count in enumerate(delta.hist):
        snapshot.hist[index] += count
    snapshot.stream_count += delta.stream_count
    snapshot.stream_error_count += delta.stream_error_count
    snapshot.stream_duration_sum_us += delta.stream_duration_sum_us
    snapshot.stream_ttfb_sum_us += delta.stream_ttfb_sum_us


class ApiPerformanceMiddleware:
    """Pure-ASGI collection middleware. Times each request with a monotonic
    clock to the last body byte; classifies exceptions, cancellation and
    stream errors without ever disguising an unfinished response as
    success. Background tasks that run after the final body byte are
    excluded by construction (timing stops at the final send's return)."""

    def __init__(self, app: Any, collector: ApiPerformanceCollector) -> None:
        self.app = app
        self.collector = collector

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        tracker = _RequestTracker(self.collector, scope)
        try:
            await self.app(
                scope,
                receive=tracker.wrap_receive(receive),
                send=tracker.wrap_send(send),
            )
        except Exception:
            tracker.on_exception()
            raise
        except BaseException:
            # asyncio.CancelledError and friends: shutdown/cancellation is
            # not a server exception; record it as cancellation, honestly.
            tracker.on_cancelled()
            raise
        else:
            tracker.on_app_return()


def _status_class_for(status_code: int) -> str:
    if status_code < 200:
        return CLASS_1XX
    if status_code < 300:
        return CLASS_2XX
    if status_code < 400:
        return CLASS_3XX
    if status_code < 500:
        return CLASS_4XX
    return CLASS_5XX


def _header_value(headers: List[Any], name: bytes) -> str:
    for entry in headers:
        key, value = entry[0], entry[1]
        if key.lower() == name:
            return value.decode("latin-1", "replace")
    return ""


class _RequestTracker:
    __slots__ = (
        "collector", "scope", "method", "route", "traffic_class", "start",
        "status_class", "is_stream", "response_started", "completed",
        "recorded", "identity_resolved", "disconnected", "ttfb_us",
    )

    def __init__(self, collector: ApiPerformanceCollector, scope: Dict[str, Any]) -> None:
        self.collector = collector
        self.scope = scope
        self.method = normalize_method(scope.get("method", ""))
        # Identity is resolved LAZILY: scope["route"] is written by routing,
        # which runs after this middleware is entered. Reading it eagerly
        # would classify every request as unmatched. Static mounts never set
        # scope["route"], so their prefix classification happens up front.
        self.route = UNMATCHED_ROUTE
        self.traffic_class = TRAFFIC_UNMATCHED
        self.identity_resolved = False
        if (scope.get("path", "") or "").startswith(_STATIC_PREFIX):
            self.traffic_class = TRAFFIC_INFRA
            self.identity_resolved = True
        self.start = time.monotonic()
        self.status_class = None  # type: Optional[str]
        self.is_stream = False
        self.response_started = False
        self.completed = False
        self.recorded = False
        self.disconnected = False
        self.ttfb_us = None  # type: Optional[int]

    def _resolve_identity(self) -> None:
        if self.identity_resolved:
            return
        self.identity_resolved = True
        route = self.scope.get("route")
        template = getattr(route, "path_format", None) or getattr(route, "path", None)
        raw_path = self.scope.get("path", "") or ""
        if not template:
            self.route = UNMATCHED_ROUTE
            self.traffic_class = TRAFFIC_INFRA if raw_path.startswith(_STATIC_PREFIX) else TRAFFIC_UNMATCHED
            return
        if len(template) > _ROUTE_LABEL_MAX:
            template = template[:_ROUTE_LABEL_MAX]
        self.route = template
        self.traffic_class = classify_traffic(template, raw_path)

    def _elapsed_us(self) -> int:
        return int((time.monotonic() - self.start) * 1_000_000)

    def wrap_receive(self, receive: Any) -> Any:
        tracker = self

        async def _receive() -> Dict[str, Any]:
            message = await receive()
            if message.get("type") == "http.disconnect":
                tracker.disconnected = True
            return message

        return _receive

    def wrap_send(self, send: Any) -> Any:
        tracker = self

        async def _send(message: Dict[str, Any]) -> None:
            message_type = message.get("type")
            if message_type == "http.response.start" and not tracker.response_started:
                tracker.response_started = True
                tracker.ttfb_us = tracker._elapsed_us()
                tracker.status_class = _status_class_for(int(message.get("status", 0)))
                headers = message.get("headers") or []
                content_type = _header_value(headers, b"content-type").split(";", 1)[0].strip().lower()
                tracker.is_stream = content_type == _STREAM_CONTENT_TYPE
            elif message_type == "http.response.body" and not message.get("more_body", False):
                tracker.completed = True
                tracker._record(duration_us=tracker._elapsed_us())
            await send(message)

        return _send

    def on_app_return(self) -> None:
        if self.recorded or self.completed:
            return
        if self.response_started:
            # A stream that ended without an explicit final empty body
            # chunk: count it as a completed stream ending at return.
            self._record(duration_us=self._elapsed_us())
        elif self.disconnected:
            self._record_cancelled()
        else:
            # The app returned without ever starting a response: a server
            # bug; never disguise it as success.
            self._record_failure(CLASS_EXCEPTION)

    def on_exception(self) -> None:
        if self.recorded:
            # A failure after the response completed (e.g. a background
            # task raising post-send) is outside request latency.
            return
        if self.response_started:
            # Mid-stream failure after HTTP 200: stream error, counted
            # separately, never in the normal latency ranking.
            self._record(duration_us=self._elapsed_us(), stream_error=True)
        else:
            self._record_failure(CLASS_EXCEPTION)

    def on_cancelled(self) -> None:
        if self.recorded:
            return
        if self.response_started:
            self._record(duration_us=self._elapsed_us(), stream_error=True)
        else:
            self._record_cancelled()

    def _record(self, *, duration_us: int, stream_error: bool = False) -> None:
        if self.recorded:
            return
        self.recorded = True
        self._resolve_identity()
        self.collector.record(Observation(
            method=self.method,
            route=self.route,
            traffic_class=self.traffic_class,
            status_class=self.status_class or CLASS_5XX,
            duration_us=duration_us,
            is_stream=self.is_stream,
            stream_error=stream_error,
            stream_ttfb_us=self.ttfb_us if self.is_stream else None,
            completed_at=datetime.now(timezone.utc),
        ))

    def _record_failure(self, status_class: str) -> None:
        if self.recorded:
            return
        self.recorded = True
        self._resolve_identity()
        self.collector.record(Observation(
            method=self.method,
            route=self.route,
            traffic_class=self.traffic_class,
            status_class=status_class,
            duration_us=self._elapsed_us(),
            is_stream=False,
            stream_error=False,
            stream_ttfb_us=None,
            completed_at=datetime.now(timezone.utc),
        ))

    def _record_cancelled(self) -> None:
        if self.recorded:
            return
        self.recorded = True
        self._resolve_identity()
        self.collector.record(Observation(
            method=self.method,
            route=self.route,
            traffic_class=self.traffic_class,
            status_class=CLASS_CANCELLED,
            duration_us=None,
            is_stream=False,
            stream_error=False,
            stream_ttfb_us=None,
            completed_at=datetime.now(timezone.utc),
        ))
