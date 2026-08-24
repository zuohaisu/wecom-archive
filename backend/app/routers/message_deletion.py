"""Owner/admin-only archive-message deletion API (RND-362)."""

from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import AdminUser, ArchiveMessage, MediaFile
from app.db.session import get_db
from app.schemas.message_deletion import (
    MessageDeleteIn,
    MessageDeleteOut,
    RecycleBinItemOut,
    RecycleBinOut,
)
from app.services.message_deletion import (
    DeletionLockedError,
    MessageDeletionError,
    restore_messages,
    soft_delete_messages,
)

router = APIRouter(prefix="/api/admin/messages", tags=["message-deletion"])


def _raise_deletion_error(error: MessageDeletionError) -> None:
    if isinstance(error, DeletionLockedError):
        raise HTTPException(status_code=423, detail="deletion_locked") from error
    if str(error) == "invalid_delete_batch":
        raise HTTPException(status_code=422, detail="invalid_delete_batch") from error
    if str(error) == "invalid_restore_batch":
        raise HTTPException(status_code=422, detail="invalid_restore_batch") from error
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
    )


@router.get("/recycle-bin", response_model=RecycleBinOut)
def recycle_bin(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role("owner", "admin")),
) -> RecycleBinOut:
    _, tenant_id = auth
    statement = select(ArchiveMessage).where(
        ArchiveMessage.tenant_id == tenant_id,
        ArchiveMessage.deleted_at.is_not(None),
    )
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
                deleted_at=row.deleted_at,
                deleted_by_admin_user_id=row.deleted_by_admin_user_id,
                purge_after=row.purge_after,
                has_media=row.id in with_media,
                deletion_batch_id=row.deletion_batch_id,
            )
            for row in rows
        ],
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
    )
