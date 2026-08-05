"""A6-1 (RND-291): tenant-scoped media-library list and filters.

This read-only route intentionally exposes only safe metadata. It does not
write audit records: media download auditing belongs to A6-2.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.conversation_membership import _direct_conv_id
from app.db.group_chat_metadata import load_group_chat_display_names
from app.db.models import AdminUser, ArchiveMessage, ArchiveMessageRecipient, Contact, MediaFile
from app.display_names import resolve_person_display_name, resolve_room_display_name
from app.db.session import get_db
from app.schemas.media_library import MediaLibraryPage

router = APIRouter()

_ALLOWED_TYPES = {"image", "video", "voice", "file"}


def _media_label(structured_content: object) -> Optional[str]:
    """Defensively extract an attachment display name from structured content."""
    if not isinstance(structured_content, dict):
        return None
    for key in ("title", "filename"):
        value = structured_content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    items = structured_content.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        value = items[0].get("title") or items[0].get("filename")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@router.get("/media", response_model=MediaLibraryPage)
def list_media(
    file_type: Optional[str] = Query(
        None, description="Comma-separated: image,file,voice,video"
    ),
    days: Optional[int] = Query(
        None, ge=0, description="Return only the last N days by media creation time"
    ),
    since: Optional[datetime] = Query(None, description="ISO8601 lower bound"),
    until: Optional[datetime] = Query(None, description="ISO8601 upper bound"),
    q: Optional[str] = Query(None, description="Optional file-name/content search"),
    sort: Optional[str] = Query("newest", description="newest|oldest|size_desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=200),
    db: Session = Depends(get_db),
    auth: tuple[AdminUser, str] = Depends(require_role()),
) -> MediaLibraryPage:
    """List media belonging to the authenticated tenant only."""
    _, tenant_id = auth

    types = None
    if file_type:
        types = [item.strip() for item in file_type.split(",") if item.strip()]
        bad_types = set(types) - _ALLOWED_TYPES
        if bad_types:
            raise HTTPException(status_code=400, detail=f"unknown file_type: {sorted(bad_types)}")

    # Both tables are tenant-scoped. Checking each table independently also
    # fail-closes if a malformed media row points at a parent from another
    # tenant, rather than relying on the foreign-key id alone.
    statement = (
        select(MediaFile, ArchiveMessage)
        .join(
            ArchiveMessage,
            and_(
                MediaFile.archive_message_id == ArchiveMessage.id,
                MediaFile.tenant_id == ArchiveMessage.tenant_id,
            ),
        )
        .where(MediaFile.tenant_id == tenant_id)
        .where(ArchiveMessage.tenant_id == tenant_id)
    )
    if types:
        statement = statement.where(MediaFile.file_type.in_(types))
    if days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
    if since is not None:
        statement = statement.where(MediaFile.created_at >= since)
    if until is not None:
        statement = statement.where(MediaFile.created_at <= until)
    if q:
        statement = statement.where(ArchiveMessage.content_text.ilike(f"%{q}%"))

    if sort == "oldest":
        statement = statement.order_by(MediaFile.created_at.asc(), MediaFile.id.asc())
    elif sort == "size_desc":
        statement = statement.order_by(MediaFile.file_size.desc().nullslast(), MediaFile.id.asc())
    else:
        statement = statement.order_by(MediaFile.created_at.desc(), MediaFile.id.asc())

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.execute(statement.offset(offset).limit(limit)).all()
    message_ids = [message.id for _, message in rows]
    recipients_by_message: dict[int, list[str]] = {}
    if message_ids:
        for message_id, receiver_userid in db.execute(
            select(
                ArchiveMessageRecipient.message_id,
                ArchiveMessageRecipient.receiver_userid,
            ).where(
                ArchiveMessageRecipient.tenant_id == tenant_id,
                ArchiveMessageRecipient.message_id.in_(message_ids),
            )
        ):
            recipients_by_message.setdefault(message_id, []).append(receiver_userid)

    person_ids = {
        user_id
        for _, message in rows
        if not message.roomid
        for user_id in [message.sender, *recipients_by_message.get(message.id, [])]
        if user_id
    }
    display_names = {
        user_id: name
        for user_id, name in db.execute(
            select(Contact.wecom_userid, Contact.name).where(
                Contact.tenant_id == tenant_id,
                Contact.wecom_userid.in_(person_ids),
            )
        )
    } if person_ids else {}

    room_display_names = load_group_chat_display_names(
        db, tenant_id, (message.roomid for _, message in rows)
    )

    items = []
    for media_file, message in rows:
        if message.roomid:
            conversation_id = message.roomid
            session_title = resolve_room_display_name(
                message.roomid, room_display_names.get(message.roomid)
            )
        else:
            # Direct messages normally have exactly one recipient. Keep a
            # visible, non-empty fallback for malformed legacy rows rather
            # than silently returning an unusable blank field.
            sender = message.sender or "unknown"
            receiver = next(
                (user_id for user_id in recipients_by_message.get(message.id, []) if user_id != sender),
                "unknown",
            )
            conversation_id = _direct_conv_id(sender, receiver)
            session_title = resolve_person_display_name(receiver, display_names.get(receiver))

        items.append(
            {
                "id": media_file.id,
                "file_type": media_file.file_type,
                "mime_type": media_file.mime_type,
                "file_size": media_file.file_size,
                "image_width": media_file.image_width,
                "image_height": media_file.image_height,
                "download_status": media_file.download_status,
                "storage_backend": media_file.storage_backend,
                "has_thumbnail": bool(media_file.thumbnail_ref),
                "created_at": media_file.created_at,
                "message_id": media_file.archive_message_id,
                "msgid": message.msgid,
                "conversation_id": conversation_id,
                "session_title": session_title,
                "room_id": message.roomid,
                "msgtime": message.msgtime,
                "name": _media_label(message.structured_content),
            }
        )
    return MediaLibraryPage(items=items, total=total, has_more=len(items) == limit)
