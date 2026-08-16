"""Restricted self-service surface for provisioning tenants (RND-348 / RND-386).

Only a provisioning-scoped owner session may reach these routes. The tenant
scope comes exclusively from the authenticated session; tenant_id, corp_id and
lifecycle values submitted by the client are always rejected.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.auth import get_provisioning_user
from app.db.models import AdminUser, Tenant
from app.db.session import get_db
from app.i18n_assets import I18N_SCRIPT_TAG
from app.services.tenant_config_service import (
    TenantConfigBindingMissingError,
    TenantConfigValidationError,
    apply_config_updates,
    config_snapshot,
    test_config,
)
from app.web import render_template
from app.web.sidenav import render_provisioning_sidenav

router = APIRouter()


class ProvisioningConfigUpdateIn(BaseModel):
    """Allowed wizard fields only. Any other key (tenant_id, corp_id, ...) is a
    422 contract violation."""

    model_config = ConfigDict(extra="forbid")

    archive_secret: Optional[str] = None
    private_key: Optional[str] = None
    publickey_version: Optional[int] = None
    callback_token: Optional[str] = None
    callback_encoding_aes_key: Optional[str] = None


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
            "provisioning_settings",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=render_provisioning_sidenav("settings"),
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


@router.get("/api/provisioning/config")
def get_provisioning_config(
    context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
    db: Session = Depends(get_db),
) -> dict:
    """Masked, tenant-scoped snapshot of the archive configuration."""
    _user, tenant = context
    return config_snapshot(db, tenant.id)


@router.put("/api/provisioning/config")
def put_provisioning_config(
    payload: ProvisioningConfigUpdateIn,
    context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
    db: Session = Depends(get_db),
) -> dict:
    """Persist wizard fields; blank values never overwrite existing secrets."""
    _user, tenant = context
    try:
        return apply_config_updates(db, tenant.id, payload.model_dump())
    except TenantConfigValidationError as error:
        return JSONResponse(
            status_code=400,
            content={"errors": error.errors},
        )
    except TenantConfigBindingMissingError as error:
        raise HTTPException(
            status_code=409, detail="organization_binding_missing"
        ) from error


@router.post("/api/provisioning/config/test")
def test_provisioning_config(
    context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
    db: Session = Depends(get_db),
) -> dict:
    """Run local credential checks and a real WeCom connectivity probe."""
    _user, tenant = context
    return test_config(db, tenant.id)
