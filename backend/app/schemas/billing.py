from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class BillingPlanOut(BaseModel):
    code: str
    display_name: str
    amount_cents: int
    currency: str
    billing_period_months: int
    storage_quota_bytes: int
    unlimited_seats: bool
    tencent_archive_fee_separate: bool
    payment_enabled: bool


class CreatePaymentOrderIn(BaseModel):
    plan_code: str


class StorageCapacityOut(BaseModel):
    plan_code: Optional[str]
    subscription_status: str
    quota_bytes: int
    used_bytes: Optional[int]
    remaining_bytes: Optional[int]
    utilization_basis_points: Optional[int]
    state: str
    usage_status: str
    can_accept_new_media: bool
    measured_at: datetime


class SubscriptionOverviewOut(BaseModel):
    plan_code: Optional[str]
    plan_name: Optional[str]
    stored_status: Optional[str]
    effective_status: str
    display_state: str
    unavailable_reason: Optional[str]
    is_entitled: bool
    starts_at: Optional[datetime]
    ends_at: Optional[datetime]
    grace_ends_at: Optional[datetime]
    cancel_at_period_end: bool
    entitlements: list[str]
    renewal_count: int
    measured_at: datetime


class PaymentOrderOut(BaseModel):
    order_id: str
    plan_code: str
    plan_name: str
    amount_cents: int
    currency: str
    provider: str
    status: str
    provider_state: Optional[str]
    created_at: datetime
    expires_at: datetime
    paid_at: Optional[datetime]
    activated_at: Optional[datetime]
    subscription_ends_at: Optional[datetime]
    failure_code: Optional[str]
    qr_available: bool
