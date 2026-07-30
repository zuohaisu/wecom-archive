"""Authenticated tenant dashboard aggregate API (RND-282)."""
from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db.models import AdminUser
from app.db.session import get_db
from app.schemas.dashboard import DashboardOut
from app.services.dashboard_service import build_dashboard

router = APIRouter()


@router.get("/api/admin/dashboard", response_model=DashboardOut)
def get_dashboard(
    range_days: int = Query(30, ge=1, le=90, alias="range"),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """Return dashboard aggregates scoped exclusively to the authenticated tenant."""
    _, tenant_id = auth
    if range_days not in (14, 30, 90):
        range_days = 30
    return build_dashboard(db, tenant_id, range_days)
