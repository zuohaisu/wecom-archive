"""Restricted waiting/settings surface for provisioning tenants (RND-348)."""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.auth import get_provisioning_user
from app.db.models import AdminUser, Tenant
from app.web import render_template

router = APIRouter()


@router.get("/admin/provisioning", response_class=HTMLResponse)
def provisioning_waiting(
    _context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
) -> HTMLResponse:
    return HTMLResponse(
        render_template(
            "provisioning",
            page_title="组织配置中",
            heading="组织已创建",
            body="完成企业微信会话存档配置和自动检查后，系统会开始一次 15 天免费试用；无需先付款。",
            primary_href="/admin/billing",
            primary_label="开始 15 天免费试用",
        )
    )


@router.get("/admin/provisioning/settings", response_class=HTMLResponse)
def provisioning_settings(
    _context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
) -> HTMLResponse:
    return HTMLResponse(
        render_template(
            "provisioning",
            page_title="配置准备",
            heading="配置准备",
            body="请按页面指引完成企业微信会话存档配置。自动检查通过后开始 15 天免费试用；此前不会启动归档任务。",
            primary_href="/admin/billing",
            primary_label="查看试用与续费",
        )
    )


@router.get("/api/provisioning/status")
def provisioning_status(
    _context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
) -> dict:
    return {
        "lifecycle_status": "provisioning",
        "archive_enabled": False,
        "allowed_actions": ["view_status", "purchase_plan", "view_settings"],
    }
