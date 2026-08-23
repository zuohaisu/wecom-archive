"""Platform operator self-service pages and APIs (RND-415)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    PLATFORM_SESSION_COOKIE,
    require_platform_admin,
    require_platform_admin_optional,
)
from app.db.models import PlatformAdmin
from app.db.session import get_db
from app.schemas.platform_accounts import (
    PlatformOperatorInviteAcceptIn,
    PlatformOperatorInviteIn,
    PlatformOperatorInviteOut,
    PlatformOperatorListOut,
    PlatformPasswordChangeIn,
    PlatformPasswordChangeOut,
)
from app.services.platform_accounts import (
    PlatformAccountError,
    accept_platform_operator_invitation,
    change_platform_password,
    invite_platform_operator,
    list_platform_operators,
)
from app.web import render_template
from app.web.sidenav import (
    render_platform_admin_bar,
    render_platform_sidenav,
    render_platform_topbar,
)

router = APIRouter(tags=["platform-accounts"])


def _raise_account_error(error: PlatformAccountError) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.code) from error


def _render_platform_page(template: str, breadcrumb: str, admin: PlatformAdmin) -> HTMLResponse:
    return HTMLResponse(
        render_template(
            template,
            sidenav=render_platform_sidenav("settings"),
            admin_bar=render_platform_admin_bar(admin.email),
            topbar=render_platform_topbar(breadcrumb),
        )
    )


@router.get("/platform/settings", response_class=HTMLResponse)
async def platform_settings_page(
    admin: Optional[PlatformAdmin] = Depends(require_platform_admin_optional),
) -> HTMLResponse:
    """Account password controls and the read-only operator directory."""

    if admin is None:
        return RedirectResponse("/platform/login", status_code=302)
    return _render_platform_page("platform_settings", "账户与安全", admin)


@router.get("/platform/settings/operators/new", response_class=HTMLResponse)
async def platform_operator_new_page(
    admin: Optional[PlatformAdmin] = Depends(require_platform_admin_optional),
) -> HTMLResponse:
    """Controlled invitation wizard; it never accepts an initial password."""

    if admin is None:
        return RedirectResponse("/platform/login", status_code=302)
    return _render_platform_page("platform_operator_new", "新增操作员", admin)


@router.get("/platform/accept-invite", response_class=HTMLResponse)
def platform_operator_accept_page() -> HTMLResponse:
    """Public activation shell. Its bearer token remains only in browser JS."""

    return HTMLResponse(render_template("platform_operator_accept"))


@router.get("/api/platform/operators", response_model=PlatformOperatorListOut)
def platform_operator_list(
    _admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformOperatorListOut:
    return PlatformOperatorListOut(operators=list_platform_operators(db))


@router.post(
    "/api/platform/operators/invitations",
    response_model=PlatformOperatorInviteOut,
    status_code=status.HTTP_201_CREATED,
)
def platform_operator_invite(
    payload: PlatformOperatorInviteIn,
    admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformOperatorInviteOut:
    try:
        return PlatformOperatorInviteOut(
            **invite_platform_operator(
                db,
                actor=admin,
                name=payload.name,
                email=payload.email,
                reason_code=payload.reason_code,
                note=payload.note,
            )
        )
    except PlatformAccountError as error:
        _raise_account_error(error)


@router.post("/api/platform/operators/accept-invite")
def platform_operator_accept(
    payload: PlatformOperatorInviteAcceptIn,
    db: Session = Depends(get_db),
) -> JSONResponse:
    try:
        accept_platform_operator_invitation(
            db,
            token=payload.token,
            password=payload.password,
            password_confirmation=payload.password_confirmation,
        )
    except PlatformAccountError as error:
        _raise_account_error(error)
    return JSONResponse({"ok": True})


@router.post("/api/platform/account/password", response_model=PlatformPasswordChangeOut)
def platform_password_change(
    payload: PlatformPasswordChangeIn,
    request: Request,
    admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformPasswordChangeOut:
    try:
        revoked = change_platform_password(
            db,
            actor=admin,
            current_password=payload.current_password,
            new_password=payload.new_password,
            new_password_confirmation=payload.new_password_confirmation,
            revoke_other_sessions=payload.revoke_other_sessions,
            reason_code=payload.reason_code,
            note=payload.note,
            current_session_id=request.cookies.get(PLATFORM_SESSION_COOKIE),
        )
    except PlatformAccountError as error:
        _raise_account_error(error)
    return PlatformPasswordChangeOut(ok=True, revoked_other_sessions=revoked)
