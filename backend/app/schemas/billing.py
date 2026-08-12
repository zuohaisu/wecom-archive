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
