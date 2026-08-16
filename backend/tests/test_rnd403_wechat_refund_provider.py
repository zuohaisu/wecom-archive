from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.payment_provider import RefundRequest
from app.services.wechat_pay import (
    WechatPayConfig,
    WechatPayProvider,
    WechatPayVerificationError,
)

NOW = datetime(2026, 8, 16, 8, 0, tzinfo=timezone.utc)
API_KEY = b"0123456789abcdef0123456789abcdef"
APP_ID = "wx-rnd403-app"
MCH_ID = "1900004031"
PUBLIC_KEY_ID = "PUB_KEY_ID_RND403"
SERIAL = "RND403MERCHANTSERIAL"
OUT_TRADE_NO = "W-rnd403-order"
TRANSACTION_ID = "420000000020260816403"
OUT_REFUND_NO = "R-rnd403-refund"
REFUND_ID = "503000000020260816403"


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
    nonce = "response-nonce-rnd403"
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
    canonical_url = request.url.raw_path.decode()
    if request.url.query:
        canonical_url += "?" + request.url.query.decode()
    body = request.content.decode()
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


def _refund_result(*, status: str = "PROCESSING") -> dict:
    result = {
        "refund_id": REFUND_ID,
        "out_refund_no": OUT_REFUND_NO,
        "transaction_id": TRANSACTION_ID,
        "out_trade_no": OUT_TRADE_NO,
        "status": status,
        "create_time": "2026-08-16T16:00:00+08:00",
        "amount": {"refund": 9900, "total": 9900, "currency": "CNY"},
    }
    if status == "SUCCESS":
        result["success_time"] = "2026-08-16T16:03:00+08:00"
    return result


def test_apply_and_query_refund_use_signed_api_and_preserve_full_amount(
    key_material,
) -> None:
    merchant_private, wechat_private = key_material
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _assert_merchant_signature(request, merchant_private.public_key())
        seen.append(request.url.raw_path.decode())
        if request.method == "POST":
            assert body == {
                "transaction_id": TRANSACTION_ID,
                "out_refund_no": OUT_REFUND_NO,
                "reason": "经审核的全额退款",
                "notify_url": "https://billing.example.test/api/refunds/wechat/notify",
                "amount": {"refund": 9900, "total": 9900, "currency": "CNY"},
            }
            response_body = json.dumps(
                _refund_result(), separators=(",", ":")
            ).encode()
        else:
            assert request.url.path.endswith(OUT_REFUND_NO)
            response_body = json.dumps(
                _refund_result(status="SUCCESS"), separators=(",", ":")
            ).encode()
        return httpx.Response(
            200,
            content=response_body,
            headers=_signed_headers(wechat_private, response_body),
        )

    provider = WechatPayProvider(
        _config(key_material),
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
        nonce=lambda: "merchant-nonce-rnd403",
    )
    submitted = provider.create_refund(
        RefundRequest(
            provider_ref=OUT_REFUND_NO,
            provider_order_ref=OUT_TRADE_NO,
            provider_transaction_id=TRANSACTION_ID,
            amount_cents=9900,
            total_amount_cents=9900,
            currency="CNY",
            reason="经审核的全额退款",
        )
    )
    queried = provider.query_refund(OUT_REFUND_NO)

    assert seen == [
        "/v3/refund/domestic/refunds",
        f"/v3/refund/domestic/refunds/{OUT_REFUND_NO}",
    ]
    assert submitted.state == "PROCESSING"
    assert submitted.provider_refund_id == REFUND_ID
    assert queried.state == "SUCCESS" and queried.source == "query"
    assert queried.occurred_at == datetime(2026, 8, 16, 8, 3, tzinfo=timezone.utc)


def _notification(
    private_key,
    *,
    event_type: str = "REFUND.SUCCESS",
    state: str = "SUCCESS",
    merchant_id: str = MCH_ID,
    ciphertext_mutator=None,
):
    resource_data = _refund_result(status=state)
    resource_data["refund_status"] = resource_data.pop("status")
    resource_data["mchid"] = merchant_id
    plaintext = json.dumps(resource_data, separators=(",", ":")).encode()
    nonce = "refundnonce"
    associated_data = "refund"
    ciphertext = AESGCM(API_KEY).encrypt(
        nonce.encode(), plaintext, associated_data.encode()
    )
    if ciphertext_mutator is not None:
        ciphertext = ciphertext_mutator(ciphertext)
    envelope = {
        "id": f"event-{state.lower()}",
        "create_time": "2026-08-16T16:04:00+08:00",
        "event_type": event_type,
        "resource_type": "encrypt-resource",
        "resource": {
            "algorithm": "AEAD_AES_256_GCM",
            "original_type": "refund",
            "nonce": nonce,
            "associated_data": associated_data,
            "ciphertext": base64.b64encode(ciphertext).decode(),
        },
    }
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    return raw, _signed_headers(private_key, raw)


@pytest.mark.parametrize(
    ("event_type", "state"),
    [
        ("REFUND.SUCCESS", "SUCCESS"),
        ("REFUND.ABNORMAL", "ABNORMAL"),
        ("REFUND.CLOSED", "CLOSED"),
    ],
)
def test_signed_encrypted_refund_notifications_are_trusted_only_after_decryption(
    key_material, event_type: str, state: str
) -> None:
    _, wechat_private = key_material
    raw, headers = _notification(
        wechat_private, event_type=event_type, state=state
    )
    provider = WechatPayProvider(_config(key_material), now=lambda: NOW)

    event = provider.verify_and_parse_refund_notification(headers, raw)

    assert event.state == state
    assert event.provider_ref == OUT_REFUND_NO
    assert event.provider_refund_id == REFUND_ID
    assert event.provider_order_ref == OUT_TRADE_NO
    assert event.provider_transaction_id == TRANSACTION_ID
    assert event.amount_cents == event.total_amount_cents == 9900


@pytest.mark.parametrize("failure", ["signature", "stale", "merchant", "ciphertext"])
def test_untrusted_refund_notifications_fail_closed(key_material, failure: str) -> None:
    _, wechat_private = key_material
    kwargs = {}
    if failure == "merchant":
        kwargs["merchant_id"] = "wrong-merchant"
    if failure == "ciphertext":
        kwargs["ciphertext_mutator"] = lambda value: value[:-1] + bytes(
            [value[-1] ^ 1]
        )
    raw, headers = _notification(wechat_private, **kwargs)
    if failure == "signature":
        headers["Wechatpay-Signature"] = base64.b64encode(b"invalid").decode()
    if failure == "stale":
        headers = _signed_headers(wechat_private, raw, at=NOW - timedelta(minutes=6))
    provider = WechatPayProvider(_config(key_material), now=lambda: NOW)

    with pytest.raises(WechatPayVerificationError):
        provider.verify_and_parse_refund_notification(headers, raw)
