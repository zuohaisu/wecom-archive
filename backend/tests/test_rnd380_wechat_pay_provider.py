from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.payment_provider import PaymentRequest
from app.services.wechat_pay import (
    WechatPayConfig,
    WechatPayConfigurationError,
    WechatPayNotConfiguredError,
    WechatPayProvider,
    WechatPayVerificationError,
    load_wechat_pay_config,
)
from app.settings import WechatPaySettings

NOW = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
API_KEY = b"0123456789abcdef0123456789abcdef"
APP_ID = "wx-rnd380-app"
MCH_ID = "1900003801"
PUBLIC_KEY_ID = "PUB_KEY_ID_RND380"
SERIAL = "RND380MERCHANTSERIAL"


@pytest.fixture(scope="module")
def key_material():
    merchant_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wechat_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return merchant_private, wechat_private


def _config(key_material) -> WechatPayConfig:
    merchant_private, wechat_private = key_material
    return WechatPayConfig(
        app_id=APP_ID,
        merchant_id=MCH_ID,
        merchant_serial_no=SERIAL,
        merchant_private_key=merchant_private,
        api_v3_key=API_KEY,
        public_key_id=PUBLIC_KEY_ID,
        public_key=wechat_private.public_key(),
        notify_url="https://billing.example.test/api/payments/wechat/notify",
        refund_notify_url="https://billing.example.test/api/refunds/wechat/notify",
    )


def _signed_headers(private_key, body: bytes, *, at: datetime = NOW):
    timestamp = str(int(at.timestamp()))
    nonce = "response-nonce-rnd380"
    message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
    signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return {
        "Wechatpay-Serial": PUBLIC_KEY_ID,
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Signature": base64.b64encode(signature).decode(),
    }


