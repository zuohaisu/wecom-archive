"""Tenant-scoped aggregate queries for the admin usage analytics page."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, MediaFile
from app.message_type_registry import describe_message_type

# A deliberately visible estimate for database/text records not represented by
# attachment rows. It is not measured storage and must remain labelled as such.
ESTIMATED_TEXT_BYTES = 512
_PERIODS = frozenset((7, 30, 90))


def _bounds(days: int) -> tuple[datetime, datetime, datetime]:
    if days not in _PERIODS:
        raise ValueError("days must be one of 7, 30, or 90")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start, end, start - timedelta(days=days)


def _message_timestamp():
    return func.to_timestamp(ArchiveMessage.msgtime / 1000.0)


def trend(db: Session, tenant_id: str, days: int = 30) -> dict:
    """Daily current and preceding-period message counts, with no row data."""
    start, end, previous_start = _bounds(days)
    timestamp = _message_timestamp()
    stmt = (
        select(func.date_trunc("day", timestamp).label("day"), func.count().label("count"))
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
            timestamp >= previous_start,
            timestamp < end,
        )
        .group_by(func.date_trunc("day", timestamp))
        .order_by(func.date_trunc("day", timestamp))
    )
    current, previous = [], []
    for day, count in db.execute(stmt):
        point = {"date": day.date().isoformat(), "count": int(count)}
        (current if day >= start else previous).append(point)
    return {"current": current, "previous": previous}


def type_composition(db: Session, tenant_id: str, days: int = 30) -> list[dict]:
    """Map raw types through the central registry into stable UI categories."""
    start, end, _ = _bounds(days)
    timestamp = _message_timestamp()
    stmt = (
        select(ArchiveMessage.msgtype, func.count())
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
            timestamp >= start,
            timestamp < end,
        )
        .group_by(ArchiveMessage.msgtype)
    )
    categories = {name: 0 for name in ("text", "image", "file", "voice", "video", "structured", "other")}
    for msgtype, count in db.execute(stmt):
        detail = describe_message_type(msgtype)
        normalized = detail["normalized_type"]
        if normalized in ("text", "image", "file", "voice", "video"):
            category = normalized
        elif detail["category"] in ("structured", "interactive", "composite", "system", "control"):
            category = "structured"
        else:
            category = "other"
        categories[category] += int(count)
    total = sum(categories.values())
    return [
        {"category": category, "count": count, "percent": (count / total if total else 0.0)}
        for category, count in categories.items()
    ]


def storage_composition(db: Session, tenant_id: str) -> list[dict]:
    """Attachment bytes plus a labelled estimate for text/index records."""
    media_category = case(
        (MediaFile.file_type.in_(("image",)), "image"),
        (MediaFile.file_type.in_(("voice", "audio", "audio_archive")), "voice"),
        (MediaFile.file_type.in_(("video",)), "video"),
        else_="file",
    )
    media_stmt = (
        select(media_category.label("category"), func.coalesce(func.sum(MediaFile.file_size), 0))
        .join(ArchiveMessage, MediaFile.archive_message_id == ArchiveMessage.id)
        .where(ArchiveMessage.tenant_id == tenant_id)
        .group_by(media_category)
    )
    values = {name: 0 for name in ("image", "file", "voice", "video")}
    for category, size in db.execute(media_stmt):
        values[category] = int(size or 0)
    text_count = db.execute(
        select(func.count()).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtype == "text",
        )
    ).scalar() or 0
    values["text_and_index_estimate"] = int(text_count) * ESTIMATED_TEXT_BYTES
    total = sum(values.values())
    return [
        {
            "category": category,
            "bytes": size,
            "gigabytes": size / (1024 ** 3),
            "percent": (size / total if total else 0.0),
            "estimated": category == "text_and_index_estimate",
        }
        for category, size in values.items()
    ]


def hourly_distribution(db: Session, tenant_id: str, days: int = 30) -> list[dict]:
    """Exactly 24 UTC hour buckets for messages in the requested period."""
    start, end, _ = _bounds(days)
    timestamp = _message_timestamp()
    hour = func.extract("hour", timestamp)
    stmt = (
        select(hour.label("hour"), func.count())
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
            timestamp >= start,
            timestamp < end,
        )
        .group_by(hour)
    )
    counts = {int(hour): int(count) for hour, count in db.execute(stmt)}
    return [{"hour": hour, "count": counts.get(hour, 0)} for hour in range(24)]
