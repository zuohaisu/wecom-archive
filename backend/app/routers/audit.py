"""Audit-log list / filter / pagination API (RND-295 / A7-3).

Read-only, role-gated, tenant-scoped. The audit trail is immutable at the
application layer (A7-1): this endpoint NEVER mutates rows — no create /
update / delete path exists. `tenant_id` comes only from the authenticated
session, never a request param.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import AdminUser, AuditLog
from app.db.session import get_db

router = APIRouter()


class AuditLogOut(BaseModel):
    id: str
    admin_user_id: Optional[str] = None
    actor_name: Optional[str] = None
    action: str
    object_type: str
    object_id: Optional[str] = None
    detail: Optional[dict] = None
    created_at: str


class AuditLogListOut(BaseModel):
    items: list[AuditLogOut]
    total: int
    limit: int
    offset: int
    has_more: bool


def _parse_ts(value: str) -> datetime:
    """ISO-8601 → tz-aware datetime; naive values are treated as UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@router.get("/audit-logs", response_model=AuditLogListOut)
def list_audit_logs(
    action: Optional[str] = Query(None, description="Exact match; comma-separated supports many values"),
    object_type: Optional[str] = Query(None),
    operator: Optional[str] = Query(
        None, description="Exact admin_user_id; 'system' means admin_user_id IS NULL"
    ),
    q: Optional[str] = Query(
        None, description="ILIKE search of object_id / id / operator name / email"
    ),
    from_: Optional[str] = Query(None, alias="from", description="created_at lower bound, ISO-8601"),
    to: Optional[str] = Query(None, alias="to", description="created_at upper bound, ISO-8601"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role()),
):
    _, tenant_id = auth
    qry = db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id)

    if action:
        actions = [value.strip() for value in action.split(",") if value.strip()]
        if actions:
            qry = qry.filter(AuditLog.action.in_(actions))
    if object_type:
        qry = qry.filter(AuditLog.object_type == object_type)
    if operator == "system":
        qry = qry.filter(AuditLog.admin_user_id.is_(None))
    elif operator:
        qry = qry.filter(AuditLog.admin_user_id == operator)

    if q:
        like = f"%{q}%"
        qry = qry.outerjoin(AdminUser, AdminUser.id == AuditLog.admin_user_id)
        qry = qry.filter(
            or_(
                AuditLog.object_id.ilike(like),
                AuditLog.id.ilike(like),
                AdminUser.name.ilike(like),
                AdminUser.email.ilike(like),
            )
        )
    if from_:
        qry = qry.filter(AuditLog.created_at >= _parse_ts(from_))
    if to:
        qry = qry.filter(AuditLog.created_at <= _parse_ts(to))

    total = qry.count()
    rows = qry.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset).all()

    actor_ids = [row.admin_user_id for row in rows if row.admin_user_id]
    name_map = {}
    if actor_ids:
        names = db.query(AdminUser.id, AdminUser.name).filter(AdminUser.id.in_(actor_ids)).all()
        name_map = {user_id: name for user_id, name in names}

    items = [
        AuditLogOut(
            id=row.id,
            admin_user_id=row.admin_user_id,
            actor_name=name_map.get(row.admin_user_id),
            action=row.action,
            object_type=row.object_type,
            object_id=row.object_id,
            detail=row.detail,
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]
    return AuditLogListOut(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )
