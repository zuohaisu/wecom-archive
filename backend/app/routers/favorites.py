"""HTTP adapter for the shared tenant-scoped favorites contract (RND-366)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import AdminUser
from app.db.session import get_db
from app.schemas.favorites import (
    FavoriteBatchIn,
    FavoriteMutationIn,
    FavoriteMutationOut,
    FavoritePageOut,
    FavoriteStatusIn,
    FavoriteStatusOut,
)
from app.services.favorites import (
    FavoriteAuditUnavailable,
    FavoriteWriteUnavailable,
    get_favorite_statuses,
    list_favorites,
    mutate_favorite,
    mutate_favorites,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/favorites", tags=["favorites"])


def _validate_identity(user: AdminUser, tenant_id: str) -> None:
    """Fail closed if a malformed session pairs a user with another tenant."""
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant_scope_mismatch")


def _commit_mutation(db: Session) -> None:
    try:
        db.commit()
    except Exception as exc:  # fail closed; DBAPI text may contain bound identifiers
        db.rollback()
        logger.error("favorite transaction failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="favorite_write_unavailable") from None


def _mutate_or_503(db: Session, callable_):
    try:
        return callable_()
    except FavoriteAuditUnavailable:
        db.rollback()
        raise HTTPException(status_code=503, detail="favorite_audit_unavailable") from None
    except FavoriteWriteUnavailable:
        db.rollback()
        raise HTTPException(status_code=503, detail="favorite_write_unavailable") from None
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("favorite database operation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="favorite_write_unavailable") from None


@router.post("", response_model=FavoriteMutationOut)
def add_favorite(
    payload: FavoriteMutationIn,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(
        require_role("owner", "admin", "compliance", "legal")
    ),
) -> FavoriteMutationOut:
    user, tenant_id = auth
    _validate_identity(user, tenant_id)
    result = _mutate_or_503(
        db,
        lambda: mutate_favorite(
            db,
            tenant_id=tenant_id,
            actor_id=user.id,
            payload=payload,
            action="favorite",
        )
    )
    _commit_mutation(db)
    if result.not_found:
        raise HTTPException(status_code=404, detail="object_not_found")
    return result


@router.post("/batch", response_model=FavoriteMutationOut)
def mutate_favorite_batch(
    payload: FavoriteBatchIn,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(
        require_role("owner", "admin", "compliance", "legal")
    ),
) -> FavoriteMutationOut:
    user, tenant_id = auth
    _validate_identity(user, tenant_id)
    result = _mutate_or_503(
        db,
        lambda: mutate_favorites(
            db, tenant_id=tenant_id, actor_id=user.id, payload=payload
        )
    )
    _commit_mutation(db)
    return result


@router.delete("/{object_type}/{object_id}", response_model=FavoriteMutationOut)
def remove_favorite(
    object_type: str,
    object_id: str,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(
        require_role("owner", "admin", "compliance", "legal")
    ),
) -> FavoriteMutationOut:
    user, tenant_id = auth
    _validate_identity(user, tenant_id)
    try:
        payload = FavoriteMutationIn(object_type=object_type, object_id=object_id)
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid_favorite_target") from None
    result = _mutate_or_503(
        db,
        lambda: mutate_favorite(
            db,
            tenant_id=tenant_id,
            actor_id=user.id,
            payload=payload,
            action="unfavorite",
        )
    )
    _commit_mutation(db)
    if result.not_found:
        raise HTTPException(status_code=404, detail="object_not_found")
    return result


@router.post("/status", response_model=FavoriteStatusOut)
def favorite_status(
    payload: FavoriteStatusIn,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role()),
) -> FavoriteStatusOut:
    user, tenant_id = auth
    _validate_identity(user, tenant_id)
    return get_favorite_statuses(db, tenant_id=tenant_id, payload=payload)


@router.get("", response_model=FavoritePageOut)
def get_favorites(
    object_type: Optional[str] = Query(None, pattern="^(message|media)$"),
    conversation_id: Optional[str] = Query(None, min_length=1, max_length=128),
    mode: Optional[str] = Query(None, pattern="^(staff|contact)$"),
    staff_id: Optional[str] = Query(None, min_length=1, max_length=64),
    contact_id: Optional[str] = Query(None, min_length=1, max_length=64),
    favorited_by: Optional[str] = Query(None, min_length=1, max_length=36),
    favorited_since: Optional[datetime] = Query(None),
    favorited_until: Optional[datetime] = Query(None),
    message_since_ms: Optional[int] = Query(None, ge=0),
    message_until_ms: Optional[int] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role()),
) -> FavoritePageOut:
    user, tenant_id = auth
    _validate_identity(user, tenant_id)
    for value in (favorited_since, favorited_until):
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail="timezone_required")
    if (mode or staff_id) and not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_context_requires_id")
    if mode == "staff" and not staff_id:
        raise HTTPException(status_code=422, detail="staff_id_required")
    if mode == "contact" and not contact_id:
        raise HTTPException(status_code=422, detail="contact_id_required")
    if staff_id and mode != "staff":
        raise HTTPException(status_code=422, detail="staff_id_requires_staff_mode")
    try:
        return list_favorites(
            db,
            tenant_id=tenant_id,
            limit=limit,
            offset=offset,
            object_type=object_type,
            conversation_id=conversation_id,
            conversation_mode=mode,
            conversation_entity_id=(staff_id if mode == "staff" else contact_id),
            contact_id=contact_id,
            favorited_by=favorited_by,
            favorited_since=(favorited_since.astimezone(timezone.utc) if favorited_since else None),
            favorited_until=(favorited_until.astimezone(timezone.utc) if favorited_until else None),
            message_since_ms=message_since_ms,
            message_until_ms=message_until_ms,
        )
    except ValueError as exc:
        code = str(exc)
        if code not in {
            "invalid_conversation",
            "invalid_favorite_time_range",
            "invalid_message_time_range",
        }:
            code = "invalid_favorite_filter"
        raise HTTPException(status_code=422, detail=code) from None
