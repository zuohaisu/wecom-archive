"""GH-186 platform API-performance response contracts.

Aggregate-only: no request identifiers, no user/tenant attribution, no
payload content. Empty optional numeric fields mean "no observations" --
zero is never fabricated for min/max/avg/p95.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel


class EndpointStatsOut(BaseModel):
    requests: int = 0
    errors: int = 0
    error_rate: Optional[float] = None
    cancelled: int = 0
    avg_ms: Optional[float] = None
    min_ms: Optional[float] = None
    max_ms: Optional[float] = None
    avg_success_ms: Optional[float] = None
    success_min_ms: Optional[float] = None
    success_max_ms: Optional[float] = None
    p95_ms: Optional[float] = None
    p95_capped: bool = False
    p95_basis: str = "success"  # p95 is estimated over SUCCESS durations
    class_counts: Dict[str, int] = {}
    stream_count: int = 0
    stream_errors: int = 0
    stream_avg_duration_ms: Optional[float] = None
    stream_avg_start_ms: Optional[float] = None
    hist_available: bool = False


class EndpointSummaryOut(BaseModel):
    method: str
    route: str
    traffic_class: str
    registered: bool  # in the current route table (False = retired, history kept)
    has_samples: bool
    stats: EndpointStatsOut


class EndpointListOut(BaseModel):
    window_hours: int
    total: int
    page: int
    page_size: int
    endpoints: List[EndpointSummaryOut]
    site: EndpointStatsOut
    incomplete: bool = False  # query hit the bounded row cap; aggregates may be partial


class SeriesPointOut(BaseModel):
    # Hourly buckets: UTC ISO instant of the Asia/Shanghai hour start.
    # Daily buckets: the Asia/Shanghai calendar day (YYYY-MM-DD).
    bucket: str
    requests: int = 0
    errors: int = 0
    avg_ms: Optional[float] = None
    p95_ms: Optional[float] = None
    p95_capped: bool = False
    min_ms: Optional[float] = None
    max_ms: Optional[float] = None
    avg_success_ms: Optional[float] = None
    success_min_ms: Optional[float] = None
    success_max_ms: Optional[float] = None
    stream_count: int = 0
    stream_errors: int = 0
    endpoints_merged: int = 0
    hist_available: bool = False


class SeriesOut(BaseModel):
    granularity: str
    method: Optional[str] = None
    route: Optional[str] = None
    site_wide: bool = False
    timezone: str = "Asia/Shanghai"
    points: List[SeriesPointOut]
    incomplete: bool = False  # query hit the bounded row cap; aggregates may be partial


class AnomalyOut(BaseModel):
    method: str
    route: str
    kind: str  # "normal" | "stream"
    samples: int
    p95_ms: Optional[float] = None
    p95_capped: bool = False
    threshold_ms: int
    sustained_minutes: int
    minutes_covered: int
    first_detected_at: datetime


class AnomalyListOut(BaseModel):
    generated_at: datetime
    window_minutes: int
    detection_enabled: bool
    anomalies: List[AnomalyOut]


class FlushStatusOut(BaseModel):
    last_flush_at: Optional[datetime] = None
    last_flush_result: str = "never"
    consecutive_flush_errors: int = 0
    refresh_delay_note: str = "当前小时/当天展示的是已落库值，最多落后约一个落库周期"


class CoverageStatusOut(BaseModel):
    hourly_earliest: Optional[datetime] = None
    hourly_latest: Optional[datetime] = None
    hourly_rows: int = 0
    daily_earliest: Optional[str] = None
    daily_latest: Optional[str] = None
    daily_rows: int = 0


class DetectionStatusOut(BaseModel):
    enabled: bool
    window_minutes: int
    min_samples: int
    p95_threshold_ms: int
    sustained_minutes: int
    stream_p95_threshold_ms: int
    last_evaluated_at: Optional[datetime] = None
    anomaly_count: int = 0
    windows_restarted_note: str = "进程重启后短窗口重新积累，样本不足期间显示“样本不足”，不视为健康"


class EmailStatusOut(BaseModel):
    status: str
    updated_at: Optional[datetime] = None
    configured: bool
    note: str = "Provider 接受不代表实际送达；一天最多一封，未知结果当天不重试"


class ApiPerformanceStatusOut(BaseModel):
    enabled: bool
    timezone: str = "Asia/Shanghai"
    generated_at: datetime
    total_observations: int = 0
    dropped_late_observations: int = 0
    dropped_pending_overflow: int = 0
    in_memory_hour_buckets: int = 0
    in_memory_day_buckets: int = 0
    flush: FlushStatusOut
    coverage: CoverageStatusOut
    detection: DetectionStatusOut
    email: EmailStatusOut
    retention: dict = {}
    notes: List[str] = []
