"""Tenant storage-usage trend and depletion forecast (RND-163).

Single source of truth for the "how fast are we growing / when will we run
out" estimates shown on the capacity surface. It reads the same authoritative
``tenant_storage_daily`` rollup that billing measures (RND-331/RND-385), so
the trend, the live capacity numbers and the plan-quota judgement all agree.
Forecasts are explicitly estimates, never billing facts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TenantStorageDaily

SUPPORTED_RANGES = frozenset({7, 30, 90})
MIN_POINTS_FOR_ESTIMATE = 2
MIN_SPAN_DAYS_FOR_ESTIMATE = 2


@dataclass(frozen=True)
class TrendPoint:
    date: str
    bytes: int


@dataclass(frozen=True)
class StorageTrend:
    range_days: int
    series: list[TrendPoint]
    measured_points: int
    avg_daily_growth_bytes: Optional[int]
    days_until_full: Optional[int]
    estimate_available: bool
    quota_bytes: int
    used_bytes: Optional[int]
    measured_at: Optional[datetime]


def project_days_until_full(
    used_bytes: Optional[int], quota_bytes: int, daily_growth_bytes: Optional[int]
) -> Optional[int]:
    """Pure estimate: days until the quota is reached at current growth.

    Returns None when the estimate is not meaningful (no usage, no growth,
    negative growth, or no positive quota). Zero when already at/over quota.
    """
    if used_bytes is None or daily_growth_bytes is None or daily_growth_bytes <= 0:
        return None
    if quota_bytes <= 0:
        return None
    if used_bytes >= quota_bytes:
        return 0
    return int(math.ceil((quota_bytes - used_bytes) / daily_growth_bytes))


def compute_daily_growth(first: TrendPoint, last: TrendPoint, span_days: int) -> Optional[int]:
    """Average per-day byte growth across the measured span, if meaningful."""
    if span_days < MIN_SPAN_DAYS_FOR_ESTIMATE:
        return None
    delta = last.bytes - first.bytes
    if delta <= 0:
        return None
    return int(round(delta / span_days))


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def storage_trend(
    db: Session,
    tenant_id: str,
    *,
    range_days: int = 30,
    quota_bytes: int,
    used_bytes: Optional[int],
    measured_at: Optional[datetime],
    at: Optional[datetime] = None,
) -> StorageTrend:
    """Build the trend series + depletion estimate from the daily rollup."""
    if range_days not in SUPPORTED_RANGES:
        range_days = 30
    today = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    start_date = today - timedelta(days=range_days - 1)
    rows = db.scalars(
        select(TenantStorageDaily)
        .where(
            TenantStorageDaily.tenant_id == tenant_id,
            TenantStorageDaily.usage_date >= start_date,
            TenantStorageDaily.usage_date <= today,
        )
        .order_by(TenantStorageDaily.usage_date.asc())
    ).all()
    series = [
        TrendPoint(date=row.usage_date.isoformat(), bytes=int(row.used_bytes))
        for row in rows
    ]
    measured_points = len(series)

    growth: Optional[int] = None
    if measured_points >= MIN_POINTS_FOR_ESTIMATE:
        span_days = max(
            (datetime.strptime(series[-1].date, "%Y-%m-%d").date()
             - datetime.strptime(series[0].date, "%Y-%m-%d").date()).days,
            MIN_SPAN_DAYS_FOR_ESTIMATE,
        )
        growth = compute_daily_growth(series[0], series[-1], span_days)

    days_until_full = project_days_until_full(used_bytes, quota_bytes, growth)
    return StorageTrend(
        range_days=range_days,
        series=series,
        measured_points=measured_points,
        avg_daily_growth_bytes=growth,
        days_until_full=days_until_full,
        estimate_available=days_until_full is not None,
        quota_bytes=quota_bytes,
        used_bytes=used_bytes,
        measured_at=measured_at,
    )
