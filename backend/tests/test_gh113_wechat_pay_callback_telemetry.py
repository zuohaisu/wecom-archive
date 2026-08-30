"""GitHub #113: safe telemetry for rejected WeChat Pay callbacks."""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import PaymentRecoveryFinding
from app.db.session import get_db
from app.routers import billing
from app.services.wechat_pay import (
    WechatPayConfig,
    WechatPayProvider,
    WechatPaySignatureFailureClass,
    WechatPaySignatureVerificationError,
)

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)
PUBLIC_KEY_ID = "PUB_KEY_ID_GH113"
ROUTE = "/api/payments/wechat/notify"


@pytest.fixture
def provider() -> WechatPayProvider:
    wechat_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    config = WechatPayConfig(
        app_id="wx-gh113",
        merchant_id="1900000113",
        merchant_serial_no="merchant-serial-gh113",
        # These sentinels prove rejected-callback telemetry does not render
        # provider configuration. _verify_headers never uses the merchant key.
        merchant_private_key="SUPER_SECRET_PRIVATE_KEY_VALUE",  # type: ignore[arg-type]
        api_v3_key=b"SUPER_SECRET_API_V3_KEY_VALUE",
        public_key_id=PUBLIC_KEY_ID,
        public_key=wechat_private.public_key(),
        notify_url="https://billing.example.test/api/payments/wechat/notify",
        refund_notify_url="https://billing.example.test/api/refunds/wechat/notify",
    )
    return WechatPayProvider(config, now=lambda: NOW)


def _headers(case: str) -> dict[str, str]:
    values = {
        "Wechatpay-Serial": PUBLIC_KEY_ID,
        "Wechatpay-Timestamp": str(int(NOW.timestamp())),
        "Wechatpay-Nonce": "nonce-gh113",
        "Wechatpay-Signature": base64.b64encode(b"signature-mismatch").decode(),
    }
    if case == "missing":
        return {}
    if case == "unknown_serial":
        values["Wechatpay-Serial"] = "PUB_KEY_ID_UNKNOWN_GH113"
    elif case == "invalid_timestamp":
        values["Wechatpay-Timestamp"] = "not-a-timestamp"
    elif case == "stale_timestamp":
        values["Wechatpay-Timestamp"] = "0"
    elif case == "invalid_encoding":
        values["Wechatpay-Signature"] = "SUPER_SECRET_SIGNATURE_VALUE"
    elif case == "signtest":
        values["Wechatpay-Signature"] = "SIGNTEST"
    return values


@pytest.mark.parametrize(
    ("case", "expected_class", "serial_match", "sign_test"),
    [
        ("missing", "MISSING_SIGNATURE_HEADERS", False, False),
        ("unknown_serial", "UNKNOWN_PUBLIC_KEY_ID", False, False),
        ("invalid_timestamp", "INVALID_TIMESTAMP", True, False),
        ("stale_timestamp", "STALE_TIMESTAMP", True, False),
        ("invalid_encoding", "INVALID_SIGNATURE_ENCODING", True, False),
        ("mismatch", "SIGNATURE_MISMATCH", True, False),
        ("signtest", "SIGNTEST", True, True),
    ],
)
def test_provider_exposes_typed_verification_failure_metadata(
    provider: WechatPayProvider,
    case: str,
    expected_class: str,
    serial_match: bool,
    sign_test: bool,
) -> None:
    with pytest.raises(WechatPaySignatureVerificationError) as raised:
        provider._verify_headers(_headers(case), b"callback-body-gh113")

    error = raised.value
    assert error.failure_class.value == expected_class
    assert error.metadata.serial_match is serial_match
    assert error.metadata.sign_test is sign_test
    assert error.metadata.header_serial_present is (case != "missing")
    assert error.metadata.header_timestamp_present is (case != "missing")
    assert error.metadata.header_nonce_present is (case != "missing")
    assert error.metadata.header_signature_present is (case != "missing")


