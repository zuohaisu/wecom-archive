"""Tenant-scoped soft-delete lifecycle for archived messages (RND-362)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.db.models import (
    ArchiveMessage,
    ArchiveMessageRecipient,
    AuditLog,
    MediaFile,
    MediaPurgeRetry,
    MessageRevocation,
    ReachabilityFinding,
    RetentionLock,
    Tenant,
)

RECYCLE_BIN_RETENTION = timedelta(days=30)
MAX_DELETE_BATCH = 100
MAX_PURGE_BATCH = 200
PURGE_MAX_ATTEMPTS = 10
PURGE_RETRY_BACKOFF = timedelta(hours=1)
logger = logging.getLogger(__name__)


class MessageDeletionError(RuntimeError):
    """Base error for the deletion domain."""


class DeletionLockedError(MessageDeletionError):
    """The tenant's compliance hold rejects lifecycle writes."""


@dataclass(frozen=True)
class DeletionResult:
    deleted: int
    already_deleted: int
    not_found: int
    deleted_message_ids: tuple[str, ...] = ()


def active_message_filter():
    """SQL predicate every normal archive read must apply.

    A tombstone remains a normal ``ArchiveMessage`` row so repeat syncs keep
    their stable (tenant_id, msgid) identity instead of accidentally restoring
    a user-deleted message.
    """
    return ArchiveMessage.deleted_at.is_(None)


def ensure_deletion_allowed(db: Session, tenant_id: str) -> None:
    locked = db.scalar(select(Tenant.deletion_locked).where(Tenant.id == tenant_id))
    if locked is None:
        raise MessageDeletionError("tenant_not_found")
    if locked:
        raise DeletionLockedError("deletion_locked")


def soft_delete_messages(
    db: Session,
    *,
    tenant_id: str,
    actor_id: str,
    msgids: Iterable[str],
    reason: str | None = None,
    at: datetime | None = None,
    batch_id: str | None = None,
) -> DeletionResult:
    """Move current-tenant messages to the recycle bin, idempotently."""
    stable_ids = list(dict.fromkeys(item for item in msgids if item))
    if not stable_ids or len(stable_ids) > MAX_DELETE_BATCH:
        raise MessageDeletionError("invalid_delete_batch")
    ensure_deletion_allowed(db, tenant_id)
    rows = db.scalars(
        select(ArchiveMessage).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgid.in_(stable_ids),
        )
    ).all()
    found = {row.msgid for row in rows}
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    deleted = 0
    already_deleted = 0
    for row in rows:
        if row.deleted_at is not None:
            already_deleted += 1
            continue
        row.deleted_at = now
        row.deleted_by_admin_user_id = actor_id
        row.delete_reason = reason
        row.purge_after = now + RECYCLE_BIN_RETENTION
        row.restored_at = None
        row.restored_by_admin_user_id = None
        row.deletion_batch_id = batch_id
        deleted += 1
    result = DeletionResult(
        deleted,
        already_deleted,
        len(stable_ids) - len(found),
        tuple(row.msgid for row in rows if row.deleted_at is not None and row.deleted_at == now),
    )
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=actor_id,
        action=AuditAction.MESSAGES_SOFT_DELETED,
        object_type=AuditObjectType.ARCHIVE_MESSAGE,
        object_id=None,
        detail={
            "deleted": result.deleted,
            "already_deleted": result.already_deleted,
            "not_found": result.not_found,
            "batch_id": batch_id,
        },
    )
    return result


@dataclass(frozen=True)
class PurgeResult:
    purged: int
    not_found: int
    media_retry_pending: int
    locked: int
    purged_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PurgeMetrics:
    pending_purge_count: int
    oldest_pending_age_days: Optional[float]
    last_success_at: Optional[datetime]
    last_failure_at: Optional[datetime]
    recent_failure_count: int
    storage_retry_pending: int


def _resolve_media_provider() -> Callable[[str, Optional[str], Optional[str]], bool]:
    """Return a ``delete_object(backend, ref) -> bool`` deleter.

    Deleting is best-effort; it returns True when the object is gone (or
    there is nothing to delete) and False when the object could not be
    confirmed deleted, so the caller can keep a retryable purge state
    instead of pretending the object was cleaned.
    """

    def delete_object(backend: str, ref: Optional[str]) -> bool:
        if not ref:
            return True
        try:
            from app.media_storage import get_media_storage_provider

            provider = get_media_storage_provider(backend)
            return bool(provider.delete(ref))
        except Exception as exc:  # noqa: BLE001 - provider failure is expected at this boundary
            logger.warning(
                "message purge: media object delete failed (backend=%s): %s",
                backend,
                type(exc).__name__,
            )
            return False

    return delete_object


def _delete_media_for_message(
    db: Session,
    tenant_id: str,
    message_id: int,
    delete_object: Callable[[str, Optional[str], Optional[str]], bool],
    now: datetime,
) -> tuple[int, int]:
    """Delete one message's MediaFile rows; return (deleted_rows, retry_pending).

    The object is removed from storage first, and only a confirmed deletion
    removes the MediaFile row. A failure leaves the MediaFile row and records
    a durable retry row so the purge worker can retry later.
    """
    media_rows = db.scalars(
        select(MediaFile).where(
            MediaFile.tenant_id == tenant_id,
            MediaFile.archive_message_id == message_id,
        )
    ).all()
    deleted_rows = 0
    retry_pending = 0
    for media in media_rows:
        # Never remove an object still referenced by another media row
        # (shared/derived-file safety, RND-361/RND-364).
        in_use = db.scalar(
            select(MediaFile.id)
            .where(
                MediaFile.id != media.id,
                MediaFile.tenant_id == tenant_id,
                MediaFile.storage_backend == media.storage_backend,
                MediaFile.storage_ref == media.storage_ref,
            )
            .limit(1)
        )
        if in_use is not None:
            db.delete(media)
            deleted_rows += 1
            continue
        if delete_object(media.storage_backend or "", media.storage_ref):
            db.delete(media)
            deleted_rows += 1
        else:
            retry = db.scalar(
                select(MediaPurgeRetry).where(
                    MediaPurgeRetry.media_file_id == media.id,
                    MediaPurgeRetry.tenant_id == tenant_id,
                )
            )
            if retry is None:
                retry = MediaPurgeRetry(
                    id=str(uuid.uuid4()),
                    media_file_id=media.id,
                    tenant_id=tenant_id,
                    storage_backend=media.storage_backend,
                    storage_ref=media.storage_ref,
                    attempts=0,
                    next_retry_at=now,
                )
                db.add(retry)
            retry.attempts += 1
            retry.last_error = "object_storage_delete_failed"
            retry.next_retry_at = now + PURGE_RETRY_BACKOFF
            retry_pending += 1
    return deleted_rows, retry_pending


def _purge_message_row(db: Session, message: ArchiveMessage) -> None:
    """Remove derived rows first, then the message tombstone itself."""
    db.execute(
        delete(ArchiveMessageRecipient).where(
            ArchiveMessageRecipient.message_id == message.id,
            ArchiveMessageRecipient.tenant_id == message.tenant_id,
        )
    )
    db.execute(
        delete(MessageRevocation).where(
            (MessageRevocation.revoke_event_message_id == message.id)
            | (MessageRevocation.original_message_id == message.id)
        )
    )
    db.execute(
        delete(RetentionLock).where(RetentionLock.archive_message_id == message.id)
    )
    db.execute(
        delete(ReachabilityFinding).where(
            ReachabilityFinding.archive_message_id == message.id
        )
    )
    db.delete(message)


