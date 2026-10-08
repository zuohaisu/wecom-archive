"""GH-186 API performance persistence: batched flush with atomic dedup,
bounded retention cleanup, and aggregate-only read queries.

Transaction/dedup contract (ADR-0007): one flush batch == one transaction.
The batch row (unique batch_id) is inserted FIRST; its existence is the
dedup proof. A commit whose outcome is unknown is retried with the SAME
batch id -- the insert then conflicts, the whole transaction rolls back,
and the increments are never applied twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    ApiPerformanceAlertState,
    ApiPerformanceEndpointDaily,
    ApiPerformanceEndpointHourly,
    ApiPerformanceFlushBatch,
)
from app.services.api_performance_collector import (
    ERROR_CLASSES,
    HIST_SIZE,
    HIST_VERSION,
    STATS_VERSION,
    ApiPerformanceCollector,
    BucketDelta,
    day_bucket_date,
    estimate_p95_us,
)

logger = logging.getLogger(__name__)

FLUSH_APPLIED = "applied"
FLUSH_ALREADY_APPLIED = "already_applied"

_HIST_MIXED_VERSION = -1
_MAX_DELETE_ROUNDS = 5
_DELETE_CHUNK = 500
_QUERY_ROW_CAP = 50000


class FlushConflictError(RuntimeError):
    """A concurrent writer inserted the same aggregate row mid-batch.
    The batch is left unapplied; the collector retries the same batch id."""


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def flush_deltas(
    db: Session,
    batch_id: str,
    instance_id: str,
    deltas: List[BucketDelta],
    *,
    now: Optional[datetime] = None,
    retention_hours: Optional[int] = None,
    retention_days: Optional[int] = None,
) -> str:
    """Apply one flush batch atomically. Returns FLUSH_APPLIED or
    FLUSH_ALREADY_APPLIED; raises FlushConflictError on a row race (retry
    with the same batch id resolves it) and lets DB errors propagate so
    the caller records the failure and retries the same batch later.

    When retention bounds are provided, increments for already-expired
    buckets are rejected -- a late retry must not resurrect rows cleanup
    already removed. Rejection is per delta: an expired hour never
    suppresses its still-valid daily counterpart."""
    if now is not None and retention_hours is not None and retention_days is not None:
        hourly_cutoff = now - timedelta(hours=retention_hours)
        daily_cutoff = day_bucket_date(now - timedelta(days=retention_days))
        kept: List[BucketDelta] = []
        for delta in deltas:
            if delta.granularity == "hourly" and delta.bucket_start <= hourly_cutoff:
                continue
            if delta.granularity == "daily" and delta.bucket_date is not None and delta.bucket_date < daily_cutoff:
                continue
            kept.append(delta)
        deltas = kept
    try:
        db.add(ApiPerformanceFlushBatch(
            batch_id=batch_id,
            instance_id=instance_id[:64],
            flush_seq=_seq_of(batch_id),
        ))
        db.flush()
    except IntegrityError:
        # The batch id already exists: a previous attempt committed. The
        # increments are in the DB exactly once -- do not apply again.
        db.rollback()
        return FLUSH_ALREADY_APPLIED

    for delta in deltas:
        if not _delta_has_content(delta):
            continue
        if delta.granularity == "hourly":
            _merge_delta(db, ApiPerformanceEndpointHourly, delta)
        else:
            _merge_delta(db, ApiPerformanceEndpointDaily, delta)
    db.commit()
    return FLUSH_APPLIED


def _seq_of(batch_id: str) -> int:
    try:
        return int(batch_id.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def _delta_has_content(delta: BucketDelta) -> bool:
    acc = delta.acc
    return bool(
        acc.request_count
        or acc.class_counts
        or any(acc.hist)
        or acc.completed_count
        or acc.stream_count
    )


def _merge_delta(db: Session, model, delta: BucketDelta) -> None:
    acc = delta.acc
    if model is ApiPerformanceEndpointHourly:
        match = {"method": delta.key.method, "route": delta.key.route, "bucket_start": delta.bucket_start}
    else:
        match = {"method": delta.key.method, "route": delta.key.route, "bucket_date": delta.bucket_date}
    row = db.query(model).filter_by(**match).first()
    if row is None:
        row = model(
            method=delta.key.method,
            route=delta.key.route[:500],
            traffic_class=delta.key.traffic_class[:16],
            **({k: v for k, v in match.items() if k not in ("method", "route")}),
            request_count=acc.request_count,
            class_counts=dict(acc.class_counts),
            completed_count=acc.completed_count,
            duration_sum_us=acc.duration_sum_us,
            duration_min_us=acc.duration_min_us,
            duration_max_us=acc.duration_max_us,
            success_count=acc.success_count,
            success_duration_sum_us=acc.success_duration_sum_us,
            success_min_us=acc.success_min_us,
            success_max_us=acc.success_max_us,
            hist=list(acc.hist) if acc.success_count else None,
            hist_version=HIST_VERSION if acc.success_count else 0,
            stream_count=acc.stream_count,
            stream_error_count=acc.stream_error_count,
            stream_duration_sum_us=acc.stream_duration_sum_us,
            stream_min_us=acc.stream_min_us,
            stream_max_us=acc.stream_max_us,
            stream_ttfb_sum_us=acc.stream_ttfb_sum_us,
            stream_ttfb_min_us=acc.stream_ttfb_min_us,
            stream_ttfb_max_us=acc.stream_ttfb_max_us,
            stats_version=STATS_VERSION,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError as error:
            # Another writer created this (endpoint, bucket) row between
            # our SELECT and INSERT. Do not half-apply: abort the whole
            # batch; the collector retries the same batch id, and the row
            # then exists for the merge path.
            db.rollback()
            raise FlushConflictError(str(error)) from error
        return

    row.request_count += acc.request_count
    for cls, count in acc.class_counts.items():
        merged = dict(row.class_counts or {})
        merged[cls] = merged.get(cls, 0) + count
        row.class_counts = merged
    row.completed_count += acc.completed_count
    row.duration_sum_us += acc.duration_sum_us
    row.duration_min_us = _min_us(row.duration_min_us, acc.duration_min_us)
    row.duration_max_us = _max_us(row.duration_max_us, acc.duration_max_us)
    row.success_count += acc.success_count
    row.success_duration_sum_us += acc.success_duration_sum_us
    row.success_min_us = _min_us(row.success_min_us, acc.success_min_us)
    row.success_max_us = _max_us(row.success_max_us, acc.success_max_us)
    row.stream_count += acc.stream_count
    row.stream_error_count += acc.stream_error_count
    row.stream_duration_sum_us += acc.stream_duration_sum_us
    row.stream_min_us = _min_us(row.stream_min_us, acc.stream_min_us)
    row.stream_max_us = _max_us(row.stream_max_us, acc.stream_max_us)
    row.stream_ttfb_sum_us += acc.stream_ttfb_sum_us
    row.stream_ttfb_min_us = _min_us(row.stream_ttfb_min_us, acc.stream_ttfb_min_us)
    row.stream_ttfb_max_us = _max_us(row.stream_ttfb_max_us, acc.stream_ttfb_max_us)
    if acc.success_count:
        existing_hist = row.hist
        if row.hist_version == 0 or not isinstance(existing_hist, list) or not existing_hist:
            # The row existed with errors only and never had a histogram:
            # initialise it with this delta's layout instead of poisoning
            # the bucket (an error-only past is not a version conflict).
            row.hist = list(acc.hist)
            row.hist_version = HIST_VERSION
        elif row.hist_version == HIST_VERSION and len(existing_hist) == HIST_SIZE:
            row.hist = [a + b for a, b in zip(existing_hist, acc.hist)]
        else:
            # Bucket-boundary layouts differ (or the row was already
            # mixed): histograms must never be added across versions.
            # Scalars above are boundary-independent and stay merged.
            row.hist = None
            row.hist_version = _HIST_MIXED_VERSION
    db.flush()


def _min_us(existing: Optional[int], incoming: Optional[int]) -> Optional[int]:
    if incoming is None:
        return existing
    if existing is None:
        return incoming
    return min(existing, incoming)


def _max_us(existing: Optional[int], incoming: Optional[int]) -> Optional[int]:
    if incoming is None:
        return existing
    if existing is None:
        return incoming
    return max(existing, incoming)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def run_retention(
    db: Session,
    *,
    now: datetime,
    retention_hours: int,
    retention_days: int,
    batch_retention_hours: int,
) -> Dict[str, int]:
    """Bounded, chunked cleanup of THIS module's rows only. Runs on the
    flush loop's cadence regardless of request traffic; late batch retries
    never resurrect expired buckets (rows are gone, and the flush rejects
    expired increments anyway).

    Boundaries: hourly rows are deleted when their Asia/Shanghai hour
    start is at or before ``now - retention_hours`` (exactly 720 full
    hour buckets stay on an hour boundary); daily rows use the Asia/
    Shanghai natural date of ``now - retention_days`` (bucket_date is a
    Shanghai day, so the cutoff must be too)."""
    deleted = {"hourly": 0, "daily": 0, "batches": 0}
    hourly_cutoff = now - timedelta(hours=retention_hours)
    daily_cutoff = day_bucket_date(now - timedelta(days=retention_days))
    batch_cutoff = now - timedelta(hours=batch_retention_hours)
    deleted["hourly"] = _delete_in_chunks(
        db, ApiPerformanceEndpointHourly, ApiPerformanceEndpointHourly.bucket_start <= hourly_cutoff
    )
    deleted["daily"] = _delete_in_chunks(
        db, ApiPerformanceEndpointDaily, ApiPerformanceEndpointDaily.bucket_date < daily_cutoff
    )
    deleted["batches"] = _delete_in_chunks(
        db, ApiPerformanceFlushBatch, ApiPerformanceFlushBatch.created_at < batch_cutoff
    )
    db.commit()
    return deleted


def _delete_in_chunks(db: Session, model, criterion) -> int:
    pk = model.__table__.primary_key.columns[0]
    total = 0
    for _round in range(_MAX_DELETE_ROUNDS):
        ids = [
            row_id
            for (row_id,) in db.query(pk).filter(criterion).limit(_DELETE_CHUNK).all()
        ]
        if not ids:
            break
        db.query(model).filter(pk.in_(ids)).delete(synchronize_session=False)
        db.flush()
        total += len(ids)
    return total


# ---------------------------------------------------------------------------
# Daily alert intent (sent-before-persist is forbidden; see ADR-0007)
# ---------------------------------------------------------------------------


def claim_daily_alert(db: Session, *, alert_date: date, instance_id: str, anomaly_count: int) -> Optional[ApiPerformanceAlertState]:
    """Create today's single notification intent. Returns None when the
    intent already exists OR persistence failed -- in both cases no email
    may be sent (fail closed on the quota record)."""
    try:
        row = ApiPerformanceAlertState(
            alert_date=alert_date,
            instance_id=instance_id[:64],
            status="pending",
            anomaly_count=anomaly_count,
        )
        db.add(row)
        db.commit()
        return row
    except IntegrityError:
        db.rollback()
        return None
    except Exception:
        db.rollback()
        logger.warning("api performance alert intent persistence failed", exc_info=True)
        return None


def finish_daily_alert(db: Session, alert_id: int, status: str) -> None:
    """Record the send outcome. An unknown/failed outcome stays
    ``failed_or_unknown`` and is never retried today (one attempt per
    day, conservative by design)."""
    try:
        row = db.get(ApiPerformanceAlertState, alert_id)
        if row is None:
            return
        row.status = status
        row.attempted_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("api performance alert outcome persistence failed", exc_info=True)


def daily_alert_status(db: Session, alert_date: date) -> Optional[ApiPerformanceAlertState]:
    try:
        return db.query(ApiPerformanceAlertState).filter(ApiPerformanceAlertState.alert_date == alert_date).first()
    except Exception:
        logger.warning("api performance alert status read failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Read side: merged aggregates (never per-request data)
# ---------------------------------------------------------------------------


@dataclass
class MergedStats:
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
    hist: Optional[List[int]] = None
    hist_version: int = 0
    hist_unavailable: bool = False  # versions mixed across merged rows
    stream_count: int = 0
    stream_error_count: int = 0
    stream_duration_sum_us: int = 0
    stream_min_us: Optional[int] = None
    stream_max_us: Optional[int] = None
    stream_ttfb_sum_us: int = 0
    stream_ttfb_min_us: Optional[int] = None
    stream_ttfb_max_us: Optional[int] = None

    @property
    def error_count(self) -> int:
        return sum(self.class_counts.get(cls, 0) for cls in ERROR_CLASSES)

    @property
    def avg_us(self) -> Optional[int]:
        return self.duration_sum_us // self.completed_count if self.completed_count else None

    @property
    def avg_success_us(self) -> Optional[int]:
        return self.success_duration_sum_us // self.success_count if self.success_count else None

    def p95_estimate(self) -> Tuple[Optional[int], bool]:
        if not self.hist:
            return None, False
        return estimate_p95_us(self.hist)


def _merge_row_into(stats: MergedStats, row) -> None:
    stats.request_count += row.request_count
    for cls, count in (row.class_counts or {}).items():
        stats.class_counts[cls] = stats.class_counts.get(cls, 0) + count
    stats.completed_count += row.completed_count
    stats.duration_sum_us += row.duration_sum_us
    stats.duration_min_us = _min_us(stats.duration_min_us, row.duration_min_us)
    stats.duration_max_us = _max_us(stats.duration_max_us, row.duration_max_us)
    stats.success_count += row.success_count
    stats.success_duration_sum_us += row.success_duration_sum_us
    stats.success_min_us = _min_us(stats.success_min_us, row.success_min_us)
    stats.success_max_us = _max_us(stats.success_max_us, row.success_max_us)
    stats.stream_count += row.stream_count
    stats.stream_error_count += row.stream_error_count
    stats.stream_duration_sum_us += row.stream_duration_sum_us
    stats.stream_min_us = _min_us(stats.stream_min_us, row.stream_min_us)
    stats.stream_max_us = _max_us(stats.stream_max_us, row.stream_max_us)
    stats.stream_ttfb_sum_us += row.stream_ttfb_sum_us
    stats.stream_ttfb_min_us = _min_us(stats.stream_ttfb_min_us, row.stream_ttfb_min_us)
    stats.stream_ttfb_max_us = _max_us(stats.stream_ttfb_max_us, row.stream_ttfb_max_us)
    if row.hist_version == _HIST_MIXED_VERSION:
        # This row itself merged incompatible histogram versions: poison.
        stats.hist = None
        stats.hist_unavailable = True
        return
    row_hist = row.hist
    if row.hist_version == 0 or not isinstance(row_hist, list) or not row_hist:
        # The row simply has no success observations (errors/cancelled
        # only): nothing to merge and nothing to poison.
        return
    if stats.hist is None:
        if stats.hist_unavailable:
            return
        if stats.hist_version == 0 or stats.hist_version == row.hist_version:
            stats.hist = list(row_hist) if row_hist else None
            if stats.hist is not None:
                stats.hist_version = row.hist_version
            return
        stats.hist = None
        stats.hist_unavailable = True
        return
    if stats.hist_version == row.hist_version and len(row_hist) == len(stats.hist):
        stats.hist = [a + b for a, b in zip(stats.hist, row_hist)]
    else:
        # Version mixing across merged rows: p95 becomes unavailable
        # rather than fabricated.
        stats.hist = None
        stats.hist_unavailable = True


def query_endpoint_rows(
    db: Session,
    model,
    *,
    since,
    until,
    method: Optional[str] = None,
    route: Optional[str] = None,
    limit: int = _QUERY_ROW_CAP,
) -> Tuple[List, bool]:
    """Rows for a half-open [since, until) bucket range, hard-capped so a
    pathological query can never scan unbounded. ``since``/``until`` are
    datetimes for the hourly table and dates for the daily table.

    Returns (rows, truncated). ``truncated`` is True when the logical
    range had more rows than the cap -- callers must surface it instead
    of presenting silently incomplete aggregates."""
    bucket_column = model.bucket_start if hasattr(model, "bucket_start") else model.bucket_date
    query = db.query(model).filter(bucket_column >= since, bucket_column < until)
    if method:
        query = query.filter(model.method == method)
    if route:
        query = query.filter(model.route == route)
    rows = query.limit(limit + 1).all()
    truncated = len(rows) > limit
    return rows[:limit], truncated


def query_endpoint_summaries(
    db: Session,
    collector: ApiPerformanceCollector,
    *,
    since_hour: datetime,
    until_hour: datetime,
    limit: int = _QUERY_ROW_CAP,
) -> Tuple[List[Dict], bool]:
    """Endpoint-level rollup over an hourly window, merged with the route
    registry: never-called (method, template) pairs show as no-sample,
    retired routes keep their history and are flagged.

    The identity is the (method, template) PAIR: one template registered
    for several methods contributes one no-sample entry per method."""
    rows, truncated = query_endpoint_rows(
        db, ApiPerformanceEndpointHourly, since=since_hour, until=until_hour, limit=limit
    )
    grouped: Dict[Tuple[str, str, str], List] = {}
    for row in rows:
        grouped.setdefault((row.method, row.route, row.traffic_class), []).append(row)
    known_templates = {route for route, _methods, _cls in collector.known_routes()}
    summaries = []
    for (method, route, traffic_class), bucket_rows in grouped.items():
        stats = MergedStats()
        for row in bucket_rows:
            _merge_row_into(stats, row)
        summaries.append({
            "method": method,
            "route": route,
            "traffic_class": traffic_class,
            "registered": route in known_templates,
            "stats": stats,
            "bucket_count": len(bucket_rows),
        })
    present = {(s["method"], s["route"], s["traffic_class"]) for s in summaries}
    for reg_route, reg_methods, reg_class in collector.known_routes():
        for reg_method in reg_methods:
            identity = (reg_method, reg_route, reg_class)
            if identity not in present:
                summaries.append({
                    "method": reg_method,
                    "route": reg_route,
                    "traffic_class": reg_class,
                    "registered": True,
                    "stats": MergedStats(),
                    "bucket_count": 0,
                })
                present.add(identity)
    return summaries, truncated


def query_bucket_series(
    db: Session,
    model,
    *,
    since,
    until,
    method: Optional[str] = None,
    route: Optional[str] = None,
    traffic_class: Optional[str] = None,
    limit: int = _QUERY_ROW_CAP,
) -> Tuple[List[Dict], bool]:
    """Per-bucket merged series; ``route=None`` merges ALL endpoints into
    a site-wide weighted aggregate per bucket (sum counts, sum durations,
    merge histograms). Returns (points, truncated)."""
    rows, truncated = query_endpoint_rows(
        db, model, since=since, until=until, method=method, route=route, limit=limit
    )
    if traffic_class is not None:
        rows = [row for row in rows if row.traffic_class == traffic_class]
    has_hour_key = hasattr(model, "bucket_start")
    grouped: Dict[object, List] = {}
    for row in rows:
        grouped.setdefault(row.bucket_start if has_hour_key else row.bucket_date, []).append(row)
    points = []
    for bucket_key in sorted(grouped.keys()):
        stats = MergedStats()
        for row in grouped[bucket_key]:
            _merge_row_into(stats, row)
        points.append({
            "bucket": bucket_key,
            "stats": stats,
            "endpoint_count": len(grouped[bucket_key]),
        })
    return points, truncated


def query_coverage(db: Session) -> Dict[str, Dict[str, Optional[datetime]]]:
    """Persisted coverage bounds for the honest status display."""
    coverage: Dict[str, Dict[str, Optional[datetime]]] = {}
    hourly = db.query(
        func.min(ApiPerformanceEndpointHourly.bucket_start),
        func.max(ApiPerformanceEndpointHourly.bucket_start),
        func.count(ApiPerformanceEndpointHourly.id),
    ).first()
    daily = db.query(
        func.min(ApiPerformanceEndpointDaily.bucket_date),
        func.max(ApiPerformanceEndpointDaily.bucket_date),
        func.count(ApiPerformanceEndpointDaily.id),
    ).first()
    coverage["hourly"] = {
        "earliest": hourly[0],
        "latest": hourly[1],
        "rows": int(hourly[2] or 0),
    }
    coverage["daily"] = {
        "earliest": daily[0],
        "latest": daily[1],
        "rows": int(daily[2] or 0),
    }
    return coverage
