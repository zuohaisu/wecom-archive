"""Tenant-scoped retention configuration endpoints (RND-318)."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import require_role
from app.db.models import AdminUser, RetentionConfig
from app.db.session import get_db
from app.schemas.retention import RetentionConfigOut, RetentionConfigUpdate

router = APIRouter()


@router.get("/retention-config", response_model=RetentionConfigOut)
def get_retention_config(
    auth: tuple[AdminUser, str] = Depends(require_role()),
    db: Session = Depends(get_db),
) -> RetentionConfigOut:
    """Return the authenticated tenant's policy, including its unconfigured state."""
    _, tenant_id = auth
    config = (
        db.query(RetentionConfig)
        .filter(RetentionConfig.tenant_id == tenant_id)
        .first()
    )
    if config is None:
        return RetentionConfigOut(
            configured=False,
            retention_days=None,
            is_locked=False,
        )
    return RetentionConfigOut(
        configured=True,
        retention_days=config.retention_days,
        is_locked=config.is_locked,
    )


@router.put("/retention-config", response_model=RetentionConfigOut)
def put_retention_config(
    payload: RetentionConfigUpdate,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
) -> RetentionConfigOut:
    """Create or update the policy unless this tenant has permanently locked it."""
    actor, tenant_id = auth
    config = (
        db.query(RetentionConfig)
        .filter(RetentionConfig.tenant_id == tenant_id)
        .first()
    )
    if config is not None and config.is_locked:
        raise HTTPException(status_code=423, detail="Retention configuration is locked")

    old_state = None if config is None else {
        "retention_days": config.retention_days,
        "is_locked": config.is_locked,
    }
    new_state = {"retention_days": payload.retention_days, "is_locked": payload.lock}
    if old_state == new_state:
        return RetentionConfigOut(configured=True, retention_days=config.retention_days, is_locked=config.is_locked)

    if config is None:
        config = RetentionConfig(
            id=str(uuid4()), tenant_id=tenant_id,
            retention_days=payload.retention_days, is_locked=payload.lock,
        )
        db.add(config)
    else:
        config.retention_days = payload.retention_days
        config.is_locked = payload.lock

    action = (
        AuditAction.RETENTION_CONFIG_LOCKED
        if payload.lock and (old_state is None or not old_state["is_locked"])
        else AuditAction.RETENTION_CONFIG_CHANGED
    )
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=getattr(actor, "id", None),
        action=action,
        object_type=AuditObjectType.RETENTION_CONFIG,
        object_id=config.id,
        detail={"old": old_state, "new": new_state},
    )
    db.commit()
    return RetentionConfigOut(
        configured=True,
        retention_days=config.retention_days,
        is_locked=config.is_locked,
    )
