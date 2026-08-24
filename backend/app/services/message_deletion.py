"""Tenant-scoped soft-delete lifecycle for archived messages (RND-362)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.db.models import ArchiveMessage, Tenant

RECYCLE_BIN_RETENTION = timedelta(days=30)
MAX_DELETE_BATCH = 100


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
