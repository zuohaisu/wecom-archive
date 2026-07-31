"""Request and safe response schemas for platform tenant provisioning."""

from datetime import datetime
from pydantic import BaseModel


class TenantProvisionIn(BaseModel):
    name: str
    slug: str
    admin_email: str
    corp_id: str
    agent_id: str
    secret: str
    private_key_pem: str
    callback_domain: str = ""


class TenantProvisionOut(BaseModel):
    tenant_id: str
    tenant_name: str
    tenant_slug: str
    config_id: str
    corp_id: str
    agent_id: str
    is_active: bool


class TenantListItemOut(BaseModel):
    """Read-only tenant list item for platform console (zero key leakage)."""

    tenant_id: str
    tenant_name: str
    tenant_slug: str
    corp_id: str
    agent_id: str
    tenant_is_active: bool
    config_is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantListOut(BaseModel):
    """Response wrapper for tenant list endpoint."""

    tenants: list[TenantListItemOut]
