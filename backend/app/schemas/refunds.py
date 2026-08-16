"""Contracts for platform-controlled full refunds."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SubmitRefundIn(BaseModel):
    payment_order_id: str = Field(min_length=1, max_length=36)
    reason_code: str = Field(min_length=1, max_length=64)


class RefundOut(BaseModel):
    refund_id: str
    tenant_id: str
    payment_order_id: str
    amount_cents: int
    currency: str
    provider: str
    provider_ref: Optional[str]
    provider_refund_id: Optional[str]
    provider_state: Optional[str]
    status: str
    reason_code: str
    failure_code: Optional[str]
    requested_at: datetime
    succeeded_at: Optional[datetime]
    entitlement_reversed_at: Optional[datetime]
