from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.services.alipay import (
    ALIPAY_GATEWAY_URL,
    AlipayConfig,
    AlipayConfigurationError,
    AlipayProtocolError,
    AlipayProvider,
    AlipayVerificationError,
    load_alipay_config,
)
from app.services.payment_provider import PaymentRequest
from app.settings import AlipaySettings

NOW = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
APP_ID = "2026082300000416"
SELLER_ID = "2088230000000416"
OUT_TRADE_NO = "A-rnd416-order"
TRADE_NO = "2026082322001416000000000001"


@pytest.fixture(scope="module")
def key_material():
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


def _config(key_material) -> AlipayConfig:
    merchant_private, alipay_private = key_material
    return AlipayConfig(
        app_id=APP_ID,
        seller_id=SELLER_ID,
        merchant_private_key=merchant_private,
        alipay_public_key=alipay_private.public_key(),
        notify_url="https://billing.example.test/api/payments/alipay/notify",
        return_url="https://billing.example.test/admin/billing",
    )


def _canonical(params: dict[str, str]) -> bytes:
    return "&".join(f"{key}={params[key]}" for key in sorted(params)).encode("utf-8")


def _sign(private_key, content: bytes) -> str:
    return base64.b64encode(
        private_key.sign(content, padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")


def _notification(key_material, **changes) -> bytes:
    _, alipay_private = key_material
    values = {
        "notify_id": "notify-rnd416-001",
        "notify_time": "2026-08-23 16:00:01",
        "app_id": APP_ID,
        "seller_id": SELLER_ID,
        "out_trade_no": OUT_TRADE_NO,
        "trade_no": TRADE_NO,
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "99.00",
        "gmt_payment": "2026-08-23 16:00:00",
    }
    values.update(changes)
    signature = _sign(alipay_private, _canonical(values))
    return urlencode(
        {"sign_type": "RSA2", "sign": signature, **values}
    ).encode("utf-8")


def _signed_response(private_key, name: str, body: dict) -> bytes:
    content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = _sign(private_key, content)
    return (
        b'{"'
        + name.encode("ascii")
        + b'":'
        + content
        + b',"sign":"'
        + signature.encode("ascii")
        + b'","sign_type":"RSA2"}'
    )


def _request() -> PaymentRequest:
    return PaymentRequest(
        provider_order_ref=OUT_TRADE_NO,
        description="365企微会话存档-年度基础套餐",
        amount_cents=9900,
        currency="CNY",
        expires_at=NOW + timedelta(minutes=15),
    )


def test_page_payment_redirect_is_rsa2_signed_and_contains_only_protocol_fields(
    key_material,
) -> None:
    merchant_private, _ = key_material
    provider = AlipayProvider(_config(key_material), now=lambda: NOW)

    artifact = provider.create_payment(_request())

    assert artifact.kind == "redirect"
    parsed = urlparse(artifact.value)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == ALIPAY_GATEWAY_URL
    params = {key: value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
    assert params.pop("sign_type") == "RSA2"
    signature = params.pop("sign")
    merchant_private.public_key().verify(
        base64.b64decode(signature), _canonical(params), padding.PKCS1v15(), hashes.SHA256()
    )
    assert params["method"] == "alipay.trade.page.pay"
    assert params["notify_url"] == "https://billing.example.test/api/payments/alipay/notify"
    assert params["return_url"] == "https://billing.example.test/admin/billing"
    assert json.loads(params["biz_content"]) == {
        "out_trade_no": OUT_TRADE_NO,
        "product_code": "FAST_INSTANT_TRADE_PAY",
        "total_amount": "99.00",
        "subject": "365企微会话存档-年度基础套餐",
        "timeout_express": "15m",
    }


def test_signed_notification_becomes_minimal_trusted_event(key_material) -> None:
    provider = AlipayProvider(_config(key_material), now=lambda: NOW)

    event = provider.verify_and_parse_notification({}, _notification(key_material))

    assert event.provider == "alipay"
    assert event.provider_event_id == "notify-rnd416-001"
    assert event.provider_order_ref == OUT_TRADE_NO
    assert event.provider_transaction_id == TRADE_NO
    assert event.state == "SUCCESS"
    assert event.amount_cents == 9900
    assert event.currency == "CNY"
    assert event.succeeded_at == NOW
    assert event.source == "callback"
    assert len(event.payload_hash) == 64


@pytest.mark.parametrize(
    "changes",
    [
        {"app_id": "2026082300000999"},
        {"seller_id": "2088230000000999"},
        {"trade_status": "WAIT_BUYER_PAY"},
    ],
)
def test_notification_fails_closed_when_signed_fields_do_not_match(key_material, changes) -> None:
    provider = AlipayProvider(_config(key_material), now=lambda: NOW)

    with pytest.raises((AlipayProtocolError, AlipayVerificationError)):
        provider.verify_and_parse_notification({}, _notification(key_material, **changes))


def test_tampered_signature_fails_before_notification_is_trusted(key_material) -> None:
    provider = AlipayProvider(_config(key_material), now=lambda: NOW)
    values = dict(parse_qsl(_notification(key_material).decode("utf-8")))
    values["sign"] = base64.b64encode(b"invalid").decode("ascii")

    with pytest.raises(AlipayVerificationError):
        provider.verify_and_parse_notification({}, urlencode(values).encode("utf-8"))


def test_signed_query_success_and_close_validate_alipay_response(key_material) -> None:
    merchant_private, alipay_private = key_material
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.content.decode("utf-8"), keep_blank_values=True))
        signature = params.pop("sign")
        assert params.pop("sign_type") == "RSA2"
        merchant_private.public_key().verify(
            base64.b64decode(signature), _canonical(params), padding.PKCS1v15(), hashes.SHA256()
        )
        seen.append(params)
        if params["method"] == "alipay.trade.query":
            body = {
                "code": "10000",
                "out_trade_no": OUT_TRADE_NO,
                "trade_no": TRADE_NO,
                "seller_id": SELLER_ID,
                "trade_status": "TRADE_SUCCESS",
                "total_amount": "99.00",
                "send_pay_date": "2026-08-23 16:00:00",
            }
            response = _signed_response(
                alipay_private, "alipay_trade_query_response", body
            )
        else:
            assert params["method"] == "alipay.trade.close"
            assert json.loads(params["biz_content"]) == {"out_trade_no": OUT_TRADE_NO}
            response = _signed_response(
                alipay_private, "alipay_trade_close_response", {"code": "10000"}
            )
        return httpx.Response(200, content=response)

    provider = AlipayProvider(
        _config(key_material), transport=httpx.MockTransport(handler), now=lambda: NOW
    )
    result = provider.query_payment(OUT_TRADE_NO)
    provider.close_payment(OUT_TRADE_NO)

    assert result.status == "succeeded"
    assert result.state == "TRADE_SUCCESS"
    assert result.success is not None
    assert result.success.provider_transaction_id == TRADE_NO
    assert result.success.source == "query"
    assert [item["method"] for item in seen] == [
        "alipay.trade.query",
        "alipay.trade.close",
    ]


def test_missing_alipay_trade_is_pending_and_tampered_query_response_is_rejected(
    key_material,
) -> None:
    _, alipay_private = key_material
    missing = _signed_response(
        alipay_private,
        "alipay_trade_query_response",
        {"code": "40004", "sub_code": "ACQ.TRADE_NOT_EXIST"},
    )
    provider = AlipayProvider(
        _config(key_material),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=missing)),
        now=lambda: NOW,
    )
    assert provider.query_payment(OUT_TRADE_NO).status == "pending"

    tampered = missing.replace(b"TRADE_NOT_EXIST", b"TRADE_NOT_XEXST")
    broken = AlipayProvider(
        _config(key_material),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=tampered)),
        now=lambda: NOW,
    )
    with pytest.raises(AlipayVerificationError):
        broken.query_payment(OUT_TRADE_NO)


