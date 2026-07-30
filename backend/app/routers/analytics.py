"""Authenticated, tenant-scoped usage analytics API (RND-283)."""
from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db.models import AdminUser
from app.db.session import get_db
from app.services.analytics_service import (
    hourly_distribution,
    storage_composition,
    trend,
    type_composition,
)
from app.services.usageservice import count_messages, get_archived_days, sum_storage

router = APIRouter()


class OverviewOut(BaseModel):
    archived_days: int
    total_messages: int
    avg_daily: float
    storage_bytes: int


class UsageAnalyticsOut(BaseModel):
    overview: OverviewOut
    trend: dict
    type_composition: list[dict]
    storage_composition: list[dict]
    hourly_distribution: list[dict]
    meta: dict


@router.get("/api/admin/usage", response_model=UsageAnalyticsOut)
def get_usage_analytics(
    days: int = Query(30, enum=[7, 30, 90]),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """Return aggregates only; tenant_id is obtained solely from the session."""
    _, tenant_id = auth
    archived_days = get_archived_days(db, tenant_id)
    total_messages = count_messages(db, tenant_id)
    storage = storage_composition(db, tenant_id)
    estimated_text_bytes = next(
        item["bytes"] for item in storage if item["category"] == "text_and_index_estimate"
    )
    return {
        "overview": {
            "archived_days": archived_days,
            "total_messages": total_messages,
            "avg_daily": total_messages / archived_days if archived_days else 0.0,
            "storage_bytes": sum_storage(db, tenant_id) + estimated_text_bytes,
        },
        "trend": trend(db, tenant_id, days),
        "type_composition": type_composition(db, tenant_id, days),
        "storage_composition": storage,
        "hourly_distribution": hourly_distribution(db, tenant_id, days),
        "meta": {"period_days": days},
    }
