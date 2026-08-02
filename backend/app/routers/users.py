"""Tenant-scoped admin-user listing endpoints (RND-284)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import create_password_reset_token, get_current_user, require_role
from app.db.models import AdminSession, AdminUser, ArchiveMessage
from app.db.session import get_db
from app.email import send_password_reset_email
from app.schemas.admin_users import AdminUserListItem, AdminUserListOut
from app.settings import get_email_settings, get_wecom_oauth_settings

users_router = APIRouter()


@users_router.get("/users", response_model=AdminUserListOut)
def list_admin_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    role: Optional[str] = Query(
        None, pattern="^(owner|admin|compliance|legal|readonlyaudit)$"
    ),
    status: Optional[str] = Query(None, pattern="^(active|disabled)$"),
    q: Optional[str] = Query(None, max_length=128),
    silent_days: Optional[int] = Query(None, ge=1),
    auth: tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AdminUserListOut:
    """Return users belonging only to the authenticated user's tenant."""
    _, tenant_id = auth
    statement = db.query(AdminUser).filter(AdminUser.tenant_id == tenant_id)

    if role:
        statement = statement.filter(AdminUser.role == role)
    if status:
        statement = statement.filter(AdminUser.status == status)
    if silent_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=silent_days)
        statement = statement.filter(AdminUser.last_active_at < cutoff)
    if q:
        like = f"%{q}%"
        statement = statement.filter(
            or_(
                AdminUser.name.ilike(like),
                AdminUser.wecom_user_id.ilike(like),
                AdminUser.email.ilike(like),
                AdminUser.department.ilike(like),
            )
        )

    total = statement.with_entities(func.count(AdminUser.id)).scalar() or 0
    rows = (
        statement.order_by(
            AdminUser.last_active_at.desc().nullslast(), AdminUser.name.asc()
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    # RND-284: ``msg_count_30d`` is the count of archive messages sent by the
    # employee (``sender == wecom_user_id``), not received messages. This only
    # counts row ids and deliberately never reads message payload/content fields.
    cutoff_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    )
    aggregates = (
        db.query(ArchiveMessage.sender, func.count(ArchiveMessage.id))
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime >= cutoff_ms,
        )
        .group_by(ArchiveMessage.sender)
        .all()
    )
    msg_counts = {sender: count for sender, count in aggregates}

    return AdminUserListOut(
        items=[
            AdminUserListItem(
                id=user.id,
                name=user.name,
                wecom_user_id=user.wecom_user_id,
                email=user.email,
                department=user.department,
                role=user.role,
                status=user.status,
                last_active_at=(
                    user.last_active_at.isoformat() if user.last_active_at else None
                ),
                msg_count_30d=msg_counts.get(user.wecom_user_id, 0),
            )
            for user in rows
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


class _StatusUpdate(BaseModel):
    status: str


def _resolve_target(db: Session, user_id: str, tenant_id: str) -> AdminUser:
    """Look up a target within the caller's tenant without account enumeration."""
    user = (
        db.query(AdminUser)
        .filter(AdminUser.id == user_id, AdminUser.tenant_id == tenant_id)
        .first()
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _user_dto(user: AdminUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "status": user.status,
    }


@users_router.patch("/users/{user_id}")
def update_user_status(
    user_id: str,
    body: _StatusUpdate,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
):
    """Enable or disable a user within the caller's tenant."""
    if body.status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="invalid_status")
    current_user, tenant_id = auth
    target = _resolve_target(db, user_id, tenant_id)
    if target.id == current_user.id and body.status == "disabled":
        raise HTTPException(status_code=400, detail="cannot_disable_self")

    if target.status == body.status:
        return _user_dto(target)

    target.status = body.status
    if body.status == "disabled":
        now = datetime.now(timezone.utc)
        db.query(AdminSession).filter(
            AdminSession.admin_user_id == target.id,
            AdminSession.is_revoked.is_(False),
            AdminSession.expires_at > now,
        ).update({AdminSession.is_revoked: True})
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=current_user.id,
        action=AuditAction.USER_DISABLED if body.status == "disabled" else AuditAction.USER_ENABLED,
        object_type=AuditObjectType.USER,
        object_id=target.id,
    )
    db.commit()
    return _user_dto(target)


@users_router.post("/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: str,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
):
    """Email a one-time password-reset link to an active tenant user."""
    _, tenant_id = auth
    target = _resolve_target(db, user_id, tenant_id)
    if target.status != "active":
        raise HTTPException(status_code=409, detail="user_not_active")
    if not target.email:
        raise HTTPException(status_code=400, detail="user_has_no_email")

    email_settings = get_email_settings()
    try:
        ttl = int(email_settings.reset_token_ttl_hours or "1")
    except ValueError:
        ttl = 1
    raw_token = create_password_reset_token(db, target, ttl)
    base_url = email_settings.reset_base_url or get_wecom_oauth_settings().admin_domain
    reset_link = f"{base_url.rstrip('/')}/admin/reset-password?token={raw_token}"
    if not send_password_reset_email(target.email, reset_link):
        db.rollback()
        raise HTTPException(status_code=500, detail="email_delivery_failed")
    actor, _ = auth
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=actor.id,
        action=AuditAction.USER_PASSWORD_RESET_INITIATED,
        object_type=AuditObjectType.USER,
        object_id=target.id,
    )
    db.commit()
    return {"ok": True}
