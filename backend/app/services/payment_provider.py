"""Provider-neutral online-payment boundary used by billing orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol


@dataclass(frozen=True)
class PaymentRequest:
    provider_order_ref: str
    description: str
    amount_cents: int
    currency: str
    expires_at: datetime


@dataclass(frozen=True)
class CheckoutArtifact:
    provider_order_ref: str
    kind: str
    value: str


@dataclass(frozen=True)
class TrustedPaymentEvent:
    provider: str
    provider_event_id: str
    provider_order_ref: str
    provider_transaction_id: str
    event_type: str
    app_id: str
    merchant_id: str
    state: str
    amount_cents: int
    currency: str
    succeeded_at: datetime
    payload_hash: str
    source: str


@dataclass(frozen=True)
class PaymentQueryResult:
    provider: str
    provider_order_ref: str
    state: str
    status: str
    success: TrustedPaymentEvent | None


@dataclass(frozen=True)
class RefundRequest:
    provider_ref: str
    provider_order_ref: str
    provider_transaction_id: str
    amount_cents: int
    total_amount_cents: int
    currency: str
    reason: str


@dataclass(frozen=True)
class RefundSubmissionResult:
    provider: str
    provider_ref: str
    provider_refund_id: str
    provider_order_ref: str
    provider_transaction_id: str
    state: str
    amount_cents: int
    total_amount_cents: int
    currency: str
    accepted_at: datetime


@dataclass(frozen=True)
class TrustedRefundEvent:
    provider: str
    provider_event_id: str
    provider_ref: str
    provider_refund_id: str
    provider_order_ref: str
    provider_transaction_id: str
    merchant_id: str
    state: str
    source: str
    amount_cents: int
    total_amount_cents: int
    currency: str
    payload_hash: str
    occurred_at: datetime


class PaymentProvider(Protocol):
    code: str
    app_id: str
    merchant_id: str

    def create_payment(self, request: PaymentRequest) -> CheckoutArtifact: ...

    def verify_and_parse_notification(
        self, headers: Mapping[str, str], raw_body: bytes
    ) -> TrustedPaymentEvent: ...

    def query_payment(self, provider_order_ref: str) -> PaymentQueryResult: ...

    def close_payment(self, provider_order_ref: str) -> None: ...

    def create_refund(self, request: RefundRequest) -> RefundSubmissionResult: ...

    def query_refund(self, provider_ref: str) -> TrustedRefundEvent: ...

    def verify_and_parse_refund_notification(
        self, headers: Mapping[str, str], raw_body: bytes
    ) -> TrustedRefundEvent: ...