def purge_messages(
    db: Session,
    *,
    tenant_id: str,
    msgids: Iterable[str],
    actor_id: Optional[str] = None,
    at: Optional[datetime] = None,
    confirm: bool = True,
    delete_object: Optional[Callable[[str, Optional[str], Optional[str]], bool]] = None,
) -> PurgeResult:
    """Permanently purge recycle-bin messages, one batch at a time.

    Only rows currently in the caller's tenant recycle bin are touched
    (idempotent under concurrency: an already-purged row is a no-op).
    Object-storage failures keep the media row and record a retry; the
    message row is only removed once every derived row and media row is
    gone, so the database never claims a cleanup the storage side failed.
    """
    if not confirm:
        raise MessageDeletionError("purge_confirmation_required")
    stable_ids = list(dict.fromkeys(item for item in msgids if item))
    if not stable_ids or len(stable_ids) > MAX_PURGE_BATCH:
        raise MessageDeletionError("invalid_purge_batch")
    ensure_deletion_allowed(db, tenant_id)
    deleter = delete_object or _resolve_media_provider()
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = db.scalars(
        select(ArchiveMessage).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgid.in_(stable_ids),
            ArchiveMessage.deleted_at.is_not(None),
        )
    ).all()
    found = {row.msgid for row in rows}
    purged = 0
    media_retry_pending = 0
    locked = 0
    purged_ids: list[str] = []
    for message in rows:
        media_deleted, media_pending = _delete_media_for_message(
            db, tenant_id, message.id, deleter, now
        )
        if media_pending:
            media_retry_pending += 1
            continue
        _purge_message_row(db, message)
        purged += 1
        purged_ids.append(message.msgid)
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=actor_id,
        action=AuditAction.MESSAGES_PURGED,
        object_type=AuditObjectType.ARCHIVE_MESSAGE,
        object_id=None,
        detail={
            "purged": purged,
            "media_retry_pending": media_retry_pending,
            "not_found": len(stable_ids) - len(found),
            "actor": "system" if actor_id is None else "admin",
        },
    )
    if media_retry_pending:
        write_audit(
            db,
            tenant_id=tenant_id,
            admin_user_id=actor_id,
            action=AuditAction.MESSAGES_PURGE_MEDIA_FAILED,
            object_type=AuditObjectType.MEDIA_FILE,
            detail={"pending": media_retry_pending, "actor": "system" if actor_id is None else "admin"},
        )
    return PurgeResult(
        purged=purged,
        not_found=len(stable_ids) - len(found),
        media_retry_pending=media_retry_pending,
        locked=locked,
        purged_message_ids=tuple(purged_ids),
    )


def purge_expired_messages(
    db: Session,
    tenant_id: str,
    *,
    limit: int = MAX_PURGE_BATCH,
    at: Optional[datetime] = None,
    delete_object: Optional[Callable[[str, Optional[str], Optional[str]], bool]] = None,
) -> PurgeResult:
    """Auto-cleanup: purge every recycle-bin message whose 30-day window is up."""
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = db.scalars(
        select(ArchiveMessage.msgid)
        .where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.deleted_at.is_not(None),
            ArchiveMessage.purge_after.is_not(None),
            ArchiveMessage.purge_after <= now,
        )
        .order_by(ArchiveMessage.purge_after.asc())
        .limit(limit)
    ).all()
    if not rows:
        return PurgeResult(0, 0, 0, 0)
    return purge_messages(
        db,
        tenant_id=tenant_id,
        msgids=rows,
        actor_id=None,
        at=now,
        confirm=True,
        delete_object=delete_object,
    )


def retry_media_purges(
    db: Session,
    tenant_id: str,
    *,
    limit: int = 100,
    at: Optional[datetime] = None,
    delete_object: Optional[Callable[[str, Optional[str], Optional[str]], bool]] = None,
) -> int:
    """Retry failed object-storage deletions; returns retries resolved."""
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    retries = db.scalars(
        select(MediaPurgeRetry)
        .where(
            MediaPurgeRetry.tenant_id == tenant_id,
            MediaPurgeRetry.next_retry_at <= now,
            MediaPurgeRetry.attempts < PURGE_MAX_ATTEMPTS,
        )
        .order_by(MediaPurgeRetry.next_retry_at.asc())
        .limit(limit)
    ).all()
    deleter = delete_object or _resolve_media_provider()
    resolved = 0
    for retry in retries:
        media = db.scalar(
            select(MediaFile).where(
                MediaFile.id == retry.media_file_id,
                MediaFile.tenant_id == tenant_id,
            )
        )
        if media is None:
            db.delete(retry)
            resolved += 1
            continue
        if deleter(media.storage_backend or "", media.storage_ref):
            db.delete(media)
            db.delete(retry)
            resolved += 1
        else:
            retry.attempts += 1
            retry.next_retry_at = now + PURGE_RETRY_BACKOFF
    return resolved


