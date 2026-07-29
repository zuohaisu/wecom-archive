"""Response schemas for the tenant-scoped media library API (RND-291)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class MediaFileListItem(BaseModel):
    """Safe media metadata for the admin media-library grid.

    Storage references and other sensitive media-file fields are deliberately
    absent. ``storage_backend`` is only the provider type; ``has_thumbnail``
    does not expose its storage reference.
    """

    id: int
    file_type: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    download_status: str
    storage_backend: Optional[str] = None
    has_thumbnail: bool = False
    created_at: datetime
    message_id: int
    room_id: Optional[str] = None
    msgtime: Optional[int] = None
    name: Optional[str] = None


class MediaLibraryPage(BaseModel):
    items: list[MediaFileListItem]
    total: int
    has_more: bool
