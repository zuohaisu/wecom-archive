"""Owner/admin-only archive-message deletion API (RND-362 / RND-364)."""

from __future__ import annotations

from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import AdminUser, ArchiveMessage, MediaFile, Tenant
from app.db.session import get_db
from app.schemas.message_deletion import (
    DeletionStatusOut,
    MessageDeleteIn,
    MessageDeleteOut,
    PurgeIn,
    PurgeOut,
    PurgeMetricsOut,
    RecycleBinItemOut,
    RecycleBinOut,
)
from app.services.message_deletion import (
    DeletionLockedError,
    MessageDeletionError,
    purge_messages,
    recycle_bin_metrics,
    restore_messages,
    soft_delete_messages,
)

router = APIRouter(prefix="/api/admin/messages", tags=["message-deletion"])

_PREVIEW_LENGTH = 80


@router.get("/deletion-status", response_model=DeletionStatusOut)
def deletion_status(
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role()),
) -> DeletionStatusOut:
    """Return whether the current tenant/role may delete archived messages.

    Any authenticated admin may read this so the console can hide/disable
    the delete surface without a failing mutation call. ``can_delete`` is
    role-gated (Owner/Admin); ``deletion_locked`` reflects the tenant's
    compliance/legal hold, which fails every deletion closed (RND-362).
    """
    user, tenant_id = auth
    locked = db.scalar(select(Tenant.deletion_locked).where(Tenant.id == tenant_id))
    if locked is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")
    return DeletionStatusOut(
        can_delete=user.role in ("owner", "admin"),
        deletion_locked=bool(locked),
    )


def _raise_deletion_error(error: MessageDeletionError) -> None:
    if isinstance(error, DeletionLockedError):
        raise HTTPException(status_code=423, detail="deletion_locked") from error
    code = str(error)
    if code in ("invalid_delete_batch", "invalid_restore_batch", "invalid_purge_batch"):
        raise HTTPException(status_code=422, detail=code) from error
    if code == "purge_confirmation_required":
        raise HTTPException(status_code=422, detail=code) from error
    raise HTTPException(status_code=404, detail="tenant_not_found") from error


@router.post("/delete", response_model=MessageDeleteOut)
def delete_messages(
    payload: MessageDeleteIn,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role("owner", "admin")),
) -> MessageDeleteOut:
    user, tenant_id = auth
    try:
        result = soft_delete_messages(
            db,
            tenant_id=tenant_id,
            actor_id=user.id,
            msgids=payload.message_ids,
            reason=payload.reason,
        )
        db.commit()
    except MessageDeletionError as error:
        db.rollback()
        _raise_deletion_error(error)
    return MessageDeleteOut(
        deleted=result.deleted,
        already_deleted=result.already_deleted,
        not_found=result.not_found,
        deleted_message_ids=list(result.deleted_message_ids),
    )


@router.get("/recycle-bin", response_model=RecycleBinOut)
def recycle_bin(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    msgtype: Optional[str] = Query(None, description="Filter by message type"),
    roomid: Optional[str] = Query(None, description="Filter by conversation room id"),
    deleted_by: Optional[str] = Query(None, description="Filter by deleting admin user id"),
    since: Optional[str] = Query(None, description="ISO8601 lower bound on deleted_at"),
    until: Optional[str] = Query(None, description="ISO8601 upper bound on deleted_at"),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role("owner", "admin")),
) -> RecycleBinOut:
    _, tenant_id = auth
    statement = select(ArchiveMessage).where(
        ArchiveMessage.tenant_id == tenant_id,
        ArchiveMessage.deleted_at.is_not(None),
    )
    if msgtype:
        statement = statement.where(ArchiveMessage.msgtype == msgtype)
    if roomid:
        statement = statement.where(ArchiveMessage.roomid == roomid)
    if deleted_by:
        statement = statement.where(ArchiveMessage.deleted_by_admin_user_id == deleted_by)
    if since:
        statement = statement.where(ArchiveMessage.deleted_at >= since)
    if until:
        statement = statement.where(ArchiveMessage.deleted_at <= until)
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = db.scalars(
        statement.order_by(ArchiveMessage.deleted_at.desc(), ArchiveMessage.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    message_ids = [row.id for row in rows]
    with_media = set()
    if message_ids:
        with_media = set(
            db.scalars(
                select(MediaFile.archive_message_id).where(
                    MediaFile.tenant_id == tenant_id,
                    MediaFile.archive_message_id.in_(message_ids),
                )
            )
        )
    return RecycleBinOut(
        total=total,
        items=[
            RecycleBinItemOut(
                msgid=row.msgid,
                msgtype=row.msgtype,
                roomid=row.roomid,
                msgtime=row.msgtime,
                preview=(row.content_text or "")[:_PREVIEW_LENGTH],
                has_media=row.id in with_media,
                deleted_at=row.deleted_at,
                deleted_by_admin_user_id=row.deleted_by_admin_user_id,
                purge_after=row.purge_after,
                deletion_batch_id=row.deletion_batch_id,
            )
            for row in rows
        ],
    )


@router.get("/recycle-bin/metrics", response_model=PurgeMetricsOut)
def recycle_bin_metrics_endpoint(
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role("owner", "admin")),
) -> PurgeMetricsOut:
    _, tenant_id = auth
    metrics = recycle_bin_metrics(db, tenant_id)
    return PurgeMetricsOut(
        pending_purge_count=metrics.pending_purge_count,
        oldest_pending_age_days=metrics.oldest_pending_age_days,
        last_success_at=metrics.last_success_at,
        last_failure_at=metrics.last_failure_at,
        recent_failure_count=metrics.recent_failure_count,
        storage_retry_pending=metrics.storage_retry_pending,
    )


@router.post("/restore", response_model=MessageDeleteOut)
def restore_deleted_messages(
    payload: MessageDeleteIn,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role("owner", "admin")),
) -> MessageDeleteOut:
    user, tenant_id = auth
    try:
        result = restore_messages(
            db,
            tenant_id=tenant_id,
            actor_id=user.id,
            msgids=payload.message_ids,
        )
        db.commit()
    except MessageDeletionError as error:
        db.rollback()
        _raise_deletion_error(error)
    return MessageDeleteOut(
        deleted=result.deleted,
        already_deleted=result.already_deleted,
        not_found=result.not_found,
        deleted_message_ids=list(result.deleted_message_ids),
    )


@router.post("/purge", response_model=PurgeOut)
def purge_deleted_messages(
    payload: PurgeIn,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role("owner", "admin")),
) -> PurgeOut:
    """Irreversible permanent deletion of recycle-bin messages.

    Requires explicit confirmation and fails closed on the tenant
    compliance hold. Media objects are only removed after the storage
    provider confirms deletion; a failure keeps a retryable state instead
    of pretending the database was fully cleaned (RND-364).
    """
    user, tenant_id = auth
    try:
        result = purge_messages(
            db,
            tenant_id=tenant_id,
            msgids=payload.message_ids,
            actor_id=user.id,
            confirm=payload.confirm,
        )
        db.commit()
    except MessageDeletionError as error:
        db.rollback()
        _raise_deletion_error(error)
    return PurgeOut(
        purged=result.purged,
        not_found=result.not_found,
        media_retry_pending=result.media_retry_pending,
        purged_message_ids=list(result.purged_message_ids),
    )
