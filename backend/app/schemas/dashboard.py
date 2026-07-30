from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class DayBucket(BaseModel):
    date: str
    text_count: int
    media_count: int


class ActivityItem(BaseModel):
    id: str
    badge: str
    actor: str
    description: str
    scope: Optional[str] = None
    ip: Optional[str] = None
    time: str


class DashboardOut(BaseModel):
    range_days: int
    days: int
    total_messages: int
    storage_bytes: int
    staff_count: int
    silent_staff_30d: int
    sync_status: str
    sync_healthy: bool
    daily_series: List[DayBucket]
    recent_activity: List[ActivityItem]
    generated_at: str
