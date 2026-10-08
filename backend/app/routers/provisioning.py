"""Restricted self-service surface for provisioning tenants (RND-348 / RND-386).

Only a provisioning-scoped owner session may reach these routes. The tenant
scope comes exclusively from the authenticated session; tenant_id, corp_id and
lifecycle values submitted by the client are always rejected.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.auth import get_provisioning_user
from app.db.models import AdminUser, Tenant
from app.db.session import get_db
from app.i18n_assets import I18N_SCRIPT_TAG
from app.services.archive_worker_trigger import (
    ArchiveWorkerDispatch,
    dispatch_archive_worker,
)
from app.settings import APP_EDITION_SELFHOST, get_app_edition
from app.services.tenant_activation import (
    activate_tenant,
    activation_status,
    spawn_activation_worker,
)
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
cloud_activation_router = APIRouter()


class ProvisioningConfigUpdateIn(BaseModel):
    """Allowed wizard fields only. Any other key (tenant_id, corp_id, ...) is a
    422 contract violation."""

    model_config = ConfigDict(extra="forbid")

    archive_secret: Optional[str] = None
    private_key: Optional[str] = None
    publickey_version: Optional[int] = None
    callback_token: Optional[str] = None
    callback_encoding_aes_key: Optional[str] = None


@router.get("/admin/provisioning", response_class=HTMLResponse, response_model=None)
def provisioning_waiting(
    _context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
) -> HTMLResponse | RedirectResponse:
    if get_app_edition() == APP_EDITION_SELFHOST:
        return RedirectResponse("/admin/provisioning/settings", status_code=302)
    return HTMLResponse(
        render_template(
            "provisioning",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=render_provisioning_sidenav("organization"),
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
    context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
    db: Session = Depends(get_db),
) -> dict:
    """Onboarding status: lifecycle + the persisted activation-check state
    machine (RND-388).  ``activate`` appears in allowed_actions only when the
    gates already passed and the tenant is waiting for promotion."""
    _user, tenant = context
    if get_app_edition() == APP_EDITION_SELFHOST:
        return {
            "lifecycle_status": tenant.lifecycle_status,
            "archive_enabled": tenant.lifecycle_status in {"active", "frozen"},
            "activation": {
                "state": "active",
                "gate_results": {},
                "safe_error_code": None,
                "revision": 0,
            },
            "allowed_actions": ["view_status", "view_settings"],
        }
    snapshot = activation_status(db, tenant.id)
    allowed_actions = ["view_status", "purchase_plan", "view_settings"]
    if snapshot.state == "ready":
        allowed_actions.append("activate")
    return {
        "lifecycle_status": tenant.lifecycle_status,
        "archive_enabled": tenant.lifecycle_status == "active",
        "activation": {
            "state": snapshot.state,
            "gate_results": snapshot.gate_results,
            "safe_error_code": snapshot.safe_error_code,
            "revision": snapshot.revision,
        },
        "allowed_actions": allowed_actions,
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
        result = apply_config_updates(db, tenant.id, payload.model_dump())
    except TenantConfigValidationError as error:
        return JSONResponse(
            status_code=400,
            content={"errors": error.errors},
        )
    except TenantConfigBindingMissingError as error:
        raise HTTPException(
            status_code=409, detail="organization_binding_missing"
        ) from error
    # Cloud-only trial activation; selfhost uses the same tenant-scoped S2
    # wizard without creating or mutating subscription state.
    if get_app_edition() != APP_EDITION_SELFHOST:
        spawn_activation_worker(db.get_bind(), tenant.id, actor="self_service")
    return result


@router.post("/api/provisioning/config/test")
def test_provisioning_config(
    context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
    db: Session = Depends(get_db),
) -> dict:
    """Run local credential checks and a real WeCom connectivity probe."""
    _user, tenant = context
    result = test_config(db, tenant.id)
    # A successful cloud probe may trigger trial activation. Selfhost only
    # receives the credential-check result; it has no activation state.
    if get_app_edition() != APP_EDITION_SELFHOST:
        spawn_activation_worker(db.get_bind(), tenant.id, actor="self_service")
    return result


@cloud_activation_router.post("/api/provisioning/activate")
def activate_provisioning_tenant(
    context: tuple[AdminUser, Tenant] = Depends(get_provisioning_user),
    db: Session = Depends(get_db),
) -> dict:
    """Manual retry entry for a ready or blocked tenant (RND-388).

    Idempotent: a replaying or blocked attempt never double-grants a trial,
    double-audits or double-dispatches the worker.  The worker dispatch
    happens strictly after the promotion commit, with the "activation"
    trigger source the T2 worker chain already understands.
    """
    _user, tenant = context
    result = activate_tenant(db, tenant.id, actor="self_service")
    dispatch: ArchiveWorkerDispatch | None = None
    if result.activated:
        dispatch = dispatch_archive_worker(
            trigger_source="activation", tenant_id=tenant.id
        )
    return {
        "activated": result.activated,
        "replayed": result.replayed,
        "activation": {
            "state": result.snapshot.state,
            "gate_results": result.snapshot.gate_results,
            "safe_error_code": result.snapshot.safe_error_code,
            "revision": result.snapshot.revision,
        },
        "worker_dispatch": dispatch.value if dispatch is not None else None,
    }
