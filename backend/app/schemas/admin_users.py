"""Response schemas for the RND-284 admin-user list API."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

Role = Literal["owner", "admin", "compliance", "legal", "readonlyaudit"]
UserStatus = Literal["active", "disabled"]


class AdminUserListItem(BaseModel):
    id: str
    name: Optional[str] = None
    wecom_user_id: str
    email: Optional[str] = None
    department: Optional[str] = None
    avatar_url: Optional[str] = None
    avatar_status: str = "missing"
    role: Role
    status: UserStatus
    last_active_at: Optional[str] = None
    msg_count_30d: int = 0


class AdminUserDetail(AdminUserListItem):
    last_login_at: Optional[str] = None


class AdminUserListOut(BaseModel):
    items: list[AdminUserListItem]
    total: int
    page: int
    per_page: int