def test_enabled_incomplete_alipay_configuration_stops_app_startup(monkeypatch) -> None:
    from app.main import create_app

    monkeypatch.setenv("ALIPAY_ENABLED", "true")
    for name in (
        "ALIPAY_APP_ID",
        "ALIPAY_SELLER_ID",
        "ALIPAY_MERCHANT_PRIVATE_KEY",
        "ALIPAY_PUBLIC_KEY",
        "ALIPAY_NOTIFY_URL",
        "ALIPAY_RETURN_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(AlipayConfigurationError):
        create_app()


def _private_pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _public_pem(key) -> str:
    return key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def test_enabled_configuration_is_complete_validated_and_secret_safe(key_material) -> None:
    merchant_private, alipay_private = key_material
    secret_marker = "do-not-leak-rnd416-private-key"
    with pytest.raises(AlipayConfigurationError) as missing:
        load_alipay_config(
            AlipaySettings(
                alipay_enabled="true", alipay_merchant_private_key=secret_marker
            )
        )
    assert secret_marker not in str(missing.value)

    config = load_alipay_config(
        AlipaySettings(
            alipay_enabled="true",
            alipay_app_id=APP_ID,
            alipay_seller_id=SELLER_ID,
            alipay_merchant_private_key=_private_pem(merchant_private),
            alipay_public_key=_public_pem(alipay_private.public_key()),
            alipay_notify_url="https://billing.example.test/api/payments/alipay/notify",
            alipay_return_url="https://billing.example.test/admin/billing",
        )
    )
    assert config.app_id == APP_ID
    assert "BEGIN PRIVATE KEY" not in repr(config)
