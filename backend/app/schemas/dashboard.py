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
    # RND-344: additive fields for the tenant's formal archive overview.
    # Existing dashboard consumers keep their original windowed fields above.
    total_archived_messages: int
    archive_coverage_days: int
    first_archived_at: Optional[str] = None
    last_archived_at: Optional[str] = None
    archive_status: str
    sync_updated_at: Optional[str] = None
    archive_configured: bool
    can_manage_settings: bool
    type_composition: List[dict]
    storage_composition: List[dict]
    hourly_distribution: List[dict]
    # Optional insight errors are generic and safe to render to end users.
    insight_errors: dict[str, str]
    # KPI card input for the annual plan's expiry (ISO-8601 UTC). Populated
    # only when the tenant has a subscription whose plan bills annually; a
    # self-deployed instance without any subscription row keeps None, which
    # the dashboard renders as no card at all.
    annual_plan_expires_at: Optional[str] = None
