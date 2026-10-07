from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class StaffDirectoryItem(BaseModel):
    """One internal staff member with read-only archive message statistics."""

    wecom_userid: str
    name: Optional[str] = None
    department: Optional[str] = None
    avatar_url: Optional[str] = None
    avatar_status: Optional[str] = None
    has_seat: bool = False
    msg_count_30d: int = 0
    msg_count_total: int = 0


class StaffDirectoryPage(BaseModel):
    total: int
    page: int
    per_page: int
    items: List[StaffDirectoryItem]
