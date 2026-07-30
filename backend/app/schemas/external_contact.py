"""Response schemas for the tenant-scoped external-contact list API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ExternalContactListItem(BaseModel):
    id: int
    external_userid: str
    name: Optional[str] = None
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
