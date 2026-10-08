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


class StaffCustomerItem(BaseModel):
    """One external customer a staff member has interacted with (RND-374)."""

    customer_userid: str
    display_name: Optional[str] = None
    is_registered_external: bool = False
    direct_messages: int = 0
    group_messages: int = 0
    message_count: int = 0
    first_interaction_at: Optional[str] = None
    last_interaction_at: Optional[str] = None


class StaffCustomersPage(BaseModel):
    total: int
    page: int
    per_page: int
    items: list[StaffCustomerItem]
    staff_userid: str
    staff_display_name: Optional[str] = None
    staff_admin_status: Optional[str] = None