def test_signtest_still_attempts_rsa_verification(provider: WechatPayProvider) -> None:
    class RejectingPublicKey:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def verify(self, *args: object) -> None:
            self.calls.append(args)
            raise InvalidSignature

    public_key = RejectingPublicKey()
    provider = WechatPayProvider(
        replace(provider._config, public_key=public_key),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    with pytest.raises(WechatPaySignatureVerificationError) as raised:
        provider._verify_headers(_headers("signtest"), b"callback-body-gh113")

    assert raised.value.failure_class is WechatPaySignatureFailureClass.SIGNTEST
    assert public_key.calls


@pytest.fixture
def callback_client(provider: WechatPayProvider):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[PaymentRecoveryFinding.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(billing.router)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[billing.get_wechat_payment_provider] = lambda: provider
    return TestClient(app), factory


@pytest.mark.parametrize(
    ("case", "expected_class", "serial_match", "sign_test"),
    [
        ("missing", "MISSING_SIGNATURE_HEADERS", False, False),
        ("unknown_serial", "UNKNOWN_PUBLIC_KEY_ID", False, False),
        ("invalid_timestamp", "INVALID_TIMESTAMP", True, False),
        ("stale_timestamp", "STALE_TIMESTAMP", True, False),
        ("invalid_encoding", "INVALID_SIGNATURE_ENCODING", True, False),
        ("mismatch", "SIGNATURE_MISMATCH", True, False),
        ("signtest", "SIGNTEST", True, True),
    ],
)
def test_rejected_callback_is_safe_and_emits_structured_telemetry(
    callback_client,
    monkeypatch,
    caplog,
    case: str,
    expected_class: str,
    serial_match: bool,
    sign_test: bool,
) -> None:
    client, factory = callback_client
    raw_body = b"SUPER_SECRET_BODY_VALUE"

    def unexpected_payment_mutation(*args, **kwargs):
        raise AssertionError("untrusted callback must not apply a payment")

    monkeypatch.setattr(billing, "apply_trusted_payment", unexpected_payment_mutation)
    caplog.set_level(logging.WARNING, logger=billing.logger.name)

    response = client.post(ROUTE, content=raw_body, headers=_headers(case))

    assert response.status_code == 400
    assert response.json() == {"code": "FAIL", "message": "invalid notification"}
    with factory() as db:
        assert db.scalars(select(PaymentRecoveryFinding)).all() == []

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == billing.logger.name
        and "event=wechat_pay_callback_verification_rejected" in record.getMessage()
    ]
    assert len(messages) == 1
    message = messages[0]
    assert f"failure_class={expected_class}" in message
    assert f"route={ROUTE}" in message
    assert "header_serial_present=" + str(case != "missing") in message
    assert "header_timestamp_present=" + str(case != "missing") in message
    assert "header_nonce_present=" + str(case != "missing") in message
    assert "header_signature_present=" + str(case != "missing") in message
    assert f"serial_match={serial_match}" in message
    assert f"sign_test={sign_test}" in message
    assert f"body_length={len(raw_body)}" in message
    assert f"body_sha256={hashlib.sha256(raw_body).hexdigest()}" in message


def test_rejection_telemetry_never_logs_callback_or_provider_secrets(
    callback_client, caplog
) -> None:
    client, _factory = callback_client
    raw_body = b"SUPER_SECRET_BODY_VALUE"
    caplog.set_level(logging.WARNING, logger=billing.logger.name)

    response = client.post(ROUTE, content=raw_body, headers=_headers("invalid_encoding"))

    assert response.status_code == 400
    telemetry = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == billing.logger.name
    )
    for secret in (
        "SUPER_SECRET_SIGNATURE_VALUE",
        "SUPER_SECRET_BODY_VALUE",
        "SUPER_SECRET_API_V3_KEY_VALUE",
        "SUPER_SECRET_PRIVATE_KEY_VALUE",
    ):
        assert secret not in telemetry
