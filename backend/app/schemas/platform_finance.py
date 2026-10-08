"""Platform finance order-list response contracts.

Read-only projection of provider payment/refund orders for the super-admin
订单明细 page. Amounts stay in cents; the client formats currency. No
provider credentials, checkout URLs, idempotency hashes, or recovery
control-plane fields are ever exposed here.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class FinanceOrderItemOut(BaseModel):
    kind: str  # "receipt" | "refund"
    order_id: str
    tenant_id: str
    tenant_name: str
    amount_cents: int
    currency: str
    provider: str
    status: str
    plan_name: Optional[str] = None  # receipts only
    reason_code: Optional[str] = None  # refunds only
    occurred_at: datetime
    created_at: datetime


class FinanceOrderSummaryOut(BaseModel):
    currency: str
    receipt_cents: int
    receipt_count: int
    refund_cents: int
    refund_count: int
    net_cents: int


class FinanceOrderListOut(BaseModel):
    kind: str
    total: int
    page: int
    page_size: int
    summary: FinanceOrderSummaryOut
    items: List[FinanceOrderItemOut]
