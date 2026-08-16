"""Safe request and response contracts for the internal operations console."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TenantLifecycleCountsOut(BaseModel):
    total: int
    trial: int
    active: int
    grace: int
    expired: int
    canceled: int
    frozen: int
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


class ManualFinancialSummaryOut(BaseModel):
    currency: str
    receipt_cents: int
    refund_cents: int
    net_cents: int


class CommercialExceptionCountsOut(BaseModel):
    payment_activation_pending: int
    payment_failed: int
    refund_pending: int
    refund_abnormal: int
    refund_closed: int
    refund_manual_recovery: int


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
    manual_financial: ManualFinancialSummaryOut
    exceptions: CommercialExceptionCountsOut
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
    subscription_stored_status: Optional[str]
    subscription_status: str
    subscription_starts_at: Optional[datetime]
    subscription_ends_at: Optional[datetime]
    subscription_grace_ends_at: Optional[datetime]
    cancel_at_period_end: bool
    frozen_at: Optional[datetime]
    suspended_at: Optional[datetime]
    suspension_reason: Optional[str]
    provider_receipt_cents: int
    provider_refund_cents: int
    provider_net_revenue_cents: int
    manual_receipt_cents: int
    manual_refund_cents: int
    manual_net_cents: int
    payment_exception_count: int
    refund_pending_count: int
    refund_abnormal_count: int
    refund_manual_recovery_count: int
    refund_closed_count: int
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
    subscription_source: Optional[str]
    subscription_renewal_count: int
    receipt_cents: int
    refund_cents: int
    net_revenue_cents: int
    manual_financial: ManualFinancialSummaryOut
    exceptions: CommercialExceptionCountsOut
    payment_orders: list["PlatformPaymentOrderOut"]
    refund_orders: list["PlatformRefundOrderOut"]
    manual_financial_transactions: list["PlatformManualFinancialOut"]
    recent_exceptions: list["CommercialExceptionOut"]
    audit_events: list["PlatformAuditEventOut"]


class PlatformPaymentOrderOut(BaseModel):
    order_id: str
    plan_name: str
    amount_cents: int
    currency: str
    provider: str
    status: str
    provider_state: Optional[str]
    provider_order_ref_masked: Optional[str]
    provider_transaction_ref_masked: Optional[str]
    latest_event_id_masked: Optional[str]
    failure_code: Optional[str]
    created_at: datetime
    paid_at: Optional[datetime]


class PlatformRefundOrderOut(BaseModel):
    refund_id: str
    payment_order_id: str
    amount_cents: int
    currency: str
    provider: str
    status: str
    provider_state: Optional[str]
    provider_ref_masked: Optional[str]
    provider_refund_id_masked: Optional[str]
    latest_event_id_masked: Optional[str]
    reason_code: str
    failure_code: Optional[str]
    requested_at: datetime
    succeeded_at: Optional[datetime]


class PlatformManualFinancialOut(BaseModel):
    transaction_id: str
    kind: Literal["receipt", "refund"]
    amount_cents: int
    currency: str
    occurred_at: datetime
    reference_masked: Optional[str]


class CommercialExceptionOut(BaseModel):
    kind: Literal["payment", "refund", "activation"]
    subject_id: str
    status: str
    failure_code: Optional[str]
    occurred_at: datetime


class PlatformAuditEventOut(BaseModel):
    audit_id: str
    action: str
    object_type: str
    object_id: Optional[str]
    created_at: datetime


TenantOperationsDetailOut.model_rebuild()


class TenantServiceStatusUpdateIn(BaseModel):
    lifecycle_status: Literal["active", "suspended"]
    reason_code: str = Field(min_length=1, max_length=64)
    confirmation: str = Field(min_length=1, max_length=128)


class ControlledOperationIn(BaseModel):
    reason_code: str = Field(min_length=1, max_length=64)
    confirmation: str = Field(min_length=1, max_length=128)


class TenantServiceStatusOut(BaseModel):
    tenant_id: str
    lifecycle_status: str
    is_active: bool
    updated_at: datetime


class ManualSubscriptionUpdateIn(BaseModel):
    plan_code: str = Field(min_length=1, max_length=64)
    status: Literal["trial", "active", "grace", "expired", "canceled"]
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
