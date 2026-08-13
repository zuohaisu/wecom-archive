"""Paid tenant branding controls and public, host-scoped brand assets (RND-259)."""

from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import SESSION_COOKIE, require_role
from app.db.models import AdminUser
from app.db.session import get_db
from app.schemas.branding import BrandingStatusOut, DomainConfigureIn
from app.services.branding import (
    CUSTOM_BRANDING,
    CUSTOM_DOMAIN,
    BrandingValidationError,
    DomainConflictError,
    DomainStateError,
    branding_status,
    clear_favicon,
    clear_logo,
    configure_domain,
    disable_domain,
    domain_fingerprint,
    effective_favicon_payload,
    effective_logo_payload,
    enable_domain,
    set_favicon,
    set_logo,
    tenant_for_active_session,
    unbind_domain,
    validate_favicon_upload,
    validate_logo_upload,
    verify_domain_ownership,
)
from app.services.entitlements import has_entitlement

router = APIRouter(prefix="/api/branding", tags=["branding"])


def _require_capability(db: Session, tenant_id: str, capability: str) -> None:
    if not has_entitlement(db, tenant_id, capability):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"{capability}_upgrade_required")


def _owner_or_admin(
    auth: Tuple[AdminUser, str] = Depends(require_role("owner", "admin")),
) -> Tuple[AdminUser, str]:
    return auth


async def _bounded_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        if not content_length.isdigit() or int(content_length) > maximum:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="branding_file_too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="branding_file_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _status_response(db: Session, tenant_id: str) -> BrandingStatusOut:
    return BrandingStatusOut(**branding_status(db, tenant_id))


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="custom_domain_already_bound") from exc


def _asset_response(mime_type: str, content: bytes) -> Response:
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            # The asset may depend on a session on the platform hostname;
            # never permit a shared/CDN cache to reuse it for another tenant.
            "Cache-Control": "private, no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


def _asset_tenant_id(request: Request, db: Session) -> str | None:
    custom_domain_tenant_id = getattr(request.state, "branding_custom_domain_tenant_id", None)
    if custom_domain_tenant_id:
        return custom_domain_tenant_id
    return tenant_for_active_session(db, request.cookies.get(SESSION_COOKIE))


