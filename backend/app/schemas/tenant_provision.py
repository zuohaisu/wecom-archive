"""Request and safe response schemas for platform tenant provisioning."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr


class TenantProvisionIn(BaseModel):
    name: str
    slug: str
    admin_email: EmailStr
    owner_email: EmailStr
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
    owner_invite_sent: bool


class TenantConnectivityCheckOut(BaseModel):
    """Safe result of a tenant's WeCom credential connectivity check."""

    ok: bool
    reason: Optional[str] = None


class TenantListItemOut(BaseModel):
    """Read-only tenant list item for platform console (zero key leakage)."""

    tenant_id: str
    tenant_name: str
    tenant_slug: str
    corp_id: Optional[str]
    agent_id: Optional[str]
    tenant_is_active: bool
    config_is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantListOut(BaseModel):
    """Response wrapper for tenant list endpoint."""

    tenants: list[TenantListItemOut]


class SyncHealthOut(BaseModel):
    """Safe aggregate sync status returned with a tenant usage summary."""

    status: str
    error_message: Optional[str]
    last_seq: Optional[int]
    updated_at: Optional[str]


class TenantUsageItemOut(BaseModel):
    """Read-only per-tenant aggregates for the platform console."""

    tenant_id: str
    tenant_name: str
    message_count: int
    storage_bytes: int
    employee_count: int
    sync_health: SyncHealthOut


class TenantUsageListOut(BaseModel):
    """Platform-wide list of individually scoped tenant usage summaries."""

    tenants: list[TenantUsageItemOut]


class TenantStatusUpdateIn(BaseModel):
    """Request body for PATCH /tenants/{tenant_id}."""

    is_active: bool


class TenantStatusUpdateOut(BaseModel):
    """Response after updating tenant status."""

    tenant_id: str
    tenant_name: str
    tenant_slug: str
    tenant_is_active: bool
    updated_at: datetime
