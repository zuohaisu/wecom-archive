"""HTTP contracts for the RND-362 deletion lifecycle."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MessageDeleteIn(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=200)


class MessageDeleteOut(BaseModel):
    deleted: int
    already_deleted: int
    not_found: int


class RecycleBinItemOut(BaseModel):
    msgid: str
    msgtype: Optional[str]
    roomid: Optional[str]
    msgtime: Optional[int]
    deleted_at: datetime
    deleted_by_admin_user_id: Optional[str]
    purge_after: datetime
    has_media: bool
    deletion_batch_id: Optional[str]


class RecycleBinOut(BaseModel):
    items: list[RecycleBinItemOut]
    total: int
