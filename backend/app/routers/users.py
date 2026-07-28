"""Tenant-scoped admin-user listing endpoints (RND-284)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db.models import AdminUser, ArchiveMessage
from app.db.session import get_db
from app.schemas.admin_users import AdminUserListItem, AdminUserListOut

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
