"""Authenticated, tenant-scoped controlled contact-avatar delivery (RND-371)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import AdminUser, Contact, ExternalContact
from app.db.session import get_db
from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageUnavailable,
    detect_image_type_from_bytes,
    get_media_storage_provider_for_backend,
)

router = APIRouter()
_ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def _not_found() -> HTTPException:
    # The same body for missing, inaccessible, disabled, and unavailable
    # profiles prevents cross-tenant/identity enumeration.
    return HTTPException(status_code=404, detail="Not found")


@router.get("/avatars/{identity_type}/{avatar_id}")
def get_contact_avatar(
    identity_type: str,
    request: Request,
    avatar_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    auth: tuple[AdminUser, str] = Depends(require_role()),
):
    """Serve only cached avatar bytes after authentication and tenant scoping.

    ``avatar_id`` is the database-local profile PK, not a WeCom identity or
    external URL. It is still fully tenant-scoped below; an ID from another
    tenant is indistinguishable from an absent image.
    """
    _, tenant_id = auth
    if identity_type == "internal":
        profile = (
            db.query(Contact)
            .filter(Contact.id == avatar_id, Contact.tenant_id == tenant_id)
            .first()
        )
        if profile is not None:
            disabled = (
                db.query(AdminUser.id)
                .filter(
                    AdminUser.tenant_id == tenant_id,
                    AdminUser.wecom_user_id == profile.wecom_userid,
                    AdminUser.status != "active",
                )
                .first()
            )
            if disabled is not None:
                raise _not_found()
    elif identity_type == "external":
        profile = (
            db.query(ExternalContact)
            .filter(ExternalContact.id == avatar_id, ExternalContact.tenant_id == tenant_id)
            .first()
        )
    else:
        raise _not_found()

    if (
        profile is None
        or profile.avatar_status != "ready"
        or not profile.avatar_storage_backend
        or not profile.avatar_storage_ref
        or profile.avatar_content_type not in _ALLOWED_CONTENT_TYPES
    ):
        raise _not_found()

    # Copy every value used below before ending the shared read transaction.
    # SQLAlchemy expires ORM state on rollback, so no profile attribute may be
    # accessed once the storage call starts.
    profile_id = profile.id
    synced_at = profile.avatar_synced_at
    storage_backend = profile.avatar_storage_backend
    storage_ref = profile.avatar_storage_ref
    content_type = profile.avatar_content_type
    version = int(synced_at.timestamp() * 1_000_000) if synced_at else 0
    etag = f'"avatar-{identity_type}-{profile_id}-{version}"'
    cache_headers = {
        # Always revalidate: a suspended account, tenant change, or deleted
        # contact must be re-authorized before a private browser cache is used.
        "Cache-Control": "private, no-cache, max-age=0",
        "ETag": etag,
        "Vary": "Cookie",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)

    # FastAPI caches the shared get_db dependency within this request, so
    # require_role/get_current_user and this route use the same Session. The
    # profile lookup has completed; roll back its read transaction now to
    # return that connection before any potentially slow object-store access.
    db.rollback()
    try:
        content = get_media_storage_provider_for_backend(storage_backend).read_bytes(
            storage_ref
        )
    except (
        MediaObjectNotFound,
        MediaStorageConfigurationError,
        MediaStorageOperationError,
        MediaStorageUnavailable,
        OSError,
        ValueError,
    ):
        # An object-store error must be a normal image fallback, never a 500
        # or a provider URL/credential leak.
        raise _not_found()
    expected_suffix = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }[content_type]
    if len(content) > 2 * 1024 * 1024 or detect_image_type_from_bytes(content) != expected_suffix:
        raise _not_found()
    return Response(
        content=content,
        media_type=content_type,
        headers=cache_headers,
    )
