"""Platform-console tenant provisioning routes (RND-311)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import require_platform_admin
from app.audit import write_audit
from app.db.models import DuplicateCorpIdError, Tenant, TenantWecomConfig
from app.db.session import get_db
from app.schemas.tenant_provision import (
    TenantListItemOut,
    TenantListOut,
    TenantProvisionIn,
    TenantProvisionOut,
    TenantStatusUpdateIn,
    TenantStatusUpdateOut,
)

router = APIRouter()


@router.post(
    "/tenants",
    response_model=TenantProvisionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_tenant(
    payload: TenantProvisionIn,
    _admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TenantProvisionOut:
    """Create a tenant and encrypt its WeCom credentials before persistence."""
    if db.query(Tenant).filter(Tenant.slug == payload.slug).first():
        raise HTTPException(status_code=409, detail="Tenant slug already exists")

    tenant = Tenant(id=str(uuid.uuid4()), name=payload.name, slug=payload.slug)
    db.add(tenant)
    config = TenantWecomConfig(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        corp_id=payload.corp_id,
        agent_id=payload.agent_id,
        callback_domain=payload.callback_domain,
        is_active=True,
    )
    config.set_credentials(payload.secret, payload.private_key_pem)
    db.add(config)
    try:
        db.commit()
    except DuplicateCorpIdError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This WeCom CorpID is already assigned to another tenant.",
        )

    db.refresh(config)
    return TenantProvisionOut(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
        config_id=config.id,
        corp_id=config.corp_id,
        agent_id=config.agent_id,
        is_active=config.is_active,
    )


@router.get("/tenants", response_model=TenantListOut)
def list_tenants(
    _admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TenantListOut:
    """Return all provisioned tenants with their config status (no key leakage)."""
    rows = (
        db.query(Tenant, TenantWecomConfig)
        .join(TenantWecomConfig, TenantWecomConfig.tenant_id == Tenant.id)
        .all()
    )
    return TenantListOut(
        tenants=[
            TenantListItemOut(
                tenant_id=t.id,
                tenant_name=t.name,
                tenant_slug=t.slug,
                corp_id=c.corp_id,
                agent_id=c.agent_id,
                tenant_is_active=t.is_active,
                config_is_active=c.is_active,
                created_at=t.created_at,
            )
            for t, c in rows
        ]
    )


@router.patch(
    "/tenants/{tenant_id}",
    response_model=TenantStatusUpdateOut,
    status_code=status.HTTP_200_OK,
)
def update_tenant_status(
    tenant_id: str,
    payload: TenantStatusUpdateIn,
    admin_user=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TenantStatusUpdateOut:
    """Activate or deactivate a tenant (platform admin only).

    - **404**: Target tenant does not exist.
    - **401**: Missing or invalid platform admin credentials.
    - Always writes an audit log via `write_audit`.
    """
    # Find the target tenant
    tenant = (
        db.query(Tenant).filter(Tenant.id == tenant_id).first()
    )
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant with id '{tenant_id}' not found",
        )

    old_status = tenant.is_active
    new_status = payload.is_active

    # Update and persist
    tenant.is_active = new_status
    db.commit()
    db.refresh(tenant)

    # Write audit log with appropriate action based on status change
    action = (
        "platform.tenant_deactivated" if not new_status else "platform.tenant_activated"
    )
    write_audit(
        db,
        tenant_id=tenant_id,
        action=action,
        object_type="tenant_config",
        admin_user_id=admin_user.id,
        object_id=tenant_id,
        detail={
            "is_active": new_status,
            "previous_is_active": old_status,
        },
    )

    return TenantStatusUpdateOut(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
        tenant_is_active=tenant.is_active,
        updated_at=tenant.updated_at,
    )