def recycle_bin_metrics(db: Session, tenant_id: str, *, at: Optional[datetime] = None) -> PurgeMetrics:
    """Non-sensitive operational metrics for the recycle bin / purge health."""
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    pending = db.scalar(
        select(func.count(ArchiveMessage.id)).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.deleted_at.is_not(None),
            ArchiveMessage.purge_after.is_not(None),
            ArchiveMessage.purge_after <= now,
        )
    ) or 0
    oldest = db.scalar(
        select(func.min(ArchiveMessage.purge_after)).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.deleted_at.is_not(None),
            ArchiveMessage.purge_after.is_not(None),
            ArchiveMessage.purge_after <= now,
        )
    )
    if oldest is not None and oldest.tzinfo is None:
        # SQLite returns naive DATETIME values; treat them as UTC.
        oldest = oldest.replace(tzinfo=timezone.utc)
    oldest_age = None if oldest is None else (now - oldest).total_seconds() / 86400.0
    last_success = db.scalar(
        select(func.max(AuditLog.created_at)).where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AuditAction.MESSAGES_PURGED,
        )
    )
    last_failure = db.scalar(
        select(func.max(AuditLog.created_at)).where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AuditAction.MESSAGES_PURGE_MEDIA_FAILED,
        )
    )
    recent_failures = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AuditAction.MESSAGES_PURGE_MEDIA_FAILED,
            AuditLog.created_at >= now - timedelta(days=7),
        )
    ) or 0
    storage_pending = db.scalar(
        select(func.count(MediaPurgeRetry.id)).where(
            MediaPurgeRetry.tenant_id == tenant_id,
            MediaPurgeRetry.attempts < PURGE_MAX_ATTEMPTS,
        )
    ) or 0
    return PurgeMetrics(
        pending_purge_count=int(pending),
        oldest_pending_age_days=oldest_age,
        last_success_at=last_success,
        last_failure_at=last_failure,
        recent_failure_count=int(recent_failures),
        storage_retry_pending=int(storage_pending),
    )


def restore_messages(
    db: Session,
    *,
    tenant_id: str,
    actor_id: str,
    msgids: Iterable[str],
    at: datetime | None = None,
) -> DeletionResult:
    """Restore recycle-bin messages in the caller's tenant, idempotently."""
    stable_ids = list(dict.fromkeys(item for item in msgids if item))
    if not stable_ids or len(stable_ids) > MAX_DELETE_BATCH:
        raise MessageDeletionError("invalid_restore_batch")
    ensure_deletion_allowed(db, tenant_id)
    rows = db.scalars(
        select(ArchiveMessage).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgid.in_(stable_ids),
        )
    ).all()
    found = {row.msgid for row in rows}
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    restored = 0
    already_active = 0
    for row in rows:
        if row.deleted_at is None:
            already_active += 1
            continue
        row.deleted_at = None
        row.restored_at = now
        row.restored_by_admin_user_id = actor_id
        row.purge_after = None
        row.deletion_batch_id = None
        restored += 1
    result = DeletionResult(
        restored,
        already_active,
        len(stable_ids) - len(found),
        tuple(row.msgid for row in rows if row.deleted_at is None and row.restored_at == now),
    )
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=actor_id,
        action=AuditAction.MESSAGES_RESTORED,
        object_type=AuditObjectType.ARCHIVE_MESSAGE,
        detail={
            "restored": result.deleted,
            "already_active": result.already_deleted,
            "not_found": result.not_found,
        },
    )
    return result
