"""Platform-admin login page, session issuance, and logout (RND-413).

Browser flow:
- ``GET /platform/login`` renders the login page; already-authenticated
  callers are redirected straight to /platform/operations.
- ``POST /platform/login`` (JSON, mirroring the tenant password-login
  pattern) verifies credentials, issues a ``platform_session_id`` cookie
  backed by a ``platform_admin_sessions`` row, and returns
  ``{"logged_in": true}``; the page then navigates to /platform/operations.
- ``POST /platform/logout`` revokes the current session and returns to the
  login page.

API clients without a session cookie keep using HTTP Basic — their 401s
carry ``WWW-Authenticate: Basic`` (see ``require_platform_admin`` in
app.auth), so the browser native dialog still works for them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import (
    PLATFORM_SESSION_COOKIE,
    _is_production,
    create_platform_admin_session,
    require_platform_admin_optional,
    verify_platform_admin,
)
from app.db.models import PlatformAdminSession, Tenant
from app.db.session import get_db
from app.web import render_template

router = APIRouter(tags=["platform-auth"])

PLATFORM_LOGIN_ROUTE = "/platform/login"
PLATFORM_OPERATIONS_ROUTE = "/platform/operations"


def _audit_tenant_id(db: Session) -> str | None:
    """Platform actors are tenant-less; anchor their audit rows to the
    default tenant when it exists, else skip (write_audit is fail-safe and
    never raises)."""
    tenant = db.query(Tenant).filter(Tenant.slug == "default").first()
    return tenant.id if tenant is not None else None


@router.get(PLATFORM_LOGIN_ROUTE, response_class=HTMLResponse)
async def platform_login_page(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Login page; already-authenticated callers go straight to operations."""
    if await require_platform_admin_optional(request, db) is not None:
        return RedirectResponse(PLATFORM_OPERATIONS_ROUTE, status_code=302)
    return HTMLResponse(render_template("platform_login"))


class _PlatformLoginBody(BaseModel):
    email: str
    password: str


@router.post(PLATFORM_LOGIN_ROUTE, response_class=HTMLResponse)
def platform_login_submit(
    body: _PlatformLoginBody,
    db: Session = Depends(get_db),
):
    """Verify credentials, issue a platform session cookie.

    Failed attempts return 401 JSON with a generic detail (no
    account-enumeration oracle) and never set a cookie. Success and
    failure are both audited when the default tenant exists.
    """
    admin = verify_platform_admin(db, body.email, body.password)
    if admin is None:
        tenant_id = _audit_tenant_id(db)
        if tenant_id is not None:
            write_audit(
                db,
                tenant_id=tenant_id,
                action=AuditAction.LOGIN_FAILED,
                object_type=AuditObjectType.SESSION,
                detail={"platform_admin_id": None},
            )
            try:
                db.commit()
            except Exception:
                db.rollback()
        return JSONResponse({"detail": "invalid_credentials"}, status_code=401)

    session_id, ttl_hours = create_platform_admin_session(db, admin)
    tenant_id = _audit_tenant_id(db)
    if tenant_id is not None:
        write_audit(
            db,
            tenant_id=tenant_id,
            action=AuditAction.LOGIN,
            object_type=AuditObjectType.SESSION,
            detail={"platform_admin_id": admin.id},
        )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Login failed")

    response = JSONResponse({"logged_in": True})
    response.set_cookie(
        key=PLATFORM_SESSION_COOKIE,
        value=session_id,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
        max_age=ttl_hours * 3600,
    )
    return response


@router.post("/platform/logout")
def platform_logout(request: Request, db: Session = Depends(get_db)):
    """Revoke the current platform session and return to the login page."""
    session_id = request.cookies.get(PLATFORM_SESSION_COOKIE)
    if session_id:
        row = (
            db.query(PlatformAdminSession)
            .filter(PlatformAdminSession.id == session_id)
            .first()
        )
        if row is not None:
            row.is_revoked = True
            tenant_id = _audit_tenant_id(db)
            if tenant_id is not None:
                write_audit(
                    db,
                    tenant_id=tenant_id,
                    action=AuditAction.LOGOUT,
                    object_type=AuditObjectType.SESSION,
                    detail={"platform_admin_id": row.platform_admin_id},
                )
            try:
                db.commit()
            except Exception:
                db.rollback()
    response = RedirectResponse(PLATFORM_LOGIN_ROUTE, status_code=302)
    response.delete_cookie(key=PLATFORM_SESSION_COOKIE, path="/")
    return response
