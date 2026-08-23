"""Alipay PC-page-payment adapter for RND-416.

The adapter creates a signed ``alipay.trade.page.pay`` gateway URL. The
browser follows that URL to the Alipay PC cashier, which presents the QR-code
experience; this application never creates a payment fact from a browser
return. Only a verified asynchronous notification or a verified query result
is converted into the provider-neutral trusted event consumed by billing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.services.payment_provider import (
    CheckoutArtifact,
    PaymentQueryResult,
    PaymentRequest,
    TrustedPaymentEvent,
)
from app.settings import AlipaySettings, get_alipay_settings

ALIPAY_GATEWAY_URL = "https://openapi.alipay.com/gateway.do"
ALIPAY_PROVIDER = "alipay"
ALIPAY_SIGN_TYPE = "RSA2"
ALIPAY_API_VERSION = "1.0"
ALIPAY_CHARSET = "utf-8"
ALIPAY_PRODUCT_CODE = "FAST_INSTANT_TRADE_PAY"
ALIPAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
MAX_BODY_BYTES = 65536
MAX_FORM_FIELDS = 64


class AlipayError(RuntimeError):
    pass


class AlipayConfigurationError(AlipayError):
    pass


class AlipayNotConfiguredError(AlipayConfigurationError):
    pass


class AlipayProtocolError(AlipayError):
    pass


class AlipayVerificationError(AlipayProtocolError):
    pass


@dataclass(frozen=True)
class AlipayConfig:
    app_id: str
    seller_id: str
    merchant_private_key: rsa.RSAPrivateKey = field(repr=False)
    alipay_public_key: rsa.RSAPublicKey = field(repr=False)
    notify_url: str
    return_url: str


def _enabled(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    raise AlipayConfigurationError("ALIPAY_ENABLED is invalid")


def alipay_is_enabled(settings: AlipaySettings | None = None) -> bool:
    return _enabled((settings or get_alipay_settings()).alipay_enabled)


def _pem(value: str) -> bytes:
    normalized = value.strip()
    if "\\n" in normalized and "\n" not in normalized:
        normalized = normalized.replace("\\n", "\n")
    return normalized.encode("utf-8")


def _validate_url(value: str, *, name: str, expected_path: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
    ):
        raise AlipayConfigurationError(f"{name} is invalid")
    return value


def load_alipay_config(settings: AlipaySettings | None = None) -> AlipayConfig:
    source = settings or get_alipay_settings()
    if not alipay_is_enabled(source):
        raise AlipayNotConfiguredError("Alipay is not enabled")
    values = {
        "ALIPAY_APP_ID": source.alipay_app_id.strip(),
        "ALIPAY_SELLER_ID": source.alipay_seller_id.strip(),
        "ALIPAY_MERCHANT_PRIVATE_KEY": source.alipay_merchant_private_key.strip(),
        "ALIPAY_PUBLIC_KEY": source.alipay_public_key.strip(),
        "ALIPAY_NOTIFY_URL": source.alipay_notify_url.strip(),
        "ALIPAY_RETURN_URL": source.alipay_return_url.strip(),
    }
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise AlipayConfigurationError(
            "enabled Alipay configuration is incomplete: " + ", ".join(missing)
        )
    if not values["ALIPAY_APP_ID"].isdigit() or len(values["ALIPAY_APP_ID"]) > 32:
        raise AlipayConfigurationError("Alipay app ID is invalid")
    if not values["ALIPAY_SELLER_ID"].isdigit() or len(values["ALIPAY_SELLER_ID"]) > 64:
        raise AlipayConfigurationError("Alipay seller ID is invalid")
    notify_url = _validate_url(
        values["ALIPAY_NOTIFY_URL"],
        name="ALIPAY_NOTIFY_URL",
        expected_path="/api/payments/alipay/notify",
    )
    return_url = _validate_url(
        values["ALIPAY_RETURN_URL"],
        name="ALIPAY_RETURN_URL",
        expected_path="/admin/billing",
    )
    try:
        merchant_private_key = serialization.load_pem_private_key(
            _pem(values["ALIPAY_MERCHANT_PRIVATE_KEY"]), password=None
        )
        try:
            alipay_public_key = serialization.load_pem_public_key(
                _pem(values["ALIPAY_PUBLIC_KEY"])
            )
        except ValueError:
            alipay_public_key = x509.load_pem_x509_certificate(
                _pem(values["ALIPAY_PUBLIC_KEY"])
            ).public_key()
    except Exception as error:
        raise AlipayConfigurationError("Alipay RSA key configuration is invalid") from error
    if (
        not isinstance(merchant_private_key, rsa.RSAPrivateKey)
        or merchant_private_key.key_size < 2048
    ):
        raise AlipayConfigurationError("Alipay merchant private key is invalid")
    if not isinstance(alipay_public_key, rsa.RSAPublicKey) or alipay_public_key.key_size < 2048:
        raise AlipayConfigurationError("Alipay public key is invalid")
    return AlipayConfig(
        app_id=values["ALIPAY_APP_ID"],
        seller_id=values["ALIPAY_SELLER_ID"],
        merchant_private_key=merchant_private_key,
        alipay_public_key=alipay_public_key,
        notify_url=notify_url,
        return_url=return_url,
    )


def validate_alipay_configuration_if_enabled() -> None:
    settings = get_alipay_settings()
    if alipay_is_enabled(settings):
        load_alipay_config(settings)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AlipayProtocolError("provider timestamp has no timezone")
    return value.astimezone(timezone.utc)


def _parse_provider_time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) != 19:
        raise AlipayProtocolError("provider timestamp is invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ALIPAY_TIMEZONE
        ).astimezone(timezone.utc)
    except ValueError as error:
        raise AlipayProtocolError("provider timestamp is invalid") from error


def _amount_cents(value: Any) -> int:
    if not isinstance(value, str) or not value or len(value) > 32:
        raise AlipayProtocolError("provider amount is invalid")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise AlipayProtocolError("provider amount is invalid") from error
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise AlipayProtocolError("provider amount is invalid")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise AlipayProtocolError("provider amount is invalid")
    return int(cents)


def _format_amount(cents: int) -> str:
    if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
        raise AlipayProtocolError("payment amount is invalid")
    return f"{Decimal(cents) / Decimal(100):.2f}"


def _required_string(data: Mapping[str, Any], name: str, maximum: int = 128) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AlipayProtocolError(f"provider {name} is invalid")
    return value


def _canonical_params(params: Mapping[str, str]) -> bytes:
    return "&".join(
        f"{name}={params[name]}" for name in sorted(params)
    ).encode("utf-8")


def _skip_whitespace(raw: bytes, position: int) -> int:
    while position < len(raw) and raw[position] in b" \t\r\n":
        position += 1
    return position


def _json_string_end(raw: bytes, position: int) -> int:
    if position >= len(raw) or raw[position] != ord('"'):
        raise AlipayProtocolError("provider response is malformed")
    position += 1
    escaped = False
    while position < len(raw):
        char = raw[position]
        if escaped:
            escaped = False
        elif char == ord("\\"):
            escaped = True
        elif char == ord('"'):
            return position + 1
        position += 1
    raise AlipayProtocolError("provider response is malformed")


def _json_value_end(raw: bytes, position: int) -> int:
    if position >= len(raw):
        raise AlipayProtocolError("provider response is malformed")
    if raw[position] == ord('"'):
        return _json_string_end(raw, position)
    if raw[position] not in (ord("{"), ord("[")):
        end = position
        while end < len(raw) and raw[end] not in b",}":
            end += 1
        try:
            json.loads(raw[position:end].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AlipayProtocolError("provider response is malformed") from error
        return end
    opening = raw[position]
    closing = ord("}") if opening == ord("{") else ord("]")
    depth = 0
    in_string = False
    escaped = False
    for end in range(position, len(raw)):
        char = raw[end]
        if in_string:
            if escaped:
                escaped = False
            elif char == ord("\\"):
                escaped = True
            elif char == ord('"'):
                in_string = False
            continue
        if char == ord('"'):
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return end + 1
    raise AlipayProtocolError("provider response is malformed")


def _root_json_fields(raw: bytes) -> dict[str, bytes]:
    """Return exact raw values for one JSON object's top-level fields.

    Alipay signs the unescaped JSON bytes of ``*_response`` rather than a
    reconstructed JSON object. Keeping that byte slice intact avoids
    accidentally validating a different serialisation.
    """
    if not raw or len(raw) > MAX_BODY_BYTES:
        raise AlipayProtocolError("provider response is invalid")
    position = _skip_whitespace(raw, 0)
    if position >= len(raw) or raw[position] != ord("{"):
        raise AlipayProtocolError("provider response is malformed")
    position += 1
    fields: dict[str, bytes] = {}
    while True:
        position = _skip_whitespace(raw, position)
        if position < len(raw) and raw[position] == ord("}"):
            position = _skip_whitespace(raw, position + 1)
            if position != len(raw):
                raise AlipayProtocolError("provider response is malformed")
            return fields
        key_start = position
        key_end = _json_string_end(raw, key_start)
        try:
            key = json.loads(raw[key_start:key_end].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AlipayProtocolError("provider response is malformed") from error
        if not isinstance(key, str) or key in fields:
            raise AlipayProtocolError("provider response is malformed")
        position = _skip_whitespace(raw, key_end)
        if position >= len(raw) or raw[position] != ord(":"):
            raise AlipayProtocolError("provider response is malformed")
        value_start = _skip_whitespace(raw, position + 1)
        value_end = _json_value_end(raw, value_start)
        fields[key] = raw[value_start:value_end]
        position = _skip_whitespace(raw, value_end)
        if position >= len(raw):
            raise AlipayProtocolError("provider response is malformed")
        if raw[position] == ord(","):
            position += 1
            continue
        if raw[position] != ord("}"):
            raise AlipayProtocolError("provider response is malformed")


def _json_string(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlipayProtocolError("provider response is malformed") from error
    if not isinstance(value, str):
        raise AlipayProtocolError("provider response is malformed")
    return value


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlipayProtocolError("provider response is malformed") from error
    if not isinstance(value, dict):
        raise AlipayProtocolError("provider response is malformed")
    return value


class AlipayProvider:
    code = ALIPAY_PROVIDER

    def __init__(
        self,
        config: AlipayConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def app_id(self) -> str:
        return self._config.app_id

    @property
    def merchant_id(self) -> str:
        return self._config.seller_id

    def _sign(self, content: bytes) -> str:
        signature = self._config.merchant_private_key.sign(
            content,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def _verify_signature(self, content: bytes, signature_value: str) -> None:
        try:
            signature = base64.b64decode(signature_value, validate=True)
            self._config.alipay_public_key.verify(
                signature,
                content,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (binascii.Error, ValueError, InvalidSignature) as error:
            raise AlipayVerificationError("invalid Alipay signature") from error

    def _common_params(self, method: str, biz_content: dict[str, Any]) -> dict[str, str]:
        timestamp = _utc(self._now()).astimezone(ALIPAY_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return {
            "app_id": self.app_id,
            "method": method,
            "format": "JSON",
            "charset": ALIPAY_CHARSET,
            "sign_type": ALIPAY_SIGN_TYPE,
            "timestamp": timestamp,
            "version": ALIPAY_API_VERSION,
            "biz_content": json.dumps(
                biz_content, ensure_ascii=False, separators=(",", ":")
            ),
        }

    def _signed_params(self, method: str, biz_content: dict[str, Any]) -> dict[str, str]:
        params = self._common_params(method, biz_content)
        unsigned = {
            name: value for name, value in params.items() if name != "sign_type"
        }
        params["sign"] = self._sign(_canonical_params(unsigned))
        return params

    def _request(self, method: str, biz_content: dict[str, Any], response_key: str) -> dict[str, Any]:
        params = self._signed_params(method, biz_content)
        try:
            with httpx.Client(
                timeout=10.0,
                transport=self._transport,
            ) as client:
                response = client.post(
                    ALIPAY_GATEWAY_URL,
                    content=urlencode(params).encode("utf-8"),
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                        "User-Agent": "wecom-archive-365/1.0",
                    },
                )
        except Exception as error:
            raise AlipayProtocolError("Alipay request failed") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise AlipayProtocolError(
                f"Alipay request returned HTTP {response.status_code}"
            )
        fields = _root_json_fields(response.content)
        content = fields.get(response_key)
        signature = fields.get("sign")
        sign_type = fields.get("sign_type")
        if content is None or signature is None or sign_type is None:
            raise AlipayProtocolError("Alipay response is incomplete")
        if _json_string(sign_type) != ALIPAY_SIGN_TYPE:
            raise AlipayVerificationError("Alipay response sign type is invalid")
        self._verify_signature(content, _json_string(signature))
        return _json_object(content)

    def create_payment(self, request: PaymentRequest) -> CheckoutArtifact:
        if request.currency != "CNY":
            raise AlipayProtocolError("Alipay payment currency is invalid")
        expires_at = _utc(request.expires_at)
        remaining_seconds = int((expires_at - _utc(self._now())).total_seconds())
        if remaining_seconds <= 0:
            raise AlipayProtocolError("payment order has expired")
        timeout_minutes = max(1, (remaining_seconds + 59) // 60)
        if (
            not request.provider_order_ref
            or len(request.provider_order_ref) > 64
            or not request.description
            or len(request.description) > 256
        ):
            raise AlipayProtocolError("payment request is invalid")
        params = self._signed_params(
            "alipay.trade.page.pay",
            {
                "out_trade_no": request.provider_order_ref,
                "product_code": ALIPAY_PRODUCT_CODE,
                "total_amount": _format_amount(request.amount_cents),
                "subject": request.description,
                "timeout_express": f"{timeout_minutes}m",
            },
        )
        params["notify_url"] = self._config.notify_url
        params["return_url"] = self._config.return_url
        # notify_url and return_url are part of Alipay's signed common
        # parameter set, so they must be included before calculating sign.
        unsigned = {
            name: value
            for name, value in params.items()
            if name not in {"sign", "sign_type"}
        }
        params["sign"] = self._sign(_canonical_params(unsigned))
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="redirect",
            value=f"{ALIPAY_GATEWAY_URL}?{urlencode(params)}",
        )

    def _success_event(
        self,
        data: Mapping[str, Any],
        *,
        event_id: str,
        payload_hash: str,
        source: str,
    ) -> TrustedPaymentEvent:
        app_id = _required_string(data, "app_id", 32)
        seller_id = _required_string(data, "seller_id", 64)
        provider_order_ref = _required_string(data, "out_trade_no", 64)
        transaction_id = _required_string(data, "trade_no", 64)
        trade_status = _required_string(data, "trade_status", 32)
        if app_id != self.app_id or seller_id != self.merchant_id:
            raise AlipayVerificationError("Alipay merchant identity mismatch")
        if trade_status not in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
            raise AlipayProtocolError("Alipay payment is not successful")
        return TrustedPaymentEvent(
            provider=self.code,
            provider_event_id=event_id,
            provider_order_ref=provider_order_ref,
            provider_transaction_id=transaction_id,
            event_type="payment_succeeded",
            app_id=app_id,
            merchant_id=seller_id,
            state="SUCCESS",
            amount_cents=_amount_cents(data.get("total_amount")),
            currency="CNY",
            succeeded_at=_parse_provider_time(data.get("gmt_payment", data.get("send_pay_date"))),
            payload_hash=payload_hash,
            source=source,
        )

    def _notification_params(self, raw_body: bytes) -> dict[str, str]:
        if not raw_body or len(raw_body) > MAX_BODY_BYTES:
            raise AlipayProtocolError("notification body is invalid")
        try:
            pairs = parse_qsl(
                raw_body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=MAX_FORM_FIELDS,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise AlipayProtocolError("notification body is invalid") from error
        values: dict[str, str] = {}
        for name, value in pairs:
            if not name or len(name) > 128 or name in values:
                raise AlipayProtocolError("notification body is invalid")
            values[name] = value
        signature = values.pop("sign", None)
        sign_type = values.pop("sign_type", None)
        if not signature or sign_type != ALIPAY_SIGN_TYPE:
            raise AlipayVerificationError("notification signature is invalid")
        self._verify_signature(_canonical_params(values), signature)
        return values

    def verify_and_parse_notification(
        self, headers: Mapping[str, str], raw_body: bytes
    ) -> TrustedPaymentEvent:
        del headers  # Alipay signs its form body; no trusted header is used.
        data = self._notification_params(raw_body)
        event_id = _required_string(data, "notify_id", 128)
        return self._success_event(
            data,
            event_id=event_id,
            payload_hash=hashlib.sha256(raw_body).hexdigest(),
            source="callback",
        )

    def query_payment(self, provider_order_ref: str) -> PaymentQueryResult:
        if not provider_order_ref or len(provider_order_ref) > 64:
            raise AlipayProtocolError("provider order reference is invalid")
        data = self._request(
            "alipay.trade.query",
            {"out_trade_no": provider_order_ref},
            "alipay_trade_query_response",
        )
        code = _required_string(data, "code", 16)
        if code != "10000":
            if data.get("sub_code") == "ACQ.TRADE_NOT_EXIST":
                return PaymentQueryResult(
                    provider=self.code,
                    provider_order_ref=provider_order_ref,
                    state="NOT_EXIST",
                    status="pending",
                    success=None,
                )
            raise AlipayProtocolError("Alipay payment query was rejected")
        if _required_string(data, "out_trade_no", 64) != provider_order_ref:
            raise AlipayVerificationError("query order reference mismatch")
        if _required_string(data, "seller_id", 64) != self.merchant_id:
            raise AlipayVerificationError("query merchant identity mismatch")
        trade_status = _required_string(data, "trade_status", 32)
        if trade_status in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            event_data = dict(data)
            event_data["app_id"] = self.app_id
            success = self._success_event(
                event_data,
                event_id=f"query:{_required_string(data, 'trade_no', 64)}",
                payload_hash=hashlib.sha256(raw).hexdigest(),
                source="query",
            )
            return PaymentQueryResult(
                provider=self.code,
                provider_order_ref=provider_order_ref,
                state=trade_status,
                status="succeeded",
                success=success,
            )
        if trade_status == "WAIT_BUYER_PAY":
            return PaymentQueryResult(
                provider=self.code,
                provider_order_ref=provider_order_ref,
                state=trade_status,
                status="pending",
                success=None,
            )
        if trade_status == "TRADE_CLOSED":
            return PaymentQueryResult(
                provider=self.code,
                provider_order_ref=provider_order_ref,
                state=trade_status,
                status="closed",
                success=None,
            )
        raise AlipayProtocolError("query returned unknown payment state")

    def close_payment(self, provider_order_ref: str) -> None:
        if not provider_order_ref or len(provider_order_ref) > 64:
            raise AlipayProtocolError("provider order reference is invalid")
        data = self._request(
            "alipay.trade.close",
            {"out_trade_no": provider_order_ref},
            "alipay_trade_close_response",
        )
        code = _required_string(data, "code", 16)
        if code == "10000" or data.get("sub_code") == "ACQ.TRADE_NOT_EXIST":
            return
        raise AlipayProtocolError("Alipay payment close was rejected")


def get_alipay_provider() -> AlipayProvider:
    return AlipayProvider(load_alipay_config())
