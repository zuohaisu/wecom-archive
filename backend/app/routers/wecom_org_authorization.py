"""HTTP boundary for isolated WeCom organization authorization (RND-346)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import _is_production
from app.db.session import get_db
from app.services.wecom_org_authorization import (
    PROOF_COOKIE,
    WecomAuthorizationError,
    WecomOrganizationAuthorizationProvider,
    begin_authorization,
    complete_authorization,
    get_wecom_org_authorization_provider,
)

router = APIRouter()


@router.get("/api/auth/wecom/third-party/install", response_class=RedirectResponse)
def start_wecom_organization_authorization(
    db: Session = Depends(get_db),
    provider: WecomOrganizationAuthorizationProvider = Depends(get_wecom_org_authorization_provider),
) -> RedirectResponse:
    try:
        return RedirectResponse(begin_authorization(db, provider), status_code=302)
    except WecomAuthorizationError:
        db.rollback()
        return RedirectResponse("/admin/login?error=config_error", status_code=302)


@router.get("/api/auth/wecom/third-party/callback", response_class=RedirectResponse)
def finish_wecom_organization_authorization(
    code: Annotated[str, Query(min_length=1)],
    state: Annotated[str, Query(min_length=1)],
    db: Session = Depends(get_db),
    provider: WecomOrganizationAuthorizationProvider = Depends(get_wecom_org_authorization_provider),
) -> RedirectResponse:
    try:
        browser_token = complete_authorization(
            db, provider, state=state, authorization_code=code
        )
    except WecomAuthorizationError:
        db.rollback()
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)
    response = RedirectResponse("/admin/login", status_code=302)
    response.set_cookie(
        PROOF_COOKIE,
        browser_token,
        max_age=600,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
    )
    return response
