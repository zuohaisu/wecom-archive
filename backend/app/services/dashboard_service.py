"""Tenant-scoped dashboard-only window aggregates (RND-282)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import conversation_membership
from app.db import models
from app.db.models import AdminUser, ArchiveMessage, ArchiveMessageRecipient, TenantWecomConfig
from app.message_type_registry import MessageCategory, MESSAGE_TYPE_DEFINITIONS
from app.schemas.dashboard import ActivityItem, DashboardOut, DayBucket
from app.services.analytics_service import hourly_distribution, storage_composition, type_composition
from app.services.usageservice import (
    count_archived_members,
    count_reviewable_messages,
    get_archived_days,
    sum_downloaded_storage,
    sync_health,
)

MS_PER_DAY = 86_400_000
_BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000
_BEIJING = timezone(timedelta(hours=8))
MEDIA_MSGTYPES = frozenset(
    value
    for definition in MESSAGE_TYPE_DEFINITIONS
    if definition.category is MessageCategory.MEDIA
    for value in (definition.raw_type, *definition.aliases)
)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _window_bounds(range_days: int, now_ms: int) -> tuple[int, int, list[str]]:
    """Return the current Beijing calendar-day window and its epoch-ms bounds."""
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(_BEIJING)
    today = now.date()
    dates = [(today - timedelta(days=offset)).isoformat() for offset in range(range_days - 1, -1, -1)]
    start = datetime.combine(today - timedelta(days=range_days - 1), datetime.min.time(), tzinfo=_BEIJING)
    return int(start.timestamp() * 1000), now_ms, dates


def daily_message_series(
    db: Session, tenant_id: str, from_ms: int, to_ms: int, dates: list[str]
) -> List[DayBucket]:
    """Return zero-filled Beijing-day message buckets without selecting bodies."""
    beijing_day = cast(
        func.floor((ArchiveMessage.msgtime + _BEIJING_OFFSET_MS) / MS_PER_DAY), Integer
    )
    media_count = func.sum(cast(ArchiveMessage.msgtype.in_(MEDIA_MSGTYPES), Integer))
    stmt = (
        select(beijing_day.label("day"), func.count(ArchiveMessage.id), media_count)
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtime >= from_ms,
            ArchiveMessage.msgtime <= to_ms,
        )
        .group_by(beijing_day)
    )
    counts: dict[str, tuple[int, int]] = {}
    for day, total, media in db.execute(stmt):
        date = datetime.fromtimestamp((int(day) * MS_PER_DAY - _BEIJING_OFFSET_MS) / 1000, tz=timezone.utc).astimezone(_BEIJING).date().isoformat()
        counts[date] = (int(total), int(media or 0))
    return [
        DayBucket(date=date, text_count=counts.get(date, (0, 0))[0] - counts.get(date, (0, 0))[1], media_count=counts.get(date, (0, 0))[1])
        for date in dates
    ]


def count_silent_staff(db: Session, tenant_id: str, since_ms: int) -> int:
    """Count monitored staff with neither sent nor received messages in 30 days."""
    staff_ids = conversation_membership._collect_staff_ids(db, tenant_id)
    if not staff_ids:
        return 0
    senders = db.execute(
        select(ArchiveMessage.sender).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtime >= since_ms,
            ArchiveMessage.sender.isnot(None),
        ).distinct()
    ).scalars()
    recipients = db.execute(
        select(ArchiveMessageRecipient.receiver_userid)
        .join(ArchiveMessage, ArchiveMessage.id == ArchiveMessageRecipient.message_id)
        .where(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtime >= since_ms,
        )
        .distinct()
    ).scalars()
    return len(staff_ids - set(senders) - set(recipients))


def _activity_badge(action: str) -> str:
    action = action.lower()
    if "export" in action:
        return "export"
    if "view" in action:
        return "view"
    if "config" in action:
        return "config"
    if "fail" in action or "error" in action:
        return "fail"
    if "sync" in action:
        return "sync"
    return "view"


def recent_activity(db: Session, tenant_id: str, limit: int = 20) -> List[ActivityItem]:
    """Read A7 events when available; tolerate deployments without its table."""
    AuditLog = getattr(models, "AuditLog", None)
    if AuditLog is None:
        return []
    try:
        with db.begin_nested():
            rows = db.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant_id)
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            ).scalars().all()
            actor_ids = [row.admin_user_id for row in rows if row.admin_user_id]
            names = {}
            if actor_ids:
                names = dict(db.execute(
                    select(AdminUser.id, AdminUser.name).where(
                        AdminUser.tenant_id == tenant_id, AdminUser.id.in_(actor_ids)
                    )
                ).all())
            return [
                ActivityItem(
                    id=f"audit#{row.id}",
                    badge=_activity_badge(row.action),
                    actor=names.get(row.admin_user_id) or "system",
                    description=f"{row.action}: {row.object_type}",
                    scope=row.object_id,
                    ip=(row.detail or {}).get("ip") if isinstance(row.detail, dict) else None,
                    time=row.created_at.isoformat() if row.created_at else "",
                )
                for row in rows
            ]
    except SQLAlchemyError:
        return []


def _archive_status(
    db: Session, tenant_id: str, *, total_archived_messages: int, sync_status: str
) -> tuple[str, bool]:
    """Return a conservative product status from persisted tenant state only."""
    config = db.execute(
        select(TenantWecomConfig.is_active).where(TenantWecomConfig.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if config is None:
        return "not_configured", False
    if not config:
        return "configuration_error", False
    if sync_status == "error":
        return "needs_attention", True
    if sync_status == "syncing":
        return "processing", True
    if sync_status == "idle" and total_archived_messages:
        return "normal", True
    if sync_status == "idle":
        return "not_started", True
    # A configured tenant with historic records but no persisted worker state
    # cannot safely be called healthy.
    if total_archived_messages:
        return "needs_attention", True
    return "not_started", True


def _archive_bounds(db: Session, tenant_id: str) -> tuple[str | None, str | None]:
    first_ms, last_ms = db.execute(
        select(func.min(ArchiveMessage.msgtime), func.max(ArchiveMessage.msgtime)).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtime.isnot(None),
        )
    ).one()

    def _as_iso(value: int | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()

    return _as_iso(first_ms), _as_iso(last_ms)


def _optional_insights(
    db: Session, tenant_id: str, range_days: int
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Load independent insights without turning one failed chart into a failed overview."""
    loaders = {
        "type_composition": lambda: type_composition(db, tenant_id, range_days),
        "storage_composition": lambda: storage_composition(db, tenant_id),
        "hourly_distribution": lambda: hourly_distribution(db, tenant_id, range_days),
    }
    values: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for name, loader in loaders.items():
        try:
            values[name] = loader()
        except SQLAlchemyError:
            # Never expose database details or interrupt the tenant's status and
            # summary cards. The UI supplies a scoped retry for this module.
            values[name] = []
            errors[name] = "unavailable"
    return values, errors


