"""Platform-console tenant provisioning routes (RND-311)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import require_platform_admin
from app.db.models import DuplicateCorpIdError, Tenant, TenantWecomConfig
from app.db.session import get_db
from app.schemas.tenant_provision import TenantProvisionIn, TenantProvisionOut

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