def _assert_merchant_signature(request: httpx.Request, merchant_public_key) -> dict:
    authorization = request.headers["Authorization"]
    assert authorization.startswith("WECHATPAY2-SHA256-RSA2048 ")
    fields = dict(re.findall(r'(\w+)="([^"]+)"', authorization))
    assert fields["mchid"] == MCH_ID
    assert fields["serial_no"] == SERIAL
    body = request.content.decode()
    canonical_url = request.url.raw_path.decode()
    message = (
        f"{request.method}\n{canonical_url}\n{fields['timestamp']}\n"
        f"{fields['nonce_str']}\n{body}\n"
    ).encode()
    merchant_public_key.verify(
        base64.b64decode(fields["signature"]),
        message,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return json.loads(body) if body else {}


def test_native_create_query_and_close_use_signed_api_v3_requests(key_material) -> None:
    merchant_private, wechat_private = key_material
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _assert_merchant_signature(request, merchant_private.public_key())
        seen.append((request.method, request.url.raw_path.decode(), payload))
        if request.url.path == "/v3/pay/transactions/native":
            body = json.dumps(
                {"code_url": "weixin://wxpay/bizpayurl?pr=rnd380"},
                separators=(",", ":"),
            ).encode()
            return httpx.Response(
                200,
                content=body,
                headers=_signed_headers(wechat_private, body),
            )
        if request.method == "GET":
            body = json.dumps(
                {
                    "appid": APP_ID,
                    "mchid": MCH_ID,
                    "out_trade_no": "W-rnd380-order",
                    "transaction_id": "42000000000000000000000380",
                    "trade_state": "SUCCESS",
                    "success_time": "2026-08-13T16:00:00+08:00",
                    "amount": {"total": 9900, "currency": "CNY"},
                },
                separators=(",", ":"),
            ).encode()
            return httpx.Response(
                200,
                content=body,
                headers=_signed_headers(wechat_private, body),
            )
        return httpx.Response(204)

    provider = WechatPayProvider(
        _config(key_material),
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
        nonce=lambda: "merchant-request-nonce",
    )
    artifact = provider.create_payment(
        PaymentRequest(
            provider_order_ref="W-rnd380-order",
            description="365企微会话存档-年度基础套餐",
            amount_cents=9900,
            currency="CNY",
            expires_at=NOW + timedelta(minutes=15),
        )
    )
    queried = provider.query_payment("W-rnd380-order")
    provider.close_payment("W-rnd380-order")

    assert artifact.kind == "qr_code"
    assert artifact.value == "weixin://wxpay/bizpayurl?pr=rnd380"
    assert queried.status == "succeeded"
    assert queried.success is not None
    assert queried.success.amount_cents == 9900
    assert queried.success.succeeded_at == NOW
    create_payload = seen[0][2]
    assert create_payload["appid"] == APP_ID
    assert create_payload["mchid"] == MCH_ID
    assert create_payload["amount"] == {"total": 9900, "currency": "CNY"}
    assert create_payload["notify_url"].endswith("/api/payments/wechat/notify")
    assert seen[1][1].endswith(f"?mchid={MCH_ID}")
    assert seen[2][2] == {"mchid": MCH_ID}


def _notification(
    key_material,
    *,
    event_id: str = "EV-RND380-1",
    app_id: str = APP_ID,
    merchant_id: str = MCH_ID,
    amount: int = 9900,
):
    _, wechat_private = key_material
    transaction = {
        "appid": app_id,
        "mchid": merchant_id,
        "out_trade_no": "W-rnd380-order",
        "transaction_id": "42000000000000000000000380",
        "trade_state": "SUCCESS",
        "success_time": "2026-08-13T16:00:00+08:00",
        "amount": {"total": amount, "currency": "CNY"},
    }
    nonce = "notify-nonce"
    associated_data = "transaction"
    ciphertext = AESGCM(API_KEY).encrypt(
        nonce.encode(),
        json.dumps(transaction, separators=(",", ":")).encode(),
        associated_data.encode(),
    )
    envelope = {
        "id": event_id,
        "event_type": "TRANSACTION.SUCCESS",
        "resource_type": "encrypt-resource",
        "resource": {
            "original_type": "transaction",
            "algorithm": "AEAD_AES_256_GCM",
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "associated_data": associated_data,
            "nonce": nonce,
        },
    }
    body = json.dumps(envelope, separators=(",", ":")).encode()
    return body, _signed_headers(wechat_private, body)


def test_signed_notification_is_decrypted_into_minimal_trusted_event(key_material) -> None:
    body, headers = _notification(key_material)
    provider = WechatPayProvider(_config(key_material), now=lambda: NOW)

    event = provider.verify_and_parse_notification(headers, body)

    assert event.provider == "wechat_pay"
    assert event.provider_event_id == "EV-RND380-1"
    assert event.provider_order_ref == "W-rnd380-order"
    assert event.provider_transaction_id == "42000000000000000000000380"
    assert event.amount_cents == 9900
    assert event.currency == "CNY"
    assert event.succeeded_at == NOW
    assert len(event.payload_hash) == 64
    assert event.source == "callback"


@pytest.mark.parametrize("failure", ["signature", "serial", "timestamp", "merchant"])
def test_notification_fails_closed_on_untrusted_security_fields(
    key_material, failure: str
) -> None:
    body, headers = _notification(
        key_material,
        merchant_id="wrong-merchant" if failure == "merchant" else MCH_ID,
    )
    if failure == "signature":
        headers["Wechatpay-Signature"] = base64.b64encode(b"invalid").decode()
    elif failure == "serial":
        headers["Wechatpay-Serial"] = "PUB_KEY_ID_UNKNOWN"
    elif failure == "timestamp":
        _, wechat_private = key_material
        headers = _signed_headers(wechat_private, body, at=NOW - timedelta(minutes=6))
    provider = WechatPayProvider(_config(key_material), now=lambda: NOW)

    with pytest.raises(WechatPayVerificationError):
        provider.verify_and_parse_notification(headers, body)


def _pem_private(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _pem_public(key) -> str:
    return key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def test_configuration_is_explicit_complete_and_secret_safe(key_material) -> None:
    merchant_private, wechat_private = key_material
    with pytest.raises(WechatPayNotConfiguredError):
        load_wechat_pay_config(WechatPaySettings(wechat_pay_enabled="false"))

    secret_marker = "do-not-leak-api-key-material"
    with pytest.raises(WechatPayConfigurationError) as missing:
        load_wechat_pay_config(
            WechatPaySettings(
                wechat_pay_enabled="true",
                wechat_pay_api_v3_key=secret_marker,
            )
        )
    assert secret_marker not in str(missing.value)

    settings = WechatPaySettings(
        wechat_pay_enabled="true",
        wechat_pay_app_id=APP_ID,
        wechat_pay_mch_id=MCH_ID,
        wechat_pay_merchant_serial_no=SERIAL,
        wechat_pay_merchant_private_key=_pem_private(merchant_private),
        wechat_pay_api_v3_key=API_KEY.decode(),
        wechat_pay_public_key_id=PUBLIC_KEY_ID,
        wechat_pay_public_key=_pem_public(wechat_private.public_key()),
        wechat_pay_notify_url="https://billing.example.test/api/payments/wechat/notify",
        wechat_pay_refund_notify_url="https://billing.example.test/api/refunds/wechat/notify",
    )
    config = load_wechat_pay_config(settings)

    assert config.app_id == APP_ID
    rendered = repr(config)
    assert API_KEY.decode() not in rendered
    assert "BEGIN PRIVATE KEY" not in rendered


def test_signed_response_tampering_is_rejected_before_json_is_trusted(key_material) -> None:
    _, wechat_private = key_material

    def handler(_request: httpx.Request) -> httpx.Response:
        signed = b'{"code_url":"weixin://wxpay/bizpayurl?pr=original"}'
        tampered = b'{"code_url":"weixin://wxpay/bizpayurl?pr=tampered"}'
        return httpx.Response(
            200,
            content=tampered,
            headers=_signed_headers(wechat_private, signed),
        )

    provider = WechatPayProvider(
        _config(key_material),
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
    )
    with pytest.raises(WechatPayVerificationError):
        provider.create_payment(
            PaymentRequest(
                provider_order_ref="W-rnd380-order",
                description="annual",
                amount_cents=9900,
                currency="CNY",
                expires_at=NOW + timedelta(minutes=15),
            )
        )