def build_dashboard(
    db: Session, tenant_id: str, range_days: int, *, can_manage_settings: bool = False
) -> DashboardOut:
    now_ms = _now_ms()
    from_ms, to_ms, dates = _window_bounds(range_days, now_ms)
    series = daily_message_series(db, tenant_id, from_ms, to_ms, dates)
    total = sum(bucket.text_count + bucket.media_count for bucket in series)
    health = sync_health(db, tenant_id)
    status = health["status"]
    total_archived_messages = count_reviewable_messages(db, tenant_id)
    archive_status, archive_configured = _archive_status(
        db, tenant_id, total_archived_messages=total_archived_messages, sync_status=status
    )
    first_archived_at, last_archived_at = _archive_bounds(db, tenant_id)
    insights, insight_errors = _optional_insights(db, tenant_id, range_days)
    return DashboardOut(
        range_days=range_days,
        days=sum(1 for bucket in series if bucket.text_count + bucket.media_count),
        total_messages=total,
        storage_bytes=sum_downloaded_storage(db, tenant_id),
        staff_count=count_archived_members(db, tenant_id),
        silent_staff_30d=count_silent_staff(db, tenant_id, now_ms - 30 * MS_PER_DAY),
        sync_status=status,
        sync_healthy=status != "error",
        daily_series=series,
        recent_activity=recent_activity(db, tenant_id),
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_archived_messages=total_archived_messages,
        archive_coverage_days=get_archived_days(db, tenant_id),
        first_archived_at=first_archived_at,
        last_archived_at=last_archived_at,
        archive_status=archive_status,
        sync_updated_at=health["updated_at"],
        archive_configured=archive_configured,
        can_manage_settings=can_manage_settings,
        type_composition=insights["type_composition"],
        storage_composition=insights["storage_composition"],
        hourly_distribution=insights["hourly_distribution"],
        insight_errors=insight_errors,
    )
