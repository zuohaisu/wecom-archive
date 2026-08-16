"""WeChat Pay API v3 Native provider implementation for RND-380.

Every accepted response/notification is verified with the configured WeChat
Pay public key ID before JSON is trusted. Secret values are never included in
exceptions, logs, response objects, or dataclass representations.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.payment_provider import (
    CheckoutArtifact,
    PaymentQueryResult,
    PaymentRequest,
    RefundRequest,
    RefundSubmissionResult,
    TrustedPaymentEvent,
    TrustedRefundEvent,
)
from app.settings import WechatPaySettings, get_wechat_pay_settings

WECHAT_PAY_API_BASE = "https://api.mch.weixin.qq.com"
WECHAT_PAY_PROVIDER = "wechat_pay"
CALLBACK_TOLERANCE_SECONDS = 300
PENDING_STATES = frozenset({"NOTPAY", "USERPAYING"})
TERMINAL_FAILED_STATES = frozenset({"CLOSED", "REVOKED", "PAYERROR"})
REFUND_STATES = frozenset({"PROCESSING", "SUCCESS", "CLOSED", "ABNORMAL"})


class WechatPayError(RuntimeError):
    pass


class WechatPayConfigurationError(WechatPayError):
    pass


class WechatPayNotConfiguredError(WechatPayConfigurationError):
    pass


class WechatPayProtocolError(WechatPayError):
    pass


class WechatPayVerificationError(WechatPayProtocolError):
    pass


@dataclass(frozen=True)
class WechatPayConfig:
    app_id: str
    merchant_id: str
    merchant_serial_no: str
    merchant_private_key: rsa.RSAPrivateKey = field(repr=False)
    api_v3_key: bytes = field(repr=False)
    public_key_id: str
    public_key: rsa.RSAPublicKey = field(repr=False)
    notify_url: str
    refund_notify_url: str


def _enabled(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    raise WechatPayConfigurationError("WECHAT_PAY_ENABLED is invalid")


def wechat_pay_is_enabled(settings: WechatPaySettings | None = None) -> bool:
    return _enabled((settings or get_wechat_pay_settings()).wechat_pay_enabled)


def _pem(value: str) -> bytes:
    normalized = value.strip()
    if "\\n" in normalized and "\n" not in normalized:
        normalized = normalized.replace("\\n", "\n")
    return normalized.encode("utf-8")


def load_wechat_pay_config(
    settings: WechatPaySettings | None = None,
) -> WechatPayConfig:
    source = settings or get_wechat_pay_settings()
    if not wechat_pay_is_enabled(source):
        raise WechatPayNotConfiguredError("WeChat Pay is not enabled")
    values = {
        "WECHAT_PAY_APP_ID": source.wechat_pay_app_id.strip(),
        "WECHAT_PAY_MCH_ID": source.wechat_pay_mch_id.strip(),
        "WECHAT_PAY_MERCHANT_SERIAL_NO": source.wechat_pay_merchant_serial_no.strip(),
        "WECHAT_PAY_MERCHANT_PRIVATE_KEY": source.wechat_pay_merchant_private_key.strip(),
        "WECHAT_PAY_API_V3_KEY": source.wechat_pay_api_v3_key.strip(),
        "WECHAT_PAY_PUBLIC_KEY_ID": source.wechat_pay_public_key_id.strip(),
        "WECHAT_PAY_PUBLIC_KEY": source.wechat_pay_public_key.strip(),
        "WECHAT_PAY_NOTIFY_URL": source.wechat_pay_notify_url.strip(),
        "WECHAT_PAY_REFUND_NOTIFY_URL": source.wechat_pay_refund_notify_url.strip(),
    }
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise WechatPayConfigurationError(
            "enabled WeChat Pay configuration is incomplete: " + ", ".join(missing)
        )
    if len(values["WECHAT_PAY_APP_ID"]) > 64 or len(values["WECHAT_PAY_MCH_ID"]) > 32:
        raise WechatPayConfigurationError("WeChat Pay identity configuration is invalid")
    if len(values["WECHAT_PAY_MERCHANT_SERIAL_NO"]) > 64:
        raise WechatPayConfigurationError("merchant serial number is invalid")
    if not values["WECHAT_PAY_PUBLIC_KEY_ID"].startswith("PUB_KEY_ID_"):
        raise WechatPayConfigurationError("WeChat Pay public key ID is invalid")
    api_v3_key = values["WECHAT_PAY_API_V3_KEY"].encode("utf-8")
    if len(api_v3_key) != 32:
        raise WechatPayConfigurationError("WECHAT_PAY_API_V3_KEY must be 32 bytes")
    for name, expected_path in (
        ("WECHAT_PAY_NOTIFY_URL", "/api/payments/wechat/notify"),
        ("WECHAT_PAY_REFUND_NOTIFY_URL", "/api/refunds/wechat/notify"),
    ):
        notify = urlparse(values[name])
        if (
            notify.scheme != "https"
            or not notify.hostname
            or notify.username is not None
            or notify.password is not None
            or notify.query
            or notify.fragment
            or notify.path != expected_path
        ):
            raise WechatPayConfigurationError(f"{name} is invalid")
    try:
        private_key = serialization.load_pem_private_key(
            _pem(values["WECHAT_PAY_MERCHANT_PRIVATE_KEY"]), password=None
        )
        public_key = serialization.load_pem_public_key(
            _pem(values["WECHAT_PAY_PUBLIC_KEY"])
        )
    except Exception as error:
        raise WechatPayConfigurationError("WeChat Pay RSA key configuration is invalid") from error
    if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2048:
        raise WechatPayConfigurationError("merchant RSA private key is invalid")
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 2048:
        raise WechatPayConfigurationError("WeChat Pay RSA public key is invalid")
    return WechatPayConfig(
        app_id=values["WECHAT_PAY_APP_ID"],
        merchant_id=values["WECHAT_PAY_MCH_ID"],
        merchant_serial_no=values["WECHAT_PAY_MERCHANT_SERIAL_NO"],
        merchant_private_key=private_key,
        api_v3_key=api_v3_key,
        public_key_id=values["WECHAT_PAY_PUBLIC_KEY_ID"],
        public_key=public_key,
        notify_url=values["WECHAT_PAY_NOTIFY_URL"],
        refund_notify_url=values["WECHAT_PAY_REFUND_NOTIFY_URL"],
    )


def validate_wechat_pay_configuration_if_enabled() -> None:
    settings = get_wechat_pay_settings()
    if wechat_pay_is_enabled(settings):
        load_wechat_pay_config(settings)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WechatPayProtocolError("provider timestamp has no timezone")
    return value.astimezone(timezone.utc)


def _parse_rfc3339(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise WechatPayProtocolError("provider timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise WechatPayProtocolError("provider timestamp is invalid") from error
    return _utc(parsed)


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WechatPayProtocolError("provider response is not valid JSON") from error
    if not isinstance(value, dict):
        raise WechatPayProtocolError("provider response has invalid shape")
    return value


class WechatPayProvider:
    code = WECHAT_PAY_PROVIDER

    def __init__(
        self,
        config: WechatPayConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        nonce: Callable[[], str] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._nonce = nonce or (lambda: secrets.token_hex(16))

    @property
    def app_id(self) -> str:
        return self._config.app_id

    @property
    def merchant_id(self) -> str:
        return self._config.merchant_id

    def _signature(self, message: bytes) -> str:
        signature = self._config.merchant_private_key.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def _authorization(self, method: str, canonical_url: str, body: str) -> str:
        timestamp = str(int(_utc(self._now()).timestamp()))
        nonce = self._nonce()
        message = f"{method}\n{canonical_url}\n{timestamp}\n{nonce}\n{body}\n".encode()
        signature = self._signature(message)
        return (
            'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{self.merchant_id}",nonce_str="{nonce}",'
            f'signature="{signature}",timestamp="{timestamp}",'
            f'serial_no="{self._config.merchant_serial_no}"'
        )

    def _verify_headers(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        serial = headers.get("Wechatpay-Serial") or headers.get("wechatpay-serial")
        timestamp_raw = headers.get("Wechatpay-Timestamp") or headers.get(
            "wechatpay-timestamp"
        )
        nonce = headers.get("Wechatpay-Nonce") or headers.get("wechatpay-nonce")
        signature_raw = headers.get("Wechatpay-Signature") or headers.get(
            "wechatpay-signature"
        )
        if serial != self._config.public_key_id:
            raise WechatPayVerificationError("unknown WeChat Pay public key ID")
        if not timestamp_raw or not nonce or not signature_raw:
            raise WechatPayVerificationError("missing WeChat Pay signature headers")
        try:
            timestamp = int(timestamp_raw)
        except ValueError as error:
            raise WechatPayVerificationError("invalid WeChat Pay timestamp") from error
        now_timestamp = int(_utc(self._now()).timestamp())
        if abs(now_timestamp - timestamp) > CALLBACK_TOLERANCE_SECONDS:
            raise WechatPayVerificationError("stale WeChat Pay signature")
        try:
            signature = base64.b64decode(signature_raw, validate=True)
        except (binascii.Error, ValueError) as error:
            raise WechatPayVerificationError("invalid WeChat Pay signature") from error
        message = timestamp_raw.encode() + b"\n" + nonce.encode() + b"\n" + raw_body + b"\n"
        try:
            self._config.public_key.verify(
                signature,
                message,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as error:
            raise WechatPayVerificationError("invalid WeChat Pay signature") from error

    def _request(self, method: str, canonical_url: str, body: str = "") -> bytes:
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization(method, canonical_url, body),
            "Content-Type": "application/json",
            "User-Agent": "wecom-archive-365/1.0",
        }
        try:
            with httpx.Client(
                base_url=WECHAT_PAY_API_BASE,
                timeout=10.0,
                transport=self._transport,
            ) as client:
                response = client.request(
                    method,
                    canonical_url,
                    content=body.encode("utf-8") if body else None,
                    headers=headers,
                )
        except Exception as error:
            raise WechatPayProtocolError("WeChat Pay request failed") from error
        raw = response.content
        if raw:
            self._verify_headers(response.headers, raw)
        if response.status_code < 200 or response.status_code >= 300:
            raise WechatPayProtocolError(
                f"WeChat Pay request returned HTTP {response.status_code}"
            )
        return raw

    def create_payment(self, request: PaymentRequest) -> CheckoutArtifact:
        body = json.dumps(
            {
                "appid": self.app_id,
                "mchid": self.merchant_id,
                "description": request.description,
                "out_trade_no": request.provider_order_ref,
                "time_expire": _utc(request.expires_at).isoformat().replace("+00:00", "Z"),
                "notify_url": self._config.notify_url,
                "amount": {
                    "total": request.amount_cents,
                    "currency": request.currency,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw = self._request("POST", "/v3/pay/transactions/native", body)
        data = _json_object(raw)
        code_url = data.get("code_url")
        if (
            not isinstance(code_url, str)
            or not code_url.startswith("weixin://wxpay/bizpayurl?")
            or len(code_url) > 2048
        ):
            raise WechatPayProtocolError("WeChat Pay response has invalid code_url")
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="qr_code",
            value=code_url,
        )

    def _success_event(
        self,
        data: dict[str, Any],
        *,
        event_id: str,
        payload_hash: str,
        source: str,
    ) -> TrustedPaymentEvent:
        amount = data.get("amount")
        if not isinstance(amount, dict):
            raise WechatPayProtocolError("payment amount is missing")
        required_strings = {
            "appid": data.get("appid"),
            "mchid": data.get("mchid"),
            "out_trade_no": data.get("out_trade_no"),
            "transaction_id": data.get("transaction_id"),
            "trade_state": data.get("trade_state"),
            "currency": amount.get("currency"),
        }
        if any(not isinstance(value, str) or not value for value in required_strings.values()):
            raise WechatPayProtocolError("payment result is incomplete")
        total = amount.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            raise WechatPayProtocolError("payment amount is invalid")
        if required_strings["trade_state"] != "SUCCESS":
            raise WechatPayProtocolError("payment is not successful")
        return TrustedPaymentEvent(
            provider=self.code,
            provider_event_id=event_id,
            provider_order_ref=required_strings["out_trade_no"],
            provider_transaction_id=required_strings["transaction_id"],
            event_type="payment_succeeded",
            app_id=required_strings["appid"],
            merchant_id=required_strings["mchid"],
            state=required_strings["trade_state"],
            amount_cents=total,
            currency=required_strings["currency"],
            succeeded_at=_parse_rfc3339(data.get("success_time")),
            payload_hash=payload_hash,
            source=source,
        )

    def verify_and_parse_notification(
        self, headers: Mapping[str, str], raw_body: bytes
    ) -> TrustedPaymentEvent:
        if not raw_body or len(raw_body) > 65536:
            raise WechatPayProtocolError("notification body is invalid")
        self._verify_headers(headers, raw_body)
        envelope = _json_object(raw_body)
        event_id = envelope.get("id")
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id) > 128
            or envelope.get("event_type") != "TRANSACTION.SUCCESS"
            or envelope.get("resource_type") != "encrypt-resource"
        ):
            raise WechatPayProtocolError("notification envelope is invalid")
        resource = envelope.get("resource")
        if not isinstance(resource, dict) or resource.get("original_type") != "transaction":
            raise WechatPayProtocolError("notification resource is invalid")
        nonce = resource.get("nonce")
        ciphertext = resource.get("ciphertext")
        associated_data = resource.get("associated_data", "")
        if (
            not isinstance(nonce, str)
            or not nonce
            or not isinstance(ciphertext, str)
            or not ciphertext
            or not isinstance(associated_data, str)
        ):
            raise WechatPayProtocolError("notification resource is incomplete")
        try:
            encrypted = base64.b64decode(ciphertext, validate=True)
            plaintext = AESGCM(self._config.api_v3_key).decrypt(
                nonce.encode("utf-8"),
                encrypted,
                associated_data.encode("utf-8"),
            )
        except Exception as error:
            raise WechatPayVerificationError("notification decryption failed") from error
        data = _json_object(plaintext)
        if data.get("appid") != self.app_id or data.get("mchid") != self.merchant_id:
            raise WechatPayVerificationError("notification merchant identity mismatch")
        return self._success_event(
            data,
            event_id=event_id,
            payload_hash=hashlib.sha256(raw_body).hexdigest(),
            source="callback",
        )

    def query_payment(self, provider_order_ref: str) -> PaymentQueryResult:
        safe_ref = quote(provider_order_ref, safe="")
        query = urlencode({"mchid": self.merchant_id})
        canonical_url = f"/v3/pay/transactions/out-trade-no/{safe_ref}?{query}"
        raw = self._request("GET", canonical_url)
        data = _json_object(raw)
        if data.get("out_trade_no") != provider_order_ref:
            raise WechatPayVerificationError("query order reference mismatch")
        if data.get("appid") != self.app_id or data.get("mchid") != self.merchant_id:
            raise WechatPayVerificationError("query merchant identity mismatch")
        state = data.get("trade_state")
        if not isinstance(state, str) or state not in {
            "SUCCESS",
            *PENDING_STATES,
            *TERMINAL_FAILED_STATES,
        }:
            raise WechatPayProtocolError("query returned unknown payment state")
        success = None
        if state == "SUCCESS":
            transaction_id = data.get("transaction_id")
            if not isinstance(transaction_id, str) or not transaction_id:
                raise WechatPayProtocolError("query transaction ID is missing")
            success = self._success_event(
                data,
                event_id=f"query:{transaction_id}",
                payload_hash=hashlib.sha256(raw).hexdigest(),
                source="query",
            )
        return PaymentQueryResult(
            provider=self.code,
            provider_order_ref=provider_order_ref,
            state=state,
            status=(
                "succeeded"
                if state == "SUCCESS"
                else "pending"
                if state in PENDING_STATES
                else "closed"
                if state in {"CLOSED", "REVOKED"}
                else "failed"
            ),
            success=success,
        )

    def _refund_result_fields(
        self,
        data: dict[str, Any],
        *,
        expected_provider_ref: str | None = None,
    ) -> tuple[dict[str, str], int, int]:
        amount = data.get("amount")
        if not isinstance(amount, dict):
            raise WechatPayProtocolError("refund amount is missing")
        required_strings = {
            "refund_id": data.get("refund_id"),
            "out_refund_no": data.get("out_refund_no"),
            "transaction_id": data.get("transaction_id"),
            "out_trade_no": data.get("out_trade_no"),
            "status": data.get("refund_status", data.get("status")),
            "currency": amount.get("currency"),
        }
        if any(
            not isinstance(value, str) or not value
            for value in required_strings.values()
        ):
            raise WechatPayProtocolError("refund result is incomplete")
        if required_strings["status"] not in REFUND_STATES:
            raise WechatPayProtocolError("refund result has unknown state")
        if (
            expected_provider_ref is not None
            and required_strings["out_refund_no"] != expected_provider_ref
        ):
            raise WechatPayVerificationError("refund reference mismatch")
        total = amount.get("total")
        refund = amount.get("refund")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total <= 0
            or not isinstance(refund, int)
            or isinstance(refund, bool)
            or refund <= 0
            or refund != total
        ):
            raise WechatPayProtocolError("refund amount is invalid")
        return required_strings, refund, total

    def create_refund(self, request: RefundRequest) -> RefundSubmissionResult:
        body = json.dumps(
            {
                "transaction_id": request.provider_transaction_id,
                "out_refund_no": request.provider_ref,
                "reason": request.reason,
                "notify_url": self._config.refund_notify_url,
                "amount": {
                    "refund": request.amount_cents,
                    "total": request.total_amount_cents,
                    "currency": request.currency,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw = self._request("POST", "/v3/refund/domestic/refunds", body)
        data = _json_object(raw)
        fields, refund, total = self._refund_result_fields(
            data, expected_provider_ref=request.provider_ref
        )
        if (
            fields["transaction_id"] != request.provider_transaction_id
            or fields["out_trade_no"] != request.provider_order_ref
            or refund != request.amount_cents
            or total != request.total_amount_cents
            or fields["currency"] != request.currency
        ):
            raise WechatPayVerificationError("refund submission result mismatch")
        return RefundSubmissionResult(
            provider=self.code,
            provider_ref=fields["out_refund_no"],
            provider_refund_id=fields["refund_id"],
            provider_order_ref=fields["out_trade_no"],
            provider_transaction_id=fields["transaction_id"],
            state=fields["status"],
            amount_cents=refund,
            total_amount_cents=total,
            currency=fields["currency"],
            accepted_at=_parse_rfc3339(data.get("create_time")),
        )

    def _trusted_refund_event(
        self,
        data: dict[str, Any],
        *,
        event_id: str,
        payload_hash: str,
        source: str,
        occurred_at: datetime,
        expected_provider_ref: str | None = None,
    ) -> TrustedRefundEvent:
        fields, refund, total = self._refund_result_fields(
            data, expected_provider_ref=expected_provider_ref
        )
        return TrustedRefundEvent(
            provider=self.code,
            provider_event_id=event_id,
            provider_ref=fields["out_refund_no"],
            provider_refund_id=fields["refund_id"],
            provider_order_ref=fields["out_trade_no"],
            provider_transaction_id=fields["transaction_id"],
            merchant_id=self.merchant_id,
            state=fields["status"],
            source=source,
            amount_cents=refund,
            total_amount_cents=total,
            currency=fields["currency"],
            payload_hash=payload_hash,
            occurred_at=occurred_at,
        )

    def query_refund(self, provider_ref: str) -> TrustedRefundEvent:
        safe_ref = quote(provider_ref, safe="")
        raw = self._request("GET", f"/v3/refund/domestic/refunds/{safe_ref}")
        data = _json_object(raw)
        payload_hash = hashlib.sha256(raw).hexdigest()
        state = data.get("status")
        refund_id = data.get("refund_id")
        if not isinstance(state, str) or not isinstance(refund_id, str):
            raise WechatPayProtocolError("refund query result is incomplete")
        occurred_at = (
            _parse_rfc3339(data.get("success_time"))
            if state == "SUCCESS"
            else _utc(self._now())
        )
        return self._trusted_refund_event(
            data,
            event_id=f"query:{refund_id}:{state}:{payload_hash[:32]}",
            payload_hash=payload_hash,
            source="query",
            occurred_at=occurred_at,
            expected_provider_ref=provider_ref,
        )

    def verify_and_parse_refund_notification(
        self, headers: Mapping[str, str], raw_body: bytes
    ) -> TrustedRefundEvent:
        if not raw_body or len(raw_body) > 65536:
            raise WechatPayProtocolError("refund notification body is invalid")
        self._verify_headers(headers, raw_body)
        envelope = _json_object(raw_body)
        event_id = envelope.get("id")
        event_type = envelope.get("event_type")
        expected_state = {
            "REFUND.SUCCESS": "SUCCESS",
            "REFUND.ABNORMAL": "ABNORMAL",
            "REFUND.CLOSED": "CLOSED",
        }.get(event_type)
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id) > 128
            or expected_state is None
            or envelope.get("resource_type") != "encrypt-resource"
        ):
            raise WechatPayProtocolError("refund notification envelope is invalid")
        resource = envelope.get("resource")
        if (
            not isinstance(resource, dict)
            or resource.get("original_type") != "refund"
            or resource.get("algorithm") != "AEAD_AES_256_GCM"
        ):
            raise WechatPayProtocolError("refund notification resource is invalid")
        nonce = resource.get("nonce")
        ciphertext = resource.get("ciphertext")
        associated_data = resource.get("associated_data", "")
        if (
            not isinstance(nonce, str)
            or not nonce
            or not isinstance(ciphertext, str)
            or not ciphertext
            or not isinstance(associated_data, str)
        ):
            raise WechatPayProtocolError("refund notification resource is incomplete")
        try:
            encrypted = base64.b64decode(ciphertext, validate=True)
            plaintext = AESGCM(self._config.api_v3_key).decrypt(
                nonce.encode("utf-8"),
                encrypted,
                associated_data.encode("utf-8"),
            )
        except Exception as error:
            raise WechatPayVerificationError(
                "refund notification decryption failed"
            ) from error
        data = _json_object(plaintext)
        if data.get("mchid") != self.merchant_id:
            raise WechatPayVerificationError(
                "refund notification merchant identity mismatch"
            )
        if data.get("refund_status") != expected_state:
            raise WechatPayVerificationError("refund notification state mismatch")
        return self._trusted_refund_event(
            data,
            event_id=event_id,
            payload_hash=hashlib.sha256(raw_body).hexdigest(),
            source="callback",
            occurred_at=(
                _parse_rfc3339(data.get("success_time"))
                if expected_state == "SUCCESS"
                else _parse_rfc3339(envelope.get("create_time"))
            ),
        )

    def close_payment(self, provider_order_ref: str) -> None:
        safe_ref = quote(provider_order_ref, safe="")
        body = json.dumps(
            {"mchid": self.merchant_id}, sort_keys=True, separators=(",", ":")
        )
        self._request(
            "POST",
            f"/v3/pay/transactions/out-trade-no/{safe_ref}/close",
            body,
        )


def get_wechat_pay_provider() -> WechatPayProvider:
    return WechatPayProvider(load_wechat_pay_config())
