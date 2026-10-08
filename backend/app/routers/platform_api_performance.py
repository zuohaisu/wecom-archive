"""GH-186 super-admin API performance page + read-only statistics APIs.

Platform-admin-only (page redirects to /platform/login like every other
platform shell; APIs 401). Aggregate-only responses; query parameters are
tightly bounded; nothing here ever exposes request identifiers or content.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_platform_admin, require_platform_admin_optional
from app.db.models import ApiPerformanceEndpointDaily, ApiPerformanceEndpointHourly, PlatformAdmin
from app.db.session import get_db
from app.schemas.api_performance import (
    AnomalyListOut,
    AnomalyOut,
    ApiPerformanceStatusOut,
    CoverageStatusOut,
    DetectionStatusOut,
    EmailStatusOut,
    EndpointListOut,
    EndpointStatsOut,
    EndpointSummaryOut,
    FlushStatusOut,
    SeriesOut,
    SeriesPointOut,
)
from app.services.api_performance_collector import (
    CLASS_CANCELLED,
    OTHER_METHOD,
    ApiPerformanceConfig,
    daily_bucket_start,
    day_bucket_date,
    hour_bucket_start,
)
from app.services.api_performance_service import ApiPerformanceRuntime
from app.services.api_performance_store import (
    MergedStats,
    daily_alert_status,
    query_bucket_series,
    query_coverage,
    query_endpoint_summaries,
)
from app.web import render_template
from app.web.sidenav import render_platform_admin_bar, render_platform_sidenav, render_platform_topbar

logger = logging.getLogger(__name__)
router = APIRouter(tags=["api-performance"])

_PAGE_PATH = "/platform/api-performance"
_HOURLY_RETENTION_HOURS = 720
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_SORT_KEYS = {"requests", "avg_ms", "p95_ms", "error_rate"}
_TRAFFIC_CLASSES = {"business", "infra", "self", "unmatched"}


def _runtime(request: Request) -> ApiPerformanceRuntime:
    runtime = getattr(request.app.state, "api_performance_runtime", None)
    if runtime is None:  # pragma: no cover - create_app always wires it
        raise HTTPException(status_code=503, detail="api_performance_not_enabled")
    return runtime


def _ms(value_us: Optional[int]) -> Optional[float]:
    return round(value_us / 1000.0, 3) if value_us is not None else None


def _stats_out(stats: MergedStats) -> EndpointStatsOut:
    p95_us, p95_capped = stats.p95_estimate()
    errors = stats.error_count
    return EndpointStatsOut(
        requests=stats.request_count,
        errors=errors,
        error_rate=round(errors / stats.request_count, 6) if stats.request_count else None,
        cancelled=stats.class_counts.get(CLASS_CANCELLED, 0),
        avg_ms=_ms(stats.avg_us),
        min_ms=_ms(stats.duration_min_us),
        max_ms=_ms(stats.duration_max_us),
        avg_success_ms=_ms(stats.avg_success_us),
        success_min_ms=_ms(stats.success_min_us),
        success_max_ms=_ms(stats.success_max_us),
        p95_ms=_ms(p95_us),
        p95_capped=p95_capped,
        class_counts=stats.class_counts,
        stream_count=stats.stream_count,
        stream_errors=stats.stream_error_count,
        stream_avg_duration_ms=(
            round(stats.stream_duration_sum_us / stats.stream_count / 1000.0, 3)
            if stats.stream_count else None
        ),
        stream_avg_start_ms=(
            round(stats.stream_ttfb_sum_us / stats.stream_count / 1000.0, 3)
            if stats.stream_count else None
        ),
        hist_available=bool(stats.hist),
    )


def _min_opt(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_opt(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _merged_site(stats_list) -> MergedStats:
    """Site-wide weighted rollup: sums stay weighted averages; histograms
    merge only within one version, else p95 becomes honestly unavailable."""
    site = MergedStats()
    hist_acc: Optional[list] = None
    hist_version = 0
    hist_ok = True
    for summary in stats_list:
        stats: MergedStats = summary["stats"]
        site.request_count += stats.request_count
        for cls, count in stats.class_counts.items():
            site.class_counts[cls] = site.class_counts.get(cls, 0) + count
        site.completed_count += stats.completed_count
        site.duration_sum_us += stats.duration_sum_us
        site.duration_min_us = _min_opt(site.duration_min_us, stats.duration_min_us)
        site.duration_max_us = _max_opt(site.duration_max_us, stats.duration_max_us)
        site.success_count += stats.success_count
        site.success_duration_sum_us += stats.success_duration_sum_us
        site.success_min_us = _min_opt(site.success_min_us, stats.success_min_us)
        site.success_max_us = _max_opt(site.success_max_us, stats.success_max_us)
        site.stream_count += stats.stream_count
        site.stream_error_count += stats.stream_error_count
        site.stream_duration_sum_us += stats.stream_duration_sum_us
        site.stream_ttfb_sum_us += stats.stream_ttfb_sum_us
        if stats.hist_unavailable:
            hist_ok = False
        elif stats.hist is None:
            pass  # no hist data in this slice; nothing to merge, nothing to poison
        elif hist_acc is None:
            if hist_version in (0, stats.hist_version):
                hist_acc = list(stats.hist)
                hist_version = stats.hist_version
            else:
                hist_ok = False
        elif hist_version == stats.hist_version and len(stats.hist) == len(hist_acc):
            hist_acc = [a + b for a, b in zip(hist_acc, stats.hist)]
        else:
            hist_ok = False
    site.hist = hist_acc if hist_ok else None
    site.hist_version = hist_version if hist_ok else 0
    return site


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@router.get(_PAGE_PATH, response_class=HTMLResponse, name="platform_page_api_performance")
def api_performance_page(
    admin: Optional[PlatformAdmin] = Depends(require_platform_admin_optional),
) -> HTMLResponse:
    if admin is None:
        return RedirectResponse("/platform/login", status_code=302)
    return HTMLResponse(
        render_template(
            "platform_api_performance",
            sidenav=render_platform_sidenav("api-performance"),
            admin_bar=render_platform_admin_bar(admin.email),
            topbar=render_platform_topbar("接口性能"),
        )
    )


# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------


def _validate_method(method: Optional[str]) -> Optional[str]:
    if method is None or method == "":
        return None
    upper = method.upper()
    if upper not in _HTTP_METHODS and upper != OTHER_METHOD:
        raise HTTPException(status_code=422, detail="invalid_method_filter")
    return upper


def _validate_traffic_class(traffic_class: str) -> Optional[str]:
    if traffic_class == "all":
        return None
    if traffic_class not in _TRAFFIC_CLASSES:
        raise HTTPException(status_code=422, detail="invalid_traffic_class_filter")
    return traffic_class


@router.get("/api/platform/api-performance/endpoints", response_model=EndpointListOut)
def api_performance_endpoints(
    request: Request,
    window_hours: int = Query(24, ge=1, le=168),
    method: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=200),
    traffic_class: str = Query("business"),
    sort: str = Query("requests"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    _platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> EndpointListOut:
    runtime = _runtime(request)
    if sort not in _SORT_KEYS:
        raise HTTPException(status_code=422, detail="invalid_sort_key")
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="invalid_sort_order")
    method = _validate_method(method)
    traffic = _validate_traffic_class(traffic_class)
    now = datetime.now(timezone.utc)
    until_hour = hour_bucket_start(now) + timedelta(hours=1)
    since_hour = until_hour - timedelta(hours=window_hours)
    summaries, truncated = query_endpoint_summaries(
        db, runtime.collector, since_hour=since_hour, until_hour=until_hour
    )
    if method:
        summaries = [s for s in summaries if s["method"] == method]
    if traffic is not None:
        summaries = [s for s in summaries if s["traffic_class"] == traffic]
    if q:
        summaries = [s for s in summaries if q in s["route"]]

    def _sort_value(summary: dict) -> float:
        stats: MergedStats = summary["stats"]
        if sort == "requests":
            return float(stats.request_count)
        if sort == "avg_ms":
            return (stats.avg_us or 0) / 1000.0
        if sort == "p95_ms":
            p95_us, _capped = stats.p95_estimate()
            return (p95_us or 0) / 1000.0
        return (stats.error_count / stats.request_count) if stats.request_count else 0.0

    summaries.sort(key=_sort_value, reverse=(order == "desc"))
    total = len(summaries)
    start = (page - 1) * page_size
    page_items = summaries[start:start + page_size]
    endpoints = [
        EndpointSummaryOut(
            method=s["method"],
            route=s["route"],
            traffic_class=s["traffic_class"],
            registered=s["registered"],
            has_samples=s["stats"].request_count > 0,
            stats=_stats_out(s["stats"]),
        )
        for s in page_items
    ]
    return EndpointListOut(
        window_hours=window_hours,
        total=total,
        page=page,
        page_size=page_size,
        endpoints=endpoints,
        site=_stats_out(_merged_site(summaries)),
        incomplete=truncated,
    )


def _parse_bucket_day(raw: str):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid_date_filter") from error


def _hourly_window_bounds(now: datetime):
    """Earliest still-retained Shanghai DAY for hourly queries, derived
    from the real 720-hour boundary -- not from 'today minus 29 whole
    days'. The earliest day is usually PARTIAL (its early hours have
    expired); the page states that instead of rejecting the day."""
    oldest_retained_hour = hour_bucket_start(now) - timedelta(hours=_HOURLY_RETENTION_HOURS - 1)
    return day_bucket_date(oldest_retained_hour), day_bucket_date(now)


@router.get("/api/platform/api-performance/series", response_model=SeriesOut)
def api_performance_series(
    request: Request,
    granularity: str = Query("daily"),
    days: int = Query(30, ge=1, le=180),
    date_str: Optional[str] = Query(None, alias="date", max_length=10),
    method: Optional[str] = Query(None),
    route: Optional[str] = Query(None, max_length=500),
    traffic_class: str = Query("business"),
    _platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> SeriesOut:
    _runtime(request)  # 503 when the module is not wired (parity with other APIs)
    method = _validate_method(method)
    traffic = _validate_traffic_class(traffic_class)
    now = datetime.now(timezone.utc)
    if granularity == "daily":
        today = day_bucket_date(now)
        since = today - timedelta(days=days - 1)
        until = today + timedelta(days=1)
        points, truncated = query_bucket_series(
            db, ApiPerformanceEndpointDaily, since=since, until=until,
            method=method, route=route, traffic_class=traffic,
        )
        return SeriesOut(
            granularity="daily", method=method, route=route, site_wide=route is None,
            points=[_point_out(point) for point in points], incomplete=truncated,
        )
    if granularity != "hourly":
        raise HTTPException(status_code=422, detail="invalid_granularity")
    if not date_str:
        raise HTTPException(status_code=422, detail="date_required_for_hourly")
    bucket_day = _parse_bucket_day(date_str)
    oldest_day, today = _hourly_window_bounds(now)
    if bucket_day > today or bucket_day < oldest_day:
        # Days before the 720h boundary have no hourly detail left; the
        # daily series is the honest view for them. The boundary day
        # itself is allowed (it is a partial day -- stated on the page).
        raise HTTPException(status_code=422, detail="hourly_detail_out_of_window")
    day_start = daily_bucket_start(bucket_day)
    points, truncated = query_bucket_series(
        db, ApiPerformanceEndpointHourly, since=day_start, until=day_start + timedelta(days=1),
        method=method, route=route, traffic_class=traffic,
    )
    return SeriesOut(
        granularity="hourly", method=method, route=route, site_wide=route is None,
        points=[_point_out(point) for point in points], incomplete=truncated,
    )


def _point_out(point: dict) -> SeriesPointOut:
    stats: MergedStats = point["stats"]
    p95_us, capped = stats.p95_estimate()
    bucket = point["bucket"]
    return SeriesPointOut(
        bucket=bucket.isoformat() if hasattr(bucket, "isoformat") else str(bucket),
        requests=stats.request_count,
        errors=stats.error_count,
        avg_ms=_ms(stats.avg_us),
        p95_ms=_ms(p95_us),
        p95_capped=capped,
        min_ms=_ms(stats.duration_min_us),
        max_ms=_ms(stats.duration_max_us),
        avg_success_ms=_ms(stats.avg_success_us),
        success_min_ms=_ms(stats.success_min_us),
        success_max_ms=_ms(stats.success_max_us),
        stream_count=stats.stream_count,
        stream_errors=stats.stream_error_count,
        endpoints_merged=point["endpoint_count"],
        hist_available=bool(stats.hist),
    )


_STATUS_NOTES = [
    "统计范围为单个 Web 进程；多实例部署各进程独立统计，本版不支持跨实例合并。",
    "异常退出会丢失最近未落库（最多约 15 分钟）的诊断数据；数据库长期故障可产生更长缺口。",
    "p95 为直方图桶估算（桶宽即误差上界）；最高有限桶溢出时只显示下界（≥）。",
    "无流量或样本不足不代表接口健康；取消/断连的请求不计入耗时分布。",
    "本模块不提供逐请求追踪、SQL 慢因下钻或基础设施监控。",
    "第 31–180 天的小时明细已按 720 小时保留策略过期，仅日趋势可用。",
]


@router.get("/api/platform/api-performance/status", response_model=ApiPerformanceStatusOut)
def api_performance_status(
    request: Request,
    _platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> ApiPerformanceStatusOut:
    runtime = _runtime(request)
    collector = runtime.collector
    config: ApiPerformanceConfig = runtime.config
    snapshot = collector.status_snapshot()
    try:
        coverage = query_coverage(db)
    except Exception:  # noqa: BLE001 - status must render even while the DB is degraded
        logger.warning("api performance coverage read failed", exc_info=True)
        coverage = {
            "hourly": {"earliest": None, "latest": None, "rows": 0},
            "daily": {"earliest": None, "latest": None, "rows": 0},
        }
    anomalies = runtime.detector.current_anomalies() if runtime.detector else []
    hourly_coverage = coverage["hourly"]
    daily_coverage = coverage["daily"]
    # The persisted per-day alert row is the authoritative quota state:
    # it survives restarts and must not be re-derived from the current
    # in-process anomalies (QA finding 15).
    email_status = runtime.email.status
    try:
        persisted_alert = daily_alert_status(db, day_bucket_date(datetime.now(timezone.utc)))
    except Exception:  # noqa: BLE001 - status must render while the DB is degraded
        persisted_alert = None
    if persisted_alert is not None:
        email_status = persisted_alert.status
    return ApiPerformanceStatusOut(
        enabled=config.enabled,
        generated_at=datetime.now(timezone.utc),
        total_observations=snapshot["total_observations"],
        dropped_late_observations=snapshot["dropped_late_observations"],
        dropped_pending_overflow=snapshot["dropped_pending_overflow"],
        dropped_stale_batch_observations=snapshot["dropped_stale_batch_observations"],
        in_memory_hour_buckets=snapshot["in_memory_hour_buckets"],
        in_memory_day_buckets=snapshot["in_memory_day_buckets"],
        flush=FlushStatusOut(
            last_flush_at=snapshot["last_flush_at"],
            last_flush_result=snapshot["last_flush_result"],
            consecutive_flush_errors=snapshot["consecutive_flush_errors"],
        ),
        coverage=CoverageStatusOut(
            hourly_earliest=hourly_coverage["earliest"],
            hourly_latest=hourly_coverage["latest"],
            hourly_rows=hourly_coverage["rows"],
            daily_earliest=daily_coverage["earliest"].isoformat() if daily_coverage["earliest"] else None,
            daily_latest=daily_coverage["latest"].isoformat() if daily_coverage["latest"] else None,
            daily_rows=daily_coverage["rows"],
        ),
        detection=DetectionStatusOut(
            enabled=bool(config.detection_enabled and runtime.detector is not None),
            window_minutes=config.window_minutes,
            min_samples=config.min_samples,
            p95_threshold_ms=config.p95_threshold_ms,
            sustained_minutes=config.sustained_minutes,
            stream_p95_threshold_ms=config.stream_p95_threshold_ms,
            last_evaluated_at=runtime.detector.last_evaluated_at if runtime.detector else None,
            anomaly_count=len(anomalies),
        ),
        email=EmailStatusOut(
            status=email_status,
            updated_at=runtime.email.updated_at,
            configured=bool(config.alert_email),
        ),
        retention=runtime.last_retention or {},
        notes=_STATUS_NOTES,
    )


@router.get("/api/platform/api-performance/anomalies", response_model=AnomalyListOut)
def api_performance_anomalies(
    request: Request,
    _platform_admin: PlatformAdmin = Depends(require_platform_admin),
) -> AnomalyListOut:
    runtime = _runtime(request)
    config = runtime.config
    anomalies = runtime.detector.current_anomalies() if runtime.detector else []
    return AnomalyListOut(
        generated_at=datetime.now(timezone.utc),
        window_minutes=config.window_minutes,
        detection_enabled=bool(config.detection_enabled and runtime.detector is not None),
        anomalies=[
            AnomalyOut(
                method=a.key.method,
                route=a.key.route,
                kind=a.kind,
                samples=a.samples,
                p95_ms=_ms(a.p95_us),
                p95_capped=a.p95_capped,
                threshold_ms=a.threshold_ms,
                sustained_minutes=a.sustained_minutes,
                minutes_covered=a.minutes_covered,
                first_detected_at=a.first_detected_at,
            )
            for a in anomalies
        ],
    )
