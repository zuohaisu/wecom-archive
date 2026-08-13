"""Server-authoritative monthly export limits for RND-393.

The tenant row is the serialization lock.  PostgreSQL therefore admits at
most one quota consumer for a tenant at a time, including the first request
of a new month when no counter row exists yet.  Browser state is never used
for authorization.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExportMonthlyUsage, Tenant


EXPORT_LIMITS: dict[str, int] = {"text": 10, "media_zip": 1}
_BILLING_TIMEZONE = ZoneInfo("Asia/Shanghai")


class ExportQuotaError(ValueError):
    """Base class for rejected quota operations."""


class ExportQuotaExceeded(ExportQuotaError):
    def __init__(self, bucket: "ExportQuotaBucket") -> None:
        super().__init__("monthly export quota exceeded")
        self.bucket = bucket


class ExportQuotaTenantNotFound(ExportQuotaError):
    pass


@dataclass(frozen=True)
class ExportQuotaBucket:
    export_type: str
    limit: int
    used: int
    remaining: int
    period_start: date
    resets_at: datetime


@dataclass(frozen=True)
class ExportQuotaSummary:
    period_start: date
    resets_at: datetime
    text: ExportQuotaBucket
    media_zip: ExportQuotaBucket


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _month_window(at: datetime | None = None) -> tuple[date, datetime]:
    local = _as_utc(at).astimezone(_BILLING_TIMEZONE)
    start = date(local.year, local.month, 1)
    if local.month == 12:
        reset_local = datetime(local.year + 1, 1, 1, tzinfo=_BILLING_TIMEZONE)
    else:
        reset_local = datetime(local.year, local.month + 1, 1, tzinfo=_BILLING_TIMEZONE)
    return start, reset_local


def _bucket(
    export_type: str,
    used: int,
    period_start: date,
    resets_at: datetime,
) -> ExportQuotaBucket:
    limit = EXPORT_LIMITS[export_type]
    normalized_used = max(0, int(used))
    return ExportQuotaBucket(
        export_type=export_type,
        limit=limit,
        used=normalized_used,
        remaining=max(0, limit - normalized_used),
        period_start=period_start,
        resets_at=resets_at,
    )


def get_export_quota_summary(
    db: Session,
    tenant_id: str,
    *,
    at: datetime | None = None,
) -> ExportQuotaSummary:
    period_start, resets_at = _month_window(at)
    rows = db.scalars(
        select(ExportMonthlyUsage).where(
            ExportMonthlyUsage.tenant_id == tenant_id,
            ExportMonthlyUsage.period_start == period_start,
            ExportMonthlyUsage.export_type.in_(tuple(EXPORT_LIMITS)),
        )
    ).all()
    used_by_type = {row.export_type: row.used_count for row in rows}
    return ExportQuotaSummary(
        period_start=period_start,
        resets_at=resets_at,
        text=_bucket("text", used_by_type.get("text", 0), period_start, resets_at),
        media_zip=_bucket(
            "media_zip",
            used_by_type.get("media_zip", 0),
            period_start,
            resets_at,
        ),
    )


def consume_export_quota(
    db: Session,
    tenant_id: str,
    export_type: str,
    *,
    at: datetime | None = None,
) -> ExportQuotaBucket:
    """Atomically consume one monthly allowance inside the caller transaction."""
    if export_type not in EXPORT_LIMITS:
        raise ExportQuotaError("unsupported export type")

    # This stable parent-row lock avoids the classic first-write race where
    # two requests both observe that the monthly counter does not exist.
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    if tenant is None:
        raise ExportQuotaTenantNotFound("tenant does not exist")

    period_start, resets_at = _month_window(at)
    usage = db.scalar(
        select(ExportMonthlyUsage).where(
            ExportMonthlyUsage.tenant_id == tenant_id,
            ExportMonthlyUsage.period_start == period_start,
            ExportMonthlyUsage.export_type == export_type,
        )
    )
    used = int(usage.used_count) if usage is not None else 0
    current = _bucket(export_type, used, period_start, resets_at)
    if current.remaining <= 0:
        raise ExportQuotaExceeded(current)

    if usage is None:
        usage = ExportMonthlyUsage(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            period_start=period_start,
            export_type=export_type,
            used_count=1,
        )
        db.add(usage)
    else:
        usage.used_count = used + 1
    db.flush()
    return _bucket(export_type, used + 1, period_start, resets_at)
