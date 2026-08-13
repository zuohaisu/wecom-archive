"""Wire contracts for tenant-scoped paid branding controls (RND-259)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class DomainConfigureIn(BaseModel):
    """The one hostname a tenant wants the managed edge to serve."""

    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(min_length=1, max_length=253)


class CertificateStatusIn(BaseModel):
    """Trusted platform-controller report; never accepted from a tenant admin."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "issued", "failed", "expired"]
    expires_at: Optional[datetime] = None
    failure_code: Optional[str] = Field(default=None, max_length=64)


class ManagedBrandingDomainOut(BaseModel):
    """Non-secret work item exposed only to the trusted platform TLS controller."""

    tenant_id: str
    hostname: str
    domain_state: str
    domain_enabled: bool
    certificate_status: str
    certificate_expires_at: Optional[datetime]
    certificate_last_checked_at: Optional[datetime]


class BrandingDomainMetricsOut(BaseModel):
    """Non-sensitive operational counters for the managed-domain controller."""

    pending_verification_count: int
    pending_certificate_count: int
    certificates_expiring_30_days_count: int
    certificate_failure_count: int
    invalid_binding_count: int


class BrandingStatusOut(BaseModel):
    custom_branding_entitled: bool
    custom_domain_entitled: bool
    upgrade_required: bool
    logo_configured: bool
    favicon_configured: bool
    custom_domain: Optional[str]
    domain_state: Optional[str]
    domain_enabled: bool
    effective_domain_active: bool
    verification_record_name: Optional[str]
    certificate_status: Optional[str]
    certificate_expires_at: Optional[datetime]
    certificate_last_checked_at: Optional[datetime]
    domain_last_checked_at: Optional[datetime]
    domain_failure_code: Optional[str]
    updated_at: Optional[datetime]
