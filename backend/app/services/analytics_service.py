"""Tenant-scoped aggregate queries for the admin usage analytics page."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, MediaFile
from app.message_type_registry import describe_message_type

# A deliberately visible estimate for database/text records not represented by
# attachment rows. It is not measured storage and must remain labelled as such.
ESTIMATED_TEXT_BYTES = 512
_PERIODS = frozenset((7, 14, 30, 90))
_MS_PER_DAY = 86_400_000
_MS_PER_HOUR = 3_600_000
_BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000


def _bounds(days: int) -> tuple[datetime, datetime, datetime]:
    if days not in _PERIODS:
        raise ValueError("days must be one of 7, 14, 30, or 90")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start, end, start - timedelta(days=days)


def _epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _beijing_day_bucket():
    return cast(
        func.floor((ArchiveMessage.msgtime + _BEIJING_OFFSET_MS) / _MS_PER_DAY),
        Integer,
    )


def trend(db: Session, tenant_id: str, days: int = 30) -> dict:
    """Daily current and preceding-period message counts, with no row data."""
    start, end, previous_start = _bounds(days)
    start_ms, end_ms, previous_start_ms = map(_epoch_ms, (start, end, previous_start))
    day_bucket = _beijing_day_bucket()
    current_start_day = int((start_ms + _BEIJING_OFFSET_MS) // _MS_PER_DAY)
    stmt = (
        select(day_bucket.label("day"), func.count().label("count"))
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtime >= previous_start_ms,
            ArchiveMessage.msgtime < end_ms,
        )
        .group_by(day_bucket)
        .order_by(day_bucket)
    )
    current, previous = [], []
    for day, count in db.execute(stmt):
        point = {
            "date": datetime.fromtimestamp(
                (int(day) * _MS_PER_DAY - _BEIJING_OFFSET_MS) / 1000,
                tz=timezone.utc,
            ).date().isoformat(),
            "count": int(count),
        }
        (current if int(day) >= current_start_day else previous).append(point)
    return {"current": current, "previous": previous}


def type_composition(db: Session, tenant_id: str, days: int = 30) -> list[dict]:
    """Map raw types through the central registry into stable UI categories."""
    start, end, _ = _bounds(days)
    start_ms, end_ms = map(_epoch_ms, (start, end))
    stmt = (
        select(ArchiveMessage.msgtype, func.count())
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtime >= start_ms,
            ArchiveMessage.msgtime < end_ms,
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
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.decrypt_status == "success",
            MediaFile.tenant_id == tenant_id,
            MediaFile.download_status == "downloaded",
        )
        .group_by(media_category)
    )
    values = {name: 0 for name in ("image", "file", "voice", "video")}
    for category, size in db.execute(media_stmt):
        values[category] = int(size or 0)
    text_count = db.execute(
        select(func.count()).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtype == "text",
            ArchiveMessage.decrypt_status == "success",
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
    """Exactly 24 Beijing-hour buckets for messages in the requested period."""
    start, end, _ = _bounds(days)
    start_ms, end_ms = map(_epoch_ms, (start, end))
    hour = cast(
        func.floor(
            ((ArchiveMessage.msgtime + _BEIJING_OFFSET_MS) % _MS_PER_DAY) / _MS_PER_HOUR
        ),
        Integer,
    )
    stmt = (
        select(hour.label("hour"), func.count())
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtime >= start_ms,
            ArchiveMessage.msgtime < end_ms,
        )
        .group_by(hour)
    )
    counts = {int(hour): int(count) for hour, count in db.execute(stmt)}
    return [{"hour": hour, "count": counts.get(hour, 0)} for hour in range(24)]
