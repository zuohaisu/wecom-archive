"""Haisu-request platform finance page: super-admin 订单明细.

Platform-admin-only (page redirects to /platform/login like every other
platform shell; API 401). Read-only listing of provider payment orders
(入账) and refund orders (退款); the manual ledger stays out on purpose.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_platform_admin, require_platform_admin_optional
from app.db.models import PlatformAdmin
from app.db.session import get_db
from app.schemas.platform_finance import FinanceOrderListOut
from app.services.platform_finance import (
    PlatformFinanceValidationError,
    list_orders,
)
from app.web import render_template
from app.web.sidenav import render_platform_admin_bar, render_platform_sidenav, render_platform_topbar

router = APIRouter(tags=["platform-finance"])

_PAGE_PATH = "/platform/finance/orders"


@router.get(_PAGE_PATH, response_class=HTMLResponse, name="platform_page_finance_orders")
def finance_orders_page(
    admin: Optional[PlatformAdmin] = Depends(require_platform_admin_optional),
) -> HTMLResponse:
    if admin is None:
        return RedirectResponse("/platform/login", status_code=302)
    return HTMLResponse(
        render_template(
            "platform_finance_orders",
            sidenav=render_platform_sidenav("finance-orders"),
            admin_bar=render_platform_admin_bar(admin.email),
            topbar=render_platform_topbar("订单明细"),
        )
    )


@router.get("/api/platform/finance/orders", response_model=FinanceOrderListOut)
def finance_orders_api(
    kind: str = Query("all"),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    _platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> FinanceOrderListOut:
    try:
        result = list_orders(
            db,
            kind=kind,
            status=status or None,
            q=(q or "").strip() or None,
            page=page,
            page_size=page_size,
        )
    except PlatformFinanceValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return FinanceOrderListOut(**result)
