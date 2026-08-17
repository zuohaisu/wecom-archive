"""Tenant-scoped collection plus PlatformAdmin-only product-use analysis."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_platform_admin
from app.db.models import AdminUser, PlatformAdmin
from app.db.session import get_db
from app.schemas.product_analytics import (
    ProductAnalyticsEventAcceptedOut,
    ProductAnalyticsEventIn,
    ProductAnalyticsOverviewOut,
    ProductAnalyticsTenantDetailOut,
    ProductAnalyticsTenantListOut,
)
from app.services import product_analytics

logger = logging.getLogger(__name__)
router = APIRouter(tags=["product-analytics"])


def _invalid_filter(error: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail="invalid_product_analytics_filter")


def _window(
    starts_at: datetime | None,
    ends_at: datetime | None,
) -> tuple[datetime, datetime]:
    try:
        return product_analytics.resolve_window(starts_at=starts_at, ends_at=ends_at)
    except product_analytics.ProductAnalyticsValidationError as error:
        raise _invalid_filter(error) from error


@router.post(
    "/api/product-analytics/events",
    response_model=ProductAnalyticsEventAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def collect_product_analytics_event(
    payload: ProductAnalyticsEventIn,
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProductAnalyticsEventAcceptedOut:
    """Best-effort browser collection with server-derived tenant identity."""
    user, tenant_id = auth
    try:
        product_analytics.record_frontend_event(
            db,
            event=payload,
            tenant_id=tenant_id,
            admin_user_id=user.id,
        )
        db.commit()
    except product_analytics.ProductAnalyticsValidationError as error:
        db.rollback()
        raise HTTPException(status_code=422, detail="invalid_product_analytics_event") from error
    except Exception:
        # Collection failures must be invisible to a user action.  Never log
        # payloads: a malformed client request could itself contain content.
        db.rollback()
        logger.warning("product analytics browser event was dropped", exc_info=True)
    return ProductAnalyticsEventAcceptedOut()


@router.get(
    "/api/platform/operations/product-analytics/overview",
    response_model=ProductAnalyticsOverviewOut,
)
def product_analytics_overview(
    starts_at: Optional[datetime] = Query(None),
    ends_at: Optional[datetime] = Query(None),
    tenant_id: Optional[str] = Query(None),
    event_name: Optional[str] = Query(None),
    _platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Aggregate-only Product Analytics; never returns raw event payloads."""
    start, end = _window(starts_at, ends_at)
    try:
        return product_analytics.overview(
            db,
            starts_at=start,
            ends_at=end,
            tenant_id=tenant_id,
            event_name=event_name,
        )
    except product_analytics.ProductAnalyticsValidationError as error:
        raise _invalid_filter(error) from error


@router.get(
    "/api/platform/operations/product-analytics/tenants",
    response_model=ProductAnalyticsTenantListOut,
)
def product_analytics_tenants(
    starts_at: Optional[datetime] = Query(None),
    ends_at: Optional[datetime] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    activity_status: str = Query("all"),
    tenant_id: Optional[str] = Query(None),
    event_name: Optional[str] = Query(None),
    _platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """A paginated, aggregate-only active/inactive tenant list."""
    start, end = _window(starts_at, ends_at)
    try:
        return product_analytics.list_tenants(
            db,
            starts_at=start,
            ends_at=end,
            page=page,
            page_size=page_size,
            activity_status=activity_status,
            tenant_id=tenant_id,
            event_name=event_name,
        )
    except product_analytics.ProductAnalyticsValidationError as error:
        raise _invalid_filter(error) from error


@router.get(
    "/api/platform/operations/product-analytics/tenants/{tenant_id}",
    response_model=ProductAnalyticsTenantDetailOut,
)
def product_analytics_tenant_detail(
    tenant_id: str,
    starts_at: Optional[datetime] = Query(None),
    ends_at: Optional[datetime] = Query(None),
    event_name: Optional[str] = Query(None),
    _platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """One tenant's usage summary without raw actions, content or media."""
    start, end = _window(starts_at, ends_at)
    try:
        return product_analytics.tenant_detail(
            db,
            tenant_id=tenant_id,
            starts_at=start,
            ends_at=end,
            event_name=event_name,
        )
    except product_analytics.ProductAnalyticsNotFoundError as error:
        raise HTTPException(status_code=404, detail="tenant_not_found") from error
    except product_analytics.ProductAnalyticsValidationError as error:
        raise _invalid_filter(error) from error
