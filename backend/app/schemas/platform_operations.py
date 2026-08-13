"""Safe request and response contracts for the internal operations console."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TenantLifecycleCountsOut(BaseModel):
    total: int
    trial: int
    active: int
    expired: int
    canceled: int
    past_due: int
    suspended: int


class AccountCountsOut(BaseModel):
    admin_accounts: int
    active_admin_accounts: int
    observed_users: int


class UsageTotalsOut(BaseModel):
    message_count: int
    media_file_count: int
    storage_bytes: int


class RevenuePeriodOut(BaseModel):
    period: str
    receipt_cents: int
    refund_cents: int
    net_revenue_cents: int


class RevenueOut(BaseModel):
    currency: str
    receipt_cents: int
    refund_cents: int
    net_revenue_cents: int
    periods: list[RevenuePeriodOut]


class SubscriptionDistributionOut(BaseModel):
    plan_code: Optional[str]
    plan_name: Optional[str]
    status: str
    tenant_count: int


class RecentTenantOut(BaseModel):
    tenant_id: str
    tenant_name: str
    tenant_slug: str
    occurred_at: datetime


class StorageQuotaRiskOut(BaseModel):
    tenant_id: str
    tenant_name: str
    plan_code: str
    quota_bytes: int
    used_bytes: int
    utilization_basis_points: int
    state: Literal["near_quota", "at_quota", "over_quota"]


class PlatformOperationsDashboardOut(BaseModel):
    tenant_counts: TenantLifecycleCountsOut
    account_counts: AccountCountsOut
    usage_totals: UsageTotalsOut
    subscription_distribution: list[SubscriptionDistributionOut]
    revenue: RevenueOut
    recent_tenants: list[RecentTenantOut]
    recently_active_tenants: list[RecentTenantOut]
    storage_quota_risks: list[StorageQuotaRiskOut]
    measured_at: datetime


class TenantOperationsListItemOut(BaseModel):
    tenant_id: str
    tenant_name: str
    tenant_slug: str
    lifecycle_status: str
    is_active: bool
    created_at: datetime
    subscription_plan_code: Optional[str]
    subscription_plan_name: Optional[str]
    subscription_status: str
    subscription_ends_at: Optional[datetime]
    admin_account_count: int
    message_count: int
    media_file_count: int
    storage_bytes: int
    storage_quota_bytes: Optional[int]
    storage_utilization_basis_points: Optional[int]


class TenantOperationsListOut(BaseModel):
    items: list[TenantOperationsListItemOut]
    page: int
    page_size: int
    total: int


class TenantOperationsDetailOut(TenantOperationsListItemOut):
    active_admin_account_count: int
    observed_user_count: int
    subscription_starts_at: Optional[datetime]
    subscription_source: Optional[str]
    subscription_renewal_count: int
    receipt_cents: int
    refund_cents: int
    net_revenue_cents: int


class TenantServiceStatusUpdateIn(BaseModel):
    lifecycle_status: Literal["active", "suspended"]


class TenantServiceStatusOut(BaseModel):
    tenant_id: str
    lifecycle_status: str
    is_active: bool
    updated_at: datetime


class ManualSubscriptionUpdateIn(BaseModel):
    plan_code: str = Field(min_length=1, max_length=64)
    status: Literal["trial", "active", "past_due", "expired", "canceled"]
    starts_at: datetime
    ends_at: datetime


class ManualSubscriptionOut(BaseModel):
    tenant_id: str
    plan_code: str
    plan_name: str
    status: str
    starts_at: datetime
    ends_at: datetime
    renewal_count: int
    revision: int


class ManualFinancialTransactionIn(BaseModel):
    kind: Literal["receipt", "refund"]
    amount_cents: int = Field(gt=0)
    occurred_at: Optional[datetime] = None
    reference: Optional[str] = Field(default=None, max_length=128)
    note: Optional[str] = Field(default=None, max_length=1000)


class ManualFinancialTransactionOut(BaseModel):
    transaction_id: str
    tenant_id: str
    kind: Literal["receipt", "refund"]
    amount_cents: int
    currency: str
    occurred_at: datetime
    reference: Optional[str]
