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
from app.db.models import AdminUser, ArchiveMessage, MediaFile
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
    items = [
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
            "room_id": message.roomid,
            "msgtime": message.msgtime,
            "name": _media_label(message.structured_content),
        }
        for media_file, message in rows
    ]
    return MediaLibraryPage(items=items, total=total, has_more=len(items) == limit)
