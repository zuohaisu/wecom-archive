"""HTTP contracts for the RND-362/364 deletion lifecycle."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MessageDeleteIn(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=200)
    reason: Optional[str] = Field(default=None, max_length=200)


class MessageDeleteOut(BaseModel):
    deleted: int
    already_deleted: int
    not_found: int
    deleted_message_ids: list[str] = []


class PurgeIn(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=200)
    confirm: bool = False


class PurgeOut(BaseModel):
    purged: int
    not_found: int
    media_retry_pending: int
    purged_message_ids: list[str] = []


class RecycleBinItemOut(BaseModel):
    msgid: str
    msgtype: Optional[str]
    roomid: Optional[str]
    msgtime: Optional[int]
    preview: Optional[str]
    has_media: bool
    deleted_at: datetime
    deleted_by_admin_user_id: Optional[str]
    purge_after: datetime
    deletion_batch_id: Optional[str]


class RecycleBinOut(BaseModel):
    items: list[RecycleBinItemOut]
    total: int


class PurgeMetricsOut(BaseModel):
    pending_purge_count: int
    oldest_pending_age_days: Optional[float]
    last_success_at: Optional[datetime]
    last_failure_at: Optional[datetime]
    recent_failure_count: int
    storage_retry_pending: int


class DeletionStatusOut(BaseModel):
    can_delete: bool
    deletion_locked: bool
