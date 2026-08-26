"""Tenant-scoped bulk message cleanup with impact preview (RND-370).

Every filter is built server-side from a strict whitelist — never client
SQL or arbitrary expressions. A cleanup task only soft-deletes matched
messages into the recycle bin (RND-362 tombstone); it never permanently
deletes, and it is gated by the tenant compliance hold. The background
worker (scripts/process_message_cleanup_once.py) executes tasks page by
page so a single HTTP request never processes thousands of rows.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.db.models import (
    ArchiveMessage,
    ArchiveMessageRecipient,
    MediaFile,
    MessageCleanupPreview,
    MessageCleanupTask,
    Tenant,
)
from app.message_type_registry import MESSAGE_TYPE_DEFINITIONS

logger = logging.getLogger(__name__)

PRESET_OLDER_DAYS = frozenset({30, 90, 180, 365})
MAX_TASK_PAGE = 500
PREVIEW_DRIFT_TOLERANCE = 0.10
PREVIEW_TTL = timedelta(minutes=30)
ESTIMATED_TEXT_BYTES = 512
CONFIRMATION_PHRASE = "确认清理"
CLEANUP_TASK_STATES = frozenset({"queued", "running", "partial", "completed", "failed", "canceled"})


class CleanupError(RuntimeError):
    pass


class CleanupConfirmationRequired(CleanupError):
    pass


class CleanupPreviewStale(CleanupError):
    pass


class CleanupPreviewExpired(CleanupError):
    pass


class CleanupLocked(CleanupError):
    pass


class CleanupTaskNotFound(CleanupError):
    pass


@dataclass(frozen=True)
class CleanupFilter:
    """Server-validated filter; every field optional and AND-combined."""

    date_from_ms: Optional[int] = None
    date_to_ms: Optional[int] = None
    older_than_days: Optional[int] = None
    roomid: Optional[str] = None
    staff_id: Optional[str] = None
    contact_id: Optional[str] = None
    conversation_kind: Optional[str] = None  # all | single | group
    msgtypes: tuple[str, ...] = ()
    has_media: Optional[bool] = None
    media_min_bytes: Optional[int] = None
    media_max_bytes: Optional[int] = None
    include_favorited: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "date_from_ms": self.date_from_ms,
            "date_to_ms": self.date_to_ms,
            "older_than_days": self.older_than_days,
            "roomid": self.roomid,
            "staff_id": self.staff_id,
            "contact_id": self.contact_id,
            "conversation_kind": self.conversation_kind,
            "msgtypes": list(self.msgtypes),
            "has_media": self.has_media,
            "media_min_bytes": self.media_min_bytes,
            "media_max_bytes": self.media_max_bytes,
            "include_favorited": self.include_favorited,
        }


_KNOWN_MSGTYPES = {definition.raw_type for definition in MESSAGE_TYPE_DEFINITIONS}
for _definition in MESSAGE_TYPE_DEFINITIONS:
    _KNOWN_MSGTYPES.update(_definition.aliases)


def validate_filter(raw: dict[str, Any]) -> CleanupFilter:
    """Validate and normalize a client-supplied filter dict (strict whitelist)."""
    if not isinstance(raw, dict):
        raise CleanupError("invalid_filter")

    def _int(key: str) -> Optional[int]:
        value = raw.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise CleanupError(f"invalid_filter:{key}")

    older_than_days = _int("older_than_days")
    if older_than_days is not None and older_than_days not in PRESET_OLDER_DAYS:
        raise CleanupError("invalid_filter:older_than_days")

    conversation_kind = raw.get("conversation_kind")
    if conversation_kind not in (None, "all", "single", "group"):
        raise CleanupError("invalid_filter:conversation_kind")

    msgtypes_raw = raw.get("msgtypes") or []
    if not isinstance(msgtypes_raw, list):
        raise CleanupError("invalid_filter:msgtypes")
    msgtypes: list[str] = []
    for value in msgtypes_raw:
        if not isinstance(value, str) or value not in _KNOWN_MSGTYPES:
            raise CleanupError(f"invalid_filter:msgtype:{value}")
        msgtypes.append(value)

    has_media = raw.get("has_media")
    if has_media is not None and not isinstance(has_media, bool):
        raise CleanupError("invalid_filter:has_media")

    roomid = raw.get("roomid")
    staff_id = raw.get("staff_id")
    contact_id = raw.get("contact_id")
    for key, value in (("roomid", roomid), ("staff_id", staff_id), ("contact_id", contact_id)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise CleanupError(f"invalid_filter:{key}")

    return CleanupFilter(
        date_from_ms=_int("date_from_ms"),
        date_to_ms=_int("date_to_ms"),
        older_than_days=older_than_days,
        roomid=roomid.strip() if roomid else None,
        staff_id=staff_id.strip() if staff_id else None,
        contact_id=contact_id.strip() if contact_id else None,
        conversation_kind=conversation_kind,
        msgtypes=tuple(msgtypes),
        has_media=has_media,
        media_min_bytes=_int("media_min_bytes"),
        media_max_bytes=_int("media_max_bytes"),
        include_favorited=bool(raw.get("include_favorited", False)),
    )


def filter_summary(f: CleanupFilter) -> str:
    """Natural-language summary of the filter, for confirmation + audit."""
    parts: list[str] = []
    if f.older_than_days is not None:
        parts.append(f"早于 {f.older_than_days} 天")
    else:
        if f.date_from_ms is not None:
            parts.append(f"从 {datetime.fromtimestamp(f.date_from_ms / 1000, tz=timezone.utc).date()}")
        if f.date_to_ms is not None:
            parts.append(f"至 {datetime.fromtimestamp(f.date_to_ms / 1000, tz=timezone.utc).date()}")
    if f.conversation_kind == "group":
        parts.append("群聊")
    elif f.conversation_kind == "single":
        parts.append("单聊")
    if f.roomid:
        parts.append(f"会话 {f.roomid}")
    if f.staff_id:
        parts.append(f"员工 {f.staff_id}")
    if f.contact_id:
        parts.append(f"外部联系人 {f.contact_id}")
    if f.msgtypes:
        parts.append("类型 " + "/".join(f.msgtypes))
    if f.has_media is True:
        parts.append("含媒体")
    elif f.has_media is False:
        parts.append("不含媒体")
    if f.media_min_bytes is not None or f.media_max_bytes is not None:
        lo = f.media_min_bytes or 0
        hi = f.media_max_bytes or "∞"
        parts.append(f"媒体大小 {lo}–{hi} B")
    parts.append("排除已收藏" if not f.include_favorited else "包含已收藏")
    return "，".join(parts)


def _effective_date_to(f: CleanupFilter, now: datetime) -> Optional[int]:
    if f.older_than_days is not None:
        cutoff = now - timedelta(days=f.older_than_days)
        return int(cutoff.timestamp() * 1000)
    return f.date_to_ms


def _participant_predicate(tenant_id: str, userid: str):
    recipient_ids = select(ArchiveMessageRecipient.message_id).where(
        ArchiveMessageRecipient.tenant_id == tenant_id,
        ArchiveMessageRecipient.receiver_userid == userid,
    )
    return or_(
        ArchiveMessage.sender == userid,
        ArchiveMessage.id.in_(recipient_ids),
    )


def build_message_query(db: Session, tenant_id: str, f: CleanupFilter, *, now: datetime) -> Select:
    """Build the tenant-scoped candidate query. Always excludes rows already
    soft-deleted, so reruns/restarts are idempotent."""
    conditions = [
        ArchiveMessage.tenant_id == tenant_id,
        ArchiveMessage.deleted_at.is_(None),
    ]
    if f.conversation_kind == "group":
        conditions.append(ArchiveMessage.roomid.is_not(None))
    elif f.conversation_kind == "single":
        conditions.append(ArchiveMessage.roomid.is_(None))
    if f.roomid:
        conditions.append(ArchiveMessage.roomid == f.roomid)
    if f.staff_id:
        conditions.append(_participant_predicate(tenant_id, f.staff_id))
    if f.contact_id:
        conditions.append(_participant_predicate(tenant_id, f.contact_id))
    date_to = _effective_date_to(f, now)
    if f.date_from_ms is not None:
        conditions.append(ArchiveMessage.msgtime >= f.date_from_ms)
    if date_to is not None:
        conditions.append(ArchiveMessage.msgtime <= date_to)
    if f.msgtypes:
        conditions.append(ArchiveMessage.msgtype.in_(f.msgtypes))

    if f.has_media is not None or f.media_min_bytes is not None or f.media_max_bytes is not None:
        media_filter = [
            MediaFile.tenant_id == tenant_id,
            MediaFile.archive_message_id == ArchiveMessage.id,
        ]
        if f.media_min_bytes is not None:
            media_filter.append(MediaFile.file_size >= f.media_min_bytes)
        if f.media_max_bytes is not None:
            media_filter.append(MediaFile.file_size <= f.media_max_bytes)
        # EXISTS correlates inside the enclosing archive_messages FROM even
        # when the query is wrapped in a counting subquery.
        has_matching_media = exists().where(*media_filter)
        if f.has_media is True or f.media_min_bytes is not None or f.media_max_bytes is not None:
            conditions.append(has_matching_media)
        elif f.has_media is False:
            conditions.append(~has_matching_media)
    return select(ArchiveMessage).where(*conditions)


@dataclass(frozen=True)
class CleanupPreview:
    preview_version: str
    matched: int
    conversation_count: int
    contact_count: int
    staff_count: int
    msgtype_counts: dict[str, int]
    text_bytes_estimate: int
    media_bytes: int
    shared_media_bytes: int
    releasable_bytes: int
    favorited_count: int
    locked_count: int
    earliest_msgtime: Optional[int]
    latest_msgtime: Optional[int]
    summary: str


def _count_rows(db: Session, query: Select) -> int:
    return int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)


def preview_cleanup(
    db: Session,
    tenant_id: str,
    f: CleanupFilter,
    *,
    now: Optional[datetime] = None,
) -> CleanupPreview:
    """Compute the stable impact preview for a filter (never loads all rows)."""
    run_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    query = build_message_query(db, tenant_id, f, now=run_at)
    matched = _count_rows(db, query)

    # The aggregate sibling query covers exactly the same matched messages
    # (identical predicate built by build_message_query).
    agg = build_message_query(db, tenant_id, f, now=run_at).subquery()

    conversation_count = int(
        db.scalar(
            select(func.count(func.distinct(func.coalesce(agg.c.roomid, "direct")))).select_from(agg)
        ) or 0
    )
    msgtype_counts = dict(
        db.execute(
            select(agg.c.msgtype, func.count()).group_by(agg.c.msgtype)
        ).all()
    )
    bounds = db.execute(
        select(func.min(agg.c.msgtime), func.max(agg.c.msgtime))
    ).one()

    text_count = sum(count for msgtype, count in msgtype_counts.items() if msgtype == "text")
    text_bytes = text_count * ESTIMATED_TEXT_BYTES

    # Media estimates: bytes attached to matched messages, and the shared
    # (non-releasable) portion whose object is also used outside the match.
    media_query = (
        select(
            func.coalesce(func.sum(MediaFile.file_size), 0),
            func.count(MediaFile.id),
        )
        .join(ArchiveMessage, ArchiveMessage.id == MediaFile.archive_message_id)
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.id.in_(select(agg.c.id)),
            MediaFile.download_status == "downloaded",
        )
    )
    media_bytes, media_count = db.execute(media_query).one()
    media_bytes = int(media_bytes or 0)

    shared_bytes = 0
    if media_count:
        shared_rows = db.execute(
            select(
                MediaFile.storage_ref,
                MediaFile.storage_backend,
                func.sum(MediaFile.file_size).label("bytes"),
                func.count(func.distinct(MediaFile.archive_message_id)).label("msgs"),
            )
            .join(ArchiveMessage, ArchiveMessage.id == MediaFile.archive_message_id)
            .where(
                ArchiveMessage.tenant_id == tenant_id,
                ArchiveMessage.id.in_(select(agg.c.id)),
                MediaFile.storage_ref.is_not(None),
            )
            .group_by(MediaFile.storage_ref, MediaFile.storage_backend)
        ).all()
        matched_refs = {
            (row.storage_backend, row.storage_ref)
            for row in shared_rows
            if row.msgs > 1
        }
        # For each shared ref inside the match, subtract the bytes a purge of
        # only matched messages cannot release (object still referenced by a
        # non-matched message).
        for row in shared_rows:
            if (row.storage_backend, row.storage_ref) not in matched_refs:
                continue
            other = db.scalar(
                select(func.count(MediaFile.id)).where(
                    MediaFile.tenant_id == tenant_id,
                    MediaFile.storage_backend == row.storage_backend,
                    MediaFile.storage_ref == row.storage_ref,
                    MediaFile.archive_message_id.notin_(select(agg.c.id)),
                )
            )
            if other:
                shared_bytes += int(row.bytes)

    # Favorites (RND-365) are not implemented yet; the filter field is
    # accepted but the count is always zero until that epic lands.
    favorited_count = 0
    locked_count = 0
    tenant_locked = db.scalar(select(Tenant.deletion_locked).where(Tenant.id == tenant_id))
    if tenant_locked:
        locked_count = matched

    return CleanupPreview(
        preview_version=str(uuid.uuid4()),
        matched=matched,
        conversation_count=int(conversation_count or 0),
        contact_count=0,
        staff_count=0,
        msgtype_counts={str(k): int(v) for k, v in msgtype_counts.items()},
        text_bytes_estimate=text_bytes,
        media_bytes=media_bytes,
        shared_media_bytes=shared_bytes,
        releasable_bytes=max(media_bytes - shared_bytes, 0),
        favorited_count=favorited_count,
        locked_count=locked_count,
        earliest_msgtime=int(bounds[0]) if bounds[0] is not None else None,
        latest_msgtime=int(bounds[1]) if bounds[1] is not None else None,
        summary=filter_summary(f),
    )


def store_cleanup_preview(
    db: Session,
    *,
    tenant_id: str,
    preview: CleanupPreview,
    f: CleanupFilter,
    now: Optional[datetime] = None,
) -> MessageCleanupPreview:
    """Persist a preview snapshot under its short-lived version id."""
    run_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row = MessageCleanupPreview(
        id=preview.preview_version,
        tenant_id=tenant_id,
        matched=preview.matched,
        filter_snapshot=f.snapshot(),
        expires_at=run_at + PREVIEW_TTL,
    )
    db.add(row)
    db.flush()
    return row


def purge_expired_previews(db: Session, *, now: Optional[datetime] = None) -> int:
    """Delete expired preview snapshots; returns rows removed."""
    run_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = db.scalars(
        select(MessageCleanupPreview).where(MessageCleanupPreview.expires_at <= run_at)
    ).all()
    for row in rows:
        db.delete(row)
    return len(rows)


def create_cleanup_task(
    db: Session,
    *,
    tenant_id: str,
    actor_id: str,
    raw_filter: dict[str, Any],
    preview_version: str,
    confirmation: str,
    now: Optional[datetime] = None,
) -> MessageCleanupTask:
    """Validate preview binding + confirmation, then queue a task."""
    if confirmation != CONFIRMATION_PHRASE:
        raise CleanupConfirmationRequired("confirmation_required")
    run_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    locked = db.scalar(select(Tenant.deletion_locked).where(Tenant.id == tenant_id))
    if locked is None:
        raise CleanupError("tenant_not_found")
    if locked:
        raise CleanupLocked("deletion_locked")
    f = validate_filter(raw_filter)
    stored = db.scalar(
        select(MessageCleanupPreview).where(
            MessageCleanupPreview.id == preview_version,
            MessageCleanupPreview.tenant_id == tenant_id,
        )
    )
    if stored is None:
        raise CleanupPreviewStale("preview_unknown")
    stored_expires = (
        stored.expires_at
        if stored.expires_at.tzinfo is not None
        else stored.expires_at.replace(tzinfo=timezone.utc)
    )
    if stored_expires <= run_at:
        raise CleanupPreviewExpired("preview_expired")
    if stored.filter_snapshot != f.snapshot():
        raise CleanupPreviewStale("preview_filter_mismatch")
    current = preview_cleanup(db, tenant_id, f, now=run_at)
    if current.matched == 0:
        raise CleanupError("nothing_to_clean")
    if current.locked_count:
        raise CleanupLocked("deletion_locked")
    if abs(stored.matched - current.matched) > max(
        int(stored.matched * PREVIEW_DRIFT_TOLERANCE), 1
    ):
        raise CleanupPreviewStale("preview_stale")
    db.delete(stored)
    task = MessageCleanupTask(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        created_by_admin_user_id=actor_id,
        filter_snapshot=f.snapshot(),
        filter_summary=current.summary,
        preview_version=preview_version,
        preview_matched=stored.matched,
        status="queued",
        total_matched=current.matched,
        releasable_bytes=current.releasable_bytes,
        revision=1,
    )
    db.add(task)
    db.flush()
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=actor_id,
        action=AuditAction.MESSAGES_CLEANUP_TASK_CREATED,
        object_type=AuditObjectType.ARCHIVE_MESSAGE,
        object_id=task.id,
        detail={
            "preview_version": preview_version,
            "matched": current.matched,
            "releasable_bytes": current.releasable_bytes,
            "filter_summary": current.summary,
        },
    )
    return task


def get_cleanup_task(db: Session, tenant_id: str, task_id: str) -> MessageCleanupTask:
    task = db.scalar(
        select(MessageCleanupTask).where(
            MessageCleanupTask.id == task_id,
            MessageCleanupTask.tenant_id == tenant_id,
        )
    )
    if task is None:
        raise CleanupTaskNotFound("task_not_found")
    return task


def cancel_cleanup_task(
    db: Session, tenant_id: str, task_id: str, *, actor_id: str, now: Optional[datetime] = None
) -> MessageCleanupTask:
    task = get_cleanup_task(db, tenant_id, task_id)
    run_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if task.status in ("completed", "failed", "canceled"):
        return task
    task.status = "canceled"
    task.canceled_at = run_at
    task.finished_at = run_at
    task.revision += 1
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=actor_id,
        action=AuditAction.MESSAGES_CLEANUP_TASK_CANCELED,
        object_type=AuditObjectType.ARCHIVE_MESSAGE,
        object_id=task.id,
        detail={"task_status": "canceled"},
    )
    return task


def _matching_ids_page(
    db: Session, tenant_id: str, f: CleanupFilter, *, now: datetime, limit: int
) -> list[int]:
    query = (
        build_message_query(db, tenant_id, f, now=now)
        .with_only_columns(ArchiveMessage.id)
        .order_by(ArchiveMessage.id.asc())
        .limit(limit)
    )
    return list(db.scalars(query))


def process_cleanup_task_once(
    db: Session,
    task: MessageCleanupTask,
    *,
    page_size: int = MAX_TASK_PAGE,
    now: Optional[datetime] = None,
) -> str:
    """Advance one task by one bounded page; returns its new status.

    Idempotent: build_message_query excludes already-deleted rows, so a
    crash/restart between pages never double-processes. The tenant
    compliance hold is re-checked each page.
    """
    run_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tenant_id = task.tenant_id
    if task.status in ("completed", "failed", "canceled"):
        return task.status
    locked = db.scalar(select(Tenant.deletion_locked).where(Tenant.id == tenant_id))
    if locked:
        task.status = "failed"
        task.error_message = "deletion_locked"
        task.finished_at = run_at
        task.revision += 1
        return task.status
    if task.status == "queued":
        task.status = "running"
        task.started_at = run_at
        task.revision += 1

    f = validate_filter(task.filter_snapshot or {})
    message_ids = _matching_ids_page(db, tenant_id, f, now=run_at, limit=page_size)
    if not message_ids:
        task.status = "completed" if task.failed == 0 else "partial"
        task.finished_at = run_at
        task.revision += 1
        write_audit(
            db,
            tenant_id=tenant_id,
            admin_user_id=task.created_by_admin_user_id,
            action=AuditAction.MESSAGES_CLEANUP_COMPLETED,
            object_type=AuditObjectType.ARCHIVE_MESSAGE,
            object_id=task.id,
            detail={
                "succeeded": task.succeeded,
                "skipped": task.skipped,
                "failed": task.failed,
                "locked": task.locked,
            },
        )
        return task.status

    rows = db.scalars(
        select(ArchiveMessage).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.id.in_(message_ids),
        )
    ).all()
    now_dt = run_at
    page_succeeded = 0
    for message in rows:
        if message.deleted_at is not None:
            task.skipped += 1
            continue
        message.deleted_at = now_dt
        message.deleted_by_admin_user_id = task.created_by_admin_user_id
        message.purge_after = now_dt + timedelta(days=30)
        message.deletion_batch_id = task.id
        page_succeeded += 1
    task.succeeded += page_succeeded
    task.moved_bytes += sum(
        int(media_bytes) for media_bytes in db.scalars(
            select(func.coalesce(func.sum(MediaFile.file_size), 0)).where(
                MediaFile.tenant_id == tenant_id,
                MediaFile.archive_message_id.in_(message_ids),
            )
        )
    )
    task.revision += 1
    if task.succeeded + task.skipped >= task.total_matched:
        task.status = "completed" if task.failed == 0 else "partial"
        task.finished_at = run_at
    return task.status
