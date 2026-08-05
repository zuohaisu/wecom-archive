"""Response schemas for tenant-scoped external-contact identity APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.listing import ConversationOut


class ExternalContactSearchMatch(BaseModel):
    """One way a search term matched this same external identity."""

    match_type: str  # "remark" | "current_nickname" | "historical_nickname"
    follow_userid: Optional[str] = None


class ExternalContactFollowRemark(BaseModel):
    """An employee-scoped current/historical follow relationship."""

    follow_userid: str
    follow_user_display_name: str
    remark: Optional[str] = None
    is_active: bool
    observed_at: datetime


class ExternalContactNicknameHistoryItem(BaseModel):
    """A normalized customer-nickname state transition observed by the system."""

    old_nickname: Optional[str] = None
    new_nickname: Optional[str] = None
    observed_at: datetime


class ExternalContactListItem(BaseModel):
    id: int
    external_userid: str
    # Legacy RND-287 compatibility field. New clients must use display_name,
    # current_nickname, and follow_remarks rather than treating this as truth.
    name: Optional[str] = None
    display_name: str
    current_nickname: Optional[str] = None
    follow_remarks: list[ExternalContactFollowRemark] = Field(default_factory=list)
    search_matches: list[ExternalContactSearchMatch] = Field(default_factory=list)
    company: Optional[str] = None
    tags: list[str]
    source: Optional[str] = None
    owner_wecom_userid: Optional[str] = None
    owner_display_name: str
    last_interaction_at: Optional[datetime] = None
    message_count: Optional[int] = None


class ExternalContactListPage(BaseModel):
    items: list[ExternalContactListItem]
    total: int
    has_more: bool
    available_tags: list[str] = Field(default_factory=list)


class ExternalContactDetail(ExternalContactListItem):
    nickname_history: list[ExternalContactNicknameHistoryItem] = Field(default_factory=list)
    conversations: list[ConversationOut]