@router.get("", response_model=BrandingStatusOut)
def get_branding_status(
    auth: Tuple[AdminUser, str] = Depends(require_role()),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    """Show only current tenant state and feature eligibility, never a TXT value."""
    _user, tenant_id = auth
    return _status_response(db, tenant_id)


@router.get("/logo", include_in_schema=False)
def get_effective_logo(request: Request, db: Session = Depends(get_db)) -> Response:
    """Return the host/session-scoped logo or a stable platform fallback."""
    mime_type, content = effective_logo_payload(db, _asset_tenant_id(request, db))
    return _asset_response(mime_type, content)


@router.get("/favicon", include_in_schema=False)
def get_effective_favicon(request: Request, db: Session = Depends(get_db)) -> Response:
    """Favicon is optional; absent config deliberately falls back to platform art."""
    mime_type, content = effective_favicon_payload(db, _asset_tenant_id(request, db))
    return _asset_response(mime_type, content)


@router.get("/manifest.webmanifest", include_in_schema=False)
def branding_manifest() -> JSONResponse:
    """Avoid a static manifest pinning a custom-host tab to platform icons."""
    return JSONResponse(
        {
            "name": "Crowntime WeCom Archive",
            "short_name": "Archive",
            "display": "standalone",
            "start_url": "/",
            "icons": [{"src": "/api/branding/favicon", "sizes": "any"}],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "private, no-store, max-age=0"},
    )


@router.put("/logo", response_model=BrandingStatusOut)
async def upload_logo(
    request: Request,
    auth: Tuple[AdminUser, str] = Depends(_owner_or_admin),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    user, tenant_id = auth
    _require_capability(db, tenant_id, CUSTOM_BRANDING)
    try:
        payload = validate_logo_upload(
            await _bounded_body(request, 2 * 1024 * 1024),
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower(),
        )
    except BrandingValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    config = set_logo(db, tenant_id, payload)
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        action=AuditAction.BRANDING_LOGO_UPLOADED,
        object_type=AuditObjectType.TENANT_CONFIG,
        object_id=config.id,
        detail={"mime_type": payload.mime_type, "width": payload.width, "height": payload.height},
    )
    db.commit()
    return _status_response(db, tenant_id)


@router.delete("/logo", response_model=BrandingStatusOut)
def restore_default_logo(
    auth: Tuple[AdminUser, str] = Depends(_owner_or_admin),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    user, tenant_id = auth
    _require_capability(db, tenant_id, CUSTOM_BRANDING)
    config = clear_logo(db, tenant_id)
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        action=AuditAction.BRANDING_LOGO_RESTORED,
        object_type=AuditObjectType.TENANT_CONFIG,
        object_id=config.id,
    )
    db.commit()
    return _status_response(db, tenant_id)


@router.put("/favicon", response_model=BrandingStatusOut)
async def upload_favicon(
    request: Request,
    auth: Tuple[AdminUser, str] = Depends(_owner_or_admin),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    user, tenant_id = auth
    _require_capability(db, tenant_id, CUSTOM_BRANDING)
    try:
        payload = validate_favicon_upload(
            await _bounded_body(request, 1024 * 1024),
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower(),
        )
    except BrandingValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    config = set_favicon(db, tenant_id, payload)
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        action=AuditAction.BRANDING_FAVICON_UPLOADED,
        object_type=AuditObjectType.TENANT_CONFIG,
        object_id=config.id,
        detail={"mime_type": payload.mime_type, "width": payload.width, "height": payload.height},
    )
    db.commit()
    return _status_response(db, tenant_id)


@router.delete("/favicon", response_model=BrandingStatusOut)
def restore_default_favicon(
    auth: Tuple[AdminUser, str] = Depends(_owner_or_admin),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    user, tenant_id = auth
    _require_capability(db, tenant_id, CUSTOM_BRANDING)
    config = clear_favicon(db, tenant_id)
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        action=AuditAction.BRANDING_FAVICON_RESTORED,
        object_type=AuditObjectType.TENANT_CONFIG,
        object_id=config.id,
    )
    db.commit()
    return _status_response(db, tenant_id)


@router.post("/domain")
def save_domain(
    body: DomainConfigureIn,
    auth: Tuple[AdminUser, str] = Depends(_owner_or_admin),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Start DNS ownership verification and return its raw value exactly once."""
    user, tenant_id = auth
    _require_capability(db, tenant_id, CUSTOM_DOMAIN)
    try:
        config, raw_token = configure_domain(db, tenant_id, body.hostname)
        write_audit(
            db,
            tenant_id=tenant_id,
            admin_user_id=user.id,
            action=AuditAction.BRANDING_DOMAIN_CONFIGURED,
            object_type=AuditObjectType.TENANT_CONFIG,
            object_id=config.id,
            detail={"domain_ref": domain_fingerprint(config.custom_domain or "")},
        )
        _commit_or_conflict(db)
    except (BrandingValidationError, DomainConflictError) as exc:
        db.rollback()
        detail = str(exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT if detail == "custom_domain_already_bound" else status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        ) from exc
    result = _status_response(db, tenant_id).model_dump(mode="json")
    # The value is never added to AuditLog and is deliberately absent from GET.
    result["verification_token"] = raw_token
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/domain/verify", response_model=BrandingStatusOut)
def verify_domain(
    auth: Tuple[AdminUser, str] = Depends(_owner_or_admin),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    user, tenant_id = auth
    _require_capability(db, tenant_id, CUSTOM_DOMAIN)
    try:
        config, dns_check = verify_domain_ownership(db, tenant_id)
    except DomainStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if dns_check.verified:
        write_audit(
            db,
            tenant_id=tenant_id,
            admin_user_id=user.id,
            action=AuditAction.BRANDING_DOMAIN_VERIFIED,
            object_type=AuditObjectType.TENANT_CONFIG,
            object_id=config.id,
            detail={"domain_ref": domain_fingerprint(config.custom_domain or "")},
        )
    db.commit()
    if not dns_check.verified:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=dns_check.reason or "dns_verification_failed")
    return _status_response(db, tenant_id)


@router.post("/domain/enable", response_model=BrandingStatusOut)
def activate_domain(
    auth: Tuple[AdminUser, str] = Depends(_owner_or_admin),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    user, tenant_id = auth
    _require_capability(db, tenant_id, CUSTOM_DOMAIN)
    try:
        config = enable_domain(db, tenant_id)
    except DomainStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        action=AuditAction.BRANDING_DOMAIN_ENABLED,
        object_type=AuditObjectType.TENANT_CONFIG,
        object_id=config.id,
        detail={"domain_ref": domain_fingerprint(config.custom_domain or "")},
    )
    db.commit()
    return _status_response(db, tenant_id)


@router.post("/domain/disable", response_model=BrandingStatusOut)
def deactivate_domain(
    auth: Tuple[AdminUser, str] = Depends(_owner_or_admin),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    user, tenant_id = auth
    _require_capability(db, tenant_id, CUSTOM_DOMAIN)
    try:
        config = disable_domain(db, tenant_id)
    except DomainStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        action=AuditAction.BRANDING_DOMAIN_DISABLED,
        object_type=AuditObjectType.TENANT_CONFIG,
        object_id=config.id,
        detail={"domain_ref": domain_fingerprint(config.custom_domain or "")},
    )
    db.commit()
    return _status_response(db, tenant_id)


@router.delete("/domain", response_model=BrandingStatusOut)
def remove_domain(
    auth: Tuple[AdminUser, str] = Depends(_owner_or_admin),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    user, tenant_id = auth
    _require_capability(db, tenant_id, CUSTOM_DOMAIN)
    try:
        config = unbind_domain(db, tenant_id)
    except DomainStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        action=AuditAction.BRANDING_DOMAIN_UNBOUND,
        object_type=AuditObjectType.TENANT_CONFIG,
        object_id=config.id,
    )
    db.commit()
    return _status_response(db, tenant_id)
