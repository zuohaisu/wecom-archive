"""HTTP boundary for isolated WeCom organization authorization (RND-346)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from html import escape
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, _is_production
from app.crypto import (
    FieldEncryptionConfigurationError,
    validate_field_encryption_configuration,
)
from app.db.session import get_db
from app.services.wecom_org_authorization import (
    WecomAuthorizationError,
    WecomOrganizationAuthorizationProvider,
    begin_authorization,
    complete_authorization,
    get_wecom_org_authorization_provider,
)
from app.services.wecom_organization_claims import (
    CLAIM_COOKIE,
    OrganizationAlreadyExists,
    OrganizationClaimError,
    cancel_browser_claim,
    create_claim_from_proof,
    get_browser_claim,
)
from app.services.organization_provisioning import (
    OrganizationProvisioningConflict,
    OrganizationProvisioningError,
    provision_organization,
)
from app.web import render_template

router = APIRouter()


def _get_configured_provider(db: Session) -> WecomOrganizationAuthorizationProvider:
    """Resolve all configuration before either route writes authorization state."""
    validate_field_encryption_configuration()
    return get_wecom_org_authorization_provider(db)


@router.get("/api/auth/wecom/third-party/install", response_class=RedirectResponse)
def start_wecom_organization_authorization(
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        provider = _get_configured_provider(db)
        return RedirectResponse(begin_authorization(db, provider), status_code=302)
    except (FieldEncryptionConfigurationError, WecomAuthorizationError):
        db.rollback()
        return RedirectResponse("/admin/login?error=config_error", status_code=302)


@router.get("/api/auth/wecom/third-party/callback", response_class=RedirectResponse)
def finish_wecom_organization_authorization(
    code: Annotated[str, Query(min_length=1)],
    state: Annotated[str, Query(min_length=1)],
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        provider = _get_configured_provider(db)
    except (FieldEncryptionConfigurationError, WecomAuthorizationError):
        db.rollback()
        return RedirectResponse("/admin/login?error=config_error", status_code=302)

    try:
        browser_token = complete_authorization(
            db, provider, state=state, authorization_code=code
        )
        claim_ref = create_claim_from_proof(db, browser_token)
    except FieldEncryptionConfigurationError:
        db.rollback()
        return RedirectResponse("/admin/login?error=config_error", status_code=302)
    except OrganizationAlreadyExists:
        db.rollback()
        return RedirectResponse("/admin/login?error=organization_exists", status_code=302)
    except OrganizationClaimError:
        db.rollback()
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)
    except WecomAuthorizationError:
        db.rollback()
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)
    response = RedirectResponse("/admin/login?error=organization_not_found", status_code=302)
    response.set_cookie(
        CLAIM_COOKIE,
        claim_ref,
        max_age=600,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
    )
    return response


_CONFIRM_COPY = {
    "zh": {
        "title": "创建组织",
        "heading": "确认创建组织",
        "description": "请确认企业微信返回的官方企业名称。",
        "name": "企业名称",
        "readonly": "名称来自企业微信，无法在此修改。",
        "confirm": "确认创建",
        "cancel": "取消",
        "confirmed": "信息已确认，正在准备组织。",
    },
    "en": {
        "title": "Create organization",
        "heading": "Confirm organization creation",
        "description": "Confirm the official organization name returned by WeCom.",
        "name": "Organization name",
        "readonly": "This name comes from WeCom and cannot be edited here.",
        "confirm": "Create organization",
        "cancel": "Cancel",
        "confirmed": "Confirmed. Your organization is being prepared.",
    },
    "ja": {
        "title": "組織を作成",
        "heading": "組織の作成を確認",
        "description": "WeCom から取得した正式な企業名を確認してください。",
        "name": "企業名",
        "readonly": "企業名は WeCom から取得され、この画面では変更できません。",
        "confirm": "組織を作成",
        "cancel": "キャンセル",
        "confirmed": "確認済みです。組織を準備しています。",
    },
}


def _confirmation_locale(request: Request) -> str:
    accepted = request.headers.get("accept-language", "").lower()
    if accepted.startswith("ja"):
        return "ja"
    if accepted.startswith("en"):
        return "en"
    return "zh"


@router.get("/admin/organization/confirm", response_class=HTMLResponse)
def organization_confirmation_page(
    request: Request, db: Session = Depends(get_db)
):
    claim = get_browser_claim(db, request.cookies.get(CLAIM_COOKIE))
    if claim is None:
        return RedirectResponse("/admin/login", status_code=302)
    locale = _confirmation_locale(request)
    copy = _CONFIRM_COPY[locale]
    if claim.state == "confirmed":
        actions = f'<p role="status">{copy["confirmed"]}</p>'
    else:
        actions = (
            '<form method="post" action="/api/auth/wecom/organization-claim/confirm">'
            f'<button class="btn btn-primary btn-block" type="submit">{copy["confirm"]}</button>'
            '</form>'
            '<form class="mt-2" method="post" action="/api/auth/wecom/organization-claim/cancel">'
            f'<button class="btn btn-secondary btn-block" type="submit">{copy["cancel"]}</button>'
            '</form>'
        )
    return HTMLResponse(
        render_template(
            "organization_confirm",
            lang=locale,
            page_title=copy["title"],
            heading=copy["heading"],
            description=copy["description"],
            name_label=copy["name"],
            corp_name=escape(claim.corp_name),
            read_only_help=copy["readonly"],
            actions=actions,
        )
    )


@router.post("/api/auth/wecom/organization-claim/confirm", response_class=RedirectResponse)
def confirm_organization_claim(
    request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    try:
        result = provision_organization(db, request.cookies.get(CLAIM_COOKIE))
    except OrganizationProvisioningConflict:
        db.rollback()
        return RedirectResponse("/admin/login?error=organization_exists", status_code=303)
    except OrganizationProvisioningError:
        db.rollback()
        return RedirectResponse("/admin/login?error=auth_failed", status_code=303)
    response = RedirectResponse("/admin/provisioning", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        result.session_id,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
    )
    response.delete_cookie(CLAIM_COOKIE, path="/")
    return response


@router.post("/api/auth/wecom/organization-claim/cancel", response_class=RedirectResponse)
def cancel_organization_claim(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    cancel_browser_claim(db, request.cookies.get(CLAIM_COOKIE))
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(CLAIM_COOKIE, path="/")
    return response
