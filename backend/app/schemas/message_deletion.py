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


class CleanupFilterIn(BaseModel):
    date_from_ms: Optional[int] = None
    date_to_ms: Optional[int] = None
    older_than_days: Optional[int] = None
    roomid: Optional[str] = None
    staff_id: Optional[str] = None
    contact_id: Optional[str] = None
    conversation_kind: Optional[str] = None
    msgtypes: list[str] = []
    has_media: Optional[bool] = None
    media_min_bytes: Optional[int] = None
    media_max_bytes: Optional[int] = None
    include_favorited: bool = False


class CleanupPreviewIn(BaseModel):
    filter: CleanupFilterIn


class CleanupPreviewOut(BaseModel):
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


class CleanupTaskCreateIn(BaseModel):
    filter: CleanupFilterIn
    preview_version: str
    confirmation: str


class CleanupTaskOut(BaseModel):
    id: str
    status: str
    filter_summary: str
    preview_version: str
    preview_matched: int
    total_matched: int
    succeeded: int
    skipped: int
    failed: int
    locked: int
    moved_bytes: int
    releasable_bytes: int
    failure_summary: Optional[dict] = None
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    canceled_at: Optional[datetime]


class CleanupTaskListOut(BaseModel):
    items: list[CleanupTaskOut]
    total: int
