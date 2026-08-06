"""Restricted waiting/settings surface for provisioning tenants (RND-348)."""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.auth import get_provisioning_user
from app.db.models import AdminUser, Tenant
from app.web import render_template

router = APIRouter()


def _page(title: str, heading: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        render_template(
            "provisioning",
            page_title=title,
            heading=heading,
            body=body,
        )
    )


@router.get("/admin/provisioning", response_class=HTMLResponse)
def provisioning_waiting(
    _context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
) -> HTMLResponse:
    return _page(
        "组织配置中",
        "组织已创建，正在配置",
        "归档功能尚未启用。完成企业微信会话存档配置和运维验证后，平台管理员才能激活组织。",
    )


@router.get("/admin/provisioning/settings", response_class=HTMLResponse)
def provisioning_settings(
    _context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
) -> HTMLResponse:
    return _page(
        "配置准备",
        "配置准备",
        "请等待平台管理员完成会话存档凭证、回调和连通性验证。当前页面不会启动归档任务。",
    )


@router.get("/api/provisioning/status")
def provisioning_status(
    _context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
) -> dict:
    return {
        "lifecycle_status": "provisioning",
        "archive_enabled": False,
        "allowed_actions": ["view_status", "view_settings"],
    }
