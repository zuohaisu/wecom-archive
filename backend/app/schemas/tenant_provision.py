"""Request and safe response schemas for platform tenant provisioning."""

from pydantic import BaseModel, EmailStr


class TenantProvisionIn(BaseModel):
    name: str
    slug: str
    admin_email: EmailStr
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
