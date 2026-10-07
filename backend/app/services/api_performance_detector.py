"""GH-186 short-window slow-endpoint detection and the once-a-day email.

Detection runs on in-memory minute windows (never waits for the 15-minute
flush), every ``detection_interval_seconds``, separately for normal and
stream responses. Low-sample windows are reported as insufficient and
never as healthy. The email quota is one attempt per Beijing natural day
per deployment, persisted BEFORE sending; a provider-unknown outcome is
never retried that day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.services.api_performance_collector import (
    ApiPerformanceCollector,
    ApiPerformanceConfig,
    EndpointKey,
    WindowSample,
    day_bucket_date,
    estimate_p95_us,
)
from app.services.api_performance_store import (
    claim_daily_alert,
    daily_alert_status,
    finish_daily_alert,
)

logger = logging.getLogger(__name__)

ALERT_KIND_NORMAL = "normal"
ALERT_KIND_STREAM = "stream"

# Email outcome vocabulary shown verbatim on the page; no invented precision.
EMAIL_UNCONFIGURED = "unconfigured"
EMAIL_NOT_NEEDED = "not_needed"
EMAIL_ALREADY_CLAIMED = "already_claimed"
EMAIL_CLAIM_FAILED = "claim_failed"
EMAIL_PENDING = "pending"
EMAIL_ACCEPTED = "accepted"
EMAIL_FAILED_OR_UNKNOWN = "failed_or_unknown"


@dataclass(frozen=True)
class Anomaly:
    key: EndpointKey
    kind: str  # "normal" (success p95) | "stream" (time-to-response-start p95)
    samples: int
    p95_us: Optional[int]
    p95_capped: bool
    threshold_ms: int
    sustained_minutes: int
    minutes_covered: int
    first_detected_at: datetime


class SlowEndpointDetector:
    """Stateful streak tracking over collector minute windows. Lives one
    process lifetime: after a restart windows re-accumulate and results
    are honestly insufficient until the minimum sample count is met."""

    def __init__(self, config: ApiPerformanceConfig) -> None:
        self.config = config
        self._streaks: Dict[Tuple[EndpointKey, str], int] = {}
        self._first_met: Dict[Tuple[EndpointKey, str], datetime] = {}
        self._active: Dict[Tuple[EndpointKey, str], Anomaly] = {}
        self.last_evaluated_at: Optional[datetime] = None

    def evaluate(self, collector: ApiPerformanceCollector, *, now: Optional[datetime] = None) -> List[Anomaly]:
        now = now or datetime.now(timezone.utc)
        self.last_evaluated_at = now
        samples: List[WindowSample] = collector.window_observations(self.config.window_minutes, now=now)
        seen: set = set()
        for sample in samples:
            if sample.key.traffic_class != "business":
                continue
            for kind in (ALERT_KIND_NORMAL, ALERT_KIND_STREAM):
                outcome = self._evaluate_one(sample, kind, now)
                if outcome is not None:
                    state_key = (sample.key, kind)
                    seen.add(state_key)
                    self._active[state_key] = outcome
        for state_key in list(self._active.keys()):
            if state_key not in seen:
                del self._active[state_key]
                self._streaks.pop(state_key, None)
                self._first_met.pop(state_key, None)
        return sorted(self._active.values(), key=lambda a: (a.key.route, a.kind))

    def _evaluate_one(self, sample: WindowSample, kind: str, now: datetime) -> Optional[Anomaly]:
        config = self.config
        override = config.override_for(sample.key.route)
        if kind == ALERT_KIND_NORMAL:
            count = sample.success_count
            hist = sample.success_hist
            threshold_ms = override.p95_threshold_ms if override and override.p95_threshold_ms else config.p95_threshold_ms
            min_samples = override.min_samples if override and override.min_samples else config.min_samples
        else:
            count = sample.stream_count
            hist = sample.stream_hist
            threshold_ms = config.stream_p95_threshold_ms
            min_samples = config.stream_min_samples
        state_key = (sample.key, kind)
        # Below the minimum sample count the window says nothing: never
        # "healthy", never anomalous, streak resets.
        if count < min_samples:
            self._streaks[state_key] = 0
            self._first_met.pop(state_key, None)
            return None
        p95_us, capped = estimate_p95_us(hist)
        if p95_us is None or p95_us <= threshold_ms * 1000:
            self._streaks[state_key] = 0
            self._first_met.pop(state_key, None)
            return None
        streak = self._streaks.get(state_key, 0) + 1
        self._streaks[state_key] = streak
        first_met = self._first_met.setdefault(state_key, now)
        sustained_minutes = streak * config.detection_interval_seconds // 60
        if sustained_minutes < config.sustained_minutes:
            return None
        return Anomaly(
            key=sample.key,
            kind=kind,
            samples=count,
            p95_us=p95_us,
            p95_capped=capped,
            threshold_ms=threshold_ms,
            sustained_minutes=sustained_minutes,
            minutes_covered=sample.minutes_covered,
            first_detected_at=first_met,
        )

    def current_anomalies(self) -> List[Anomaly]:
        return sorted(self._active.values(), key=lambda a: (a.key.route, a.kind))


def render_alert_email(
    anomalies: List[Anomaly],
    *,
    config: ApiPerformanceConfig,
    alert_day: date,
) -> Tuple[str, str]:
    """Fixed-format zh alert. Contains only endpoint identifiers (method +
    route template), window/sample/duration numbers and the admin entry --
    never bodies, query strings, tokens or business content."""
    subject = f"接口性能提醒：{len(anomalies)} 个接口持续变慢"
    lines = [
        f"接口性能提醒（{alert_day.isoformat()}，Asia/Shanghai）",
        "",
        f"最近 {config.window_minutes} 分钟窗口内，以下接口的成功响应 p95 耗时持续超过阈值"
        f"（连续约 {config.sustained_minutes} 分钟）：",
        "",
    ]
    for anomaly in anomalies:
        metric = "流式响应开始耗时" if anomaly.kind == ALERT_KIND_STREAM else "成功响应 p95"
        p95_ms = ">= %d" % (anomaly.p95_us // 1000) if anomaly.p95_capped else str((anomaly.p95_us or 0) // 1000)
        lines.append(
            f"- [{anomaly.key.method}] {anomaly.key.route} · {metric}: {p95_ms} ms"
            f"（阈值 {anomaly.threshold_ms} ms，样本 {anomaly.samples}，"
            f"持续约 {anomaly.sustained_minutes} 分钟）"
        )
    lines += [
        "",
        f"判定参数（工程默认，可配置）：窗口 {config.window_minutes} 分钟、"
        f"最低样本 {config.min_samples}、评估间隔 {config.detection_interval_seconds} 秒。",
        "本邮件为当天首封合并提醒；之后新的异常请登录平台控制台查看，当天不会重复发送。",
        "Provider 接受不代表实际送达。",
    ]
    if config.admin_url:
        lines += ["", f"超管入口：{config.admin_url}"]
    return subject, "\n".join(lines)


def maybe_send_daily_alert(
    db,
    anomalies: List[Anomaly],
    *,
    config: ApiPerformanceConfig,
    collector: ApiPerformanceCollector,
    now: datetime,
    send_fn,
) -> str:
    """Enforce the once-a-day quota with persist-before-send semantics.

    Outcome vocabulary (displayed verbatim on the status page):
    unconfigured / not_needed / already_claimed / claim_failed /
    accepted / failed_or_unknown. A failed intent persistence means NO
    send (fail closed); a provider-unknown outcome is never retried
    today. ``send_fn`` must be a blocking, single-attempt transactional
    email call (app.email boundary) -- the caller runs it off-loop."""
    if not config.alert_email:
        return EMAIL_UNCONFIGURED
    if not anomalies:
        return EMAIL_NOT_NEEDED
    alert_day = day_bucket_date(now)
    try:
        existing = daily_alert_status(db, alert_day)
        if existing is not None:
            return EMAIL_ALREADY_CLAIMED
        intent = claim_daily_alert(db, alert_date=alert_day, instance_id=collector.instance_id, anomaly_count=len(anomalies))
    except Exception:  # noqa: BLE001 - quota persistence failure forbids sending
        logger.warning("api performance alert intent persistence failed", exc_info=True)
        return EMAIL_CLAIM_FAILED
    if intent is None:
        # Intent already claimed by a concurrent evaluation: sending is
        # forbidden either way.
        return EMAIL_CLAIM_FAILED
    subject, body = render_alert_email(anomalies, config=config, alert_day=alert_day)
    operation_id = f"api-perf-alert/{alert_day.isoformat()}"
    try:
        delivered = bool(send_fn(config.alert_email, subject, body, operation_id=operation_id))
    except Exception:  # noqa: BLE001 - provider failures must not kill the loop
        logger.warning("api performance alert send raised", exc_info=True)
        delivered = False
    # The email.py bool cannot distinguish rejected vs unknown: the
    # conservative outcome records one attempt, never retried today.
    finish_daily_alert(db, intent.id, EMAIL_ACCEPTED if delivered else EMAIL_FAILED_OR_UNKNOWN)
    return EMAIL_ACCEPTED if delivered else EMAIL_FAILED_OR_UNKNOWN
