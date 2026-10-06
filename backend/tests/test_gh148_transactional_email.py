"""Offline provider contract, log safety and durable business-state coverage."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import select

from app import email
from app.db.models import BillingNotificationAttempt, BillingNotificationIntent, Subscription
from app.services.billing_notifications import run_billing_notifications_once
from app.settings import EmailSettings
from tests.test_rnd401_billing_notifications import NOW, _subscription
from tests.test_rnd401_billing_notifications import factory  # noqa: F401 - fixture discovery

PROVIDER_ID = "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"
API_KEY = "synthetic-gh148-key-never-log"
RECIPIENT = "synthetic-owner@example.test"
LINK = "https://archive.example.test/reset?token=synthetic-private-token"
WHEN = datetime(2026, 10, 6, tzinfo=timezone.utc)


@pytest.fixture
def configured(monkeypatch):
    settings = EmailSettings(
        resend_api_key=API_KEY, email_provider="resend", app_env="production",
        smtp_host="smtp.qq.com", smtp_user="synthetic@qq.com",
        smtp_from="synthetic@qq.com", smtp_password="synthetic-smtp-secret",
        email_reply_to="", email_from="康冠时代会话存档 <notifications@mail.crowntime.cn>",
    )
    monkeypatch.setattr(email, "get_email_settings", lambda: settings)
    smtp = MagicMock(side_effect=AssertionError("normal path must not use SMTP"))
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", smtp)
    return settings, smtp


def _mock_provider(monkeypatch, handler):
    original_client = httpx.Client
    clients = []

    def client_factory(**kwargs):
        assert kwargs == {"timeout": 10.0, "trust_env": False}
        client = original_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(email.httpx, "Client", client_factory)
    return clients


@pytest.mark.parametrize("kind", ["reset", "invite", "export", "billing"])
def test_every_producer_uses_resend_and_approved_identity(configured, monkeypatch, caplog, kind):
    requests = []

    def accept(request):
        requests.append(request)
        return httpx.Response(200, json={"id": PROVIDER_ID})

    clients = _mock_provider(monkeypatch, accept)
    caplog.set_level(logging.DEBUG)
    send = {
        "reset": lambda: email.send_password_reset_email(RECIPIENT, LINK),
        "invite": lambda: email.send_invite_email(RECIPIENT, LINK),
        "export": lambda: email.send_export_ready_email(RECIPIENT, LINK, WHEN),
        "billing": lambda: email.send_billing_notification_email(
            RECIPIENT, "subscription_expiry_30d", WHEN, LINK,
            operation_id="billing-intent/synthetic",
        ),
    }[kind]
    assert send() is True
    assert len(requests) == 1
    request = requests[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert payload["from"] == configured[0].email_from
    assert payload["from"] == "康冠时代会话存档 <notifications@mail.crowntime.cn>"
    assert "qq.com" not in payload["from"]
    assert payload["to"] == [RECIPIENT]
    assert "reply_to" not in payload
    assert "NON-PRODUCTION" not in payload["subject"]
    assert request.headers["Idempotency-Key"].startswith("transactional/")
    assert clients[0].is_closed
    configured[1].assert_not_called()
    assert f"provider_id={PROVIDER_ID}" in caplog.text
    for secret in (API_KEY, RECIPIENT, LINK, "synthetic-smtp-secret", "Authorization"):
        assert secret not in caplog.text


@pytest.mark.parametrize("status", [301, 400, 401, 403, 409, 429, 500, 503])
def test_rejection_is_observable_without_response_body_or_fallback(configured, monkeypatch, caplog, status):
    calls = []

    def reject(request):
        calls.append(request)
        return httpx.Response(status, json={"error": f"{API_KEY} {LINK} {RECIPIENT}"})

    _mock_provider(monkeypatch, reject)
    assert email.send_password_reset_email(RECIPIENT, LINK) is False
    assert len(calls) == 1
    assert f"status={status}" in caplog.text
    assert API_KEY not in caplog.text and LINK not in caplog.text
    configured[1].assert_not_called()


@pytest.mark.parametrize("error_type,outcome", [
    (httpx.ReadTimeout, "timeout_unknown"),
    (httpx.ConnectError, "network_unknown"),
    (RuntimeError, "failed_or_unknown"),
])
def test_network_exception_is_sanitized_and_not_retried(configured, monkeypatch, caplog, error_type, outcome):
    calls = []

    def fail(request):
        calls.append(request)
        logging.getLogger("httpcore.http11").debug("Authorization: %s body=%s", API_KEY, LINK)
        raise error_type(f"{API_KEY} {RECIPIENT} {LINK}")

    clients = _mock_provider(monkeypatch, fail)
    caplog.set_level(logging.DEBUG)
    assert email.send_invite_email(RECIPIENT, LINK) is False
    assert len(calls) == 1 and clients[0].is_closed
    assert outcome in caplog.text
    assert API_KEY not in caplog.text and RECIPIENT not in caplog.text and LINK not in caplog.text
    # The scoped filter must not suppress unrelated HTTP logging after a send.
    logging.getLogger("httpcore.http11").debug("unrelated-http-diagnostic")
    assert "unrelated-http-diagnostic" in caplog.text
    configured[1].assert_not_called()


@pytest.mark.parametrize("data", [{}, [], {"id": ""}, {"id": "bad\nsecret"}, {"id": None}])
def test_invalid_success_response_is_not_success(configured, monkeypatch, data):
    _mock_provider(monkeypatch, lambda _: httpx.Response(200, json=data))
    assert not email.send_invite_email(RECIPIENT, LINK)


def test_non_json_response_is_sanitized(configured, monkeypatch, caplog):
    _mock_provider(monkeypatch, lambda _: httpx.Response(200, text=API_KEY + LINK))
    assert not email.send_invite_email(RECIPIENT, LINK)
    assert API_KEY not in caplog.text and LINK not in caplog.text


@pytest.mark.parametrize("field,value", [
    ("resend_api_key", ""), ("resend_api_key", "key\nheader"),
    ("email_provider", "unknown"), ("email_provider", "console"),
    ("email_from", "personal@qq.com"), ("email_from", "notifications@crowntime.cn"),
    ("email_from", "notifications@mail.crowntime.cn.attacker.test"),
    ("email_from", "one@mail.crowntime.cn,two@mail.crowntime.cn"),
    ("email_from", "notifications@mail.crowntime.cn\nBcc: private@example.test"),
    ("email_reply_to", "broken"),
])
def test_incomplete_or_unsafe_configuration_fails_closed(configured, monkeypatch, caplog, field, value):
    setattr(configured[0], field, value)
    client = MagicMock(side_effect=AssertionError("must not contact provider"))
    monkeypatch.setattr(email.httpx, "Client", client)
    assert not email.email_delivery_ready()
    assert not email.send_password_reset_email(RECIPIENT, LINK)
    assert "configuration_incomplete" in caplog.text
    client.assert_not_called()
    configured[1].assert_not_called()


def test_settings_failure_is_sanitized(monkeypatch, caplog):
    def fail():
        raise ValueError(API_KEY)

    monkeypatch.setattr(email, "get_email_settings", fail)
    assert not email.email_delivery_ready()
    assert not email.send_password_reset_email(RECIPIENT, LINK)
    assert API_KEY not in caplog.text


def test_invalid_recipient_fails_without_request(configured, monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(email.httpx, "Client", client)
    assert not email.send_invite_email("one@example.test,two@example.test", LINK)
    client.assert_not_called()


def test_retry_has_stable_payload_key_and_new_token_has_new_key(configured, monkeypatch):
    requests = []

    def attempt(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("unknown outcome")
        return httpx.Response(200, json={"id": PROVIDER_ID})

    _mock_provider(monkeypatch, attempt)
    assert not email.send_password_reset_email(RECIPIENT, LINK)
    assert email.send_password_reset_email(RECIPIENT, LINK)
    assert email.send_password_reset_email(RECIPIENT, LINK + "-new")
    assert requests[0].content == requests[1].content
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert requests[1].headers["Idempotency-Key"] != requests[2].headers["Idempotency-Key"]
    assert API_KEY not in requests[0].headers["Idempotency-Key"]


def test_nonproduction_marker_and_explicit_reply_to(configured, monkeypatch):
    configured[0].app_env = "staging"
    configured[0].email_reply_to = "Support <support@example.test>"

    def accept(request):
        payload = json.loads(request.content)
        assert payload["subject"].startswith("[NON-PRODUCTION] ")
        assert payload["reply_to"] == configured[0].email_reply_to
        return httpx.Response(200, json={"id": PROVIDER_ID})

    _mock_provider(monkeypatch, accept)
    assert email.send_invite_email(RECIPIENT, LINK)


@pytest.mark.parametrize("outcome", ["accepted", "refused", "exception", "invalid_port"])
def test_smtp_requires_explicit_selection_and_is_bounded(configured, monkeypatch, caplog, outcome):
    configured[0].email_provider = "smtp"
    configured[0].smtp_port = "broken" if outcome == "invalid_port" else "465"
    configured[0].email_reply_to = "support@example.test"
    smtp = MagicMock()
    connection = smtp.return_value.__enter__.return_value
    connection.send_message.return_value = {} if outcome == "accepted" else {RECIPIENT: (550, b"refused")}
    if outcome == "exception":
        connection.login.side_effect = RuntimeError("synthetic-smtp-secret")
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", smtp)
    client = MagicMock()
    monkeypatch.setattr(email.httpx, "Client", client)
    assert email.send_invite_email(RECIPIENT, LINK) is (outcome == "accepted")
    client.assert_not_called()
    if outcome == "invalid_port":
        smtp.assert_not_called()
    else:
        assert smtp.call_args.kwargs["timeout"] == 10.0
    if outcome == "accepted":
        message = connection.send_message.call_args.args[0]
        assert str(message["From"]) == "synthetic@qq.com"
        assert str(message["Reply-To"]) == "support@example.test"
    assert "synthetic-smtp-secret" not in caplog.text


def test_owner_approved_sender_default_and_environment_override(monkeypatch):
    approved = "康冠时代会话存档 <notifications@mail.crowntime.cn>"
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    settings = EmailSettings(_env_file=None, resend_api_key=API_KEY)
    assert settings.email_from == approved
    assert email._configuration_ready(settings)
    monkeypatch.setenv("EMAIL_FROM", approved)
    assert EmailSettings(_env_file=None, resend_api_key=API_KEY).email_from == approved


def test_credentials_use_environment_and_are_not_in_settings_repr(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", API_KEY)
    settings = EmailSettings()
    assert settings.resend_api_key == API_KEY
    assert API_KEY not in repr(settings)


def test_billing_provider_failure_preserves_subscription_and_retries_same_request(
    factory, configured, monkeypatch,
):
    monkeypatch.setenv("ADMIN_DOMAIN", "https://billing.example.test")
    requests = []

    def provider(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("unknown acceptance")
        return httpx.Response(200, json={"id": PROVIDER_ID})

    _mock_provider(monkeypatch, provider)
    with factory() as db:
        subscription = _subscription("tenant-a", ends_at=NOW + timedelta(days=30))
        db.add(subscription)
        db.commit()
        db.refresh(subscription)  # SQLite round-trips timestamps without tzinfo.
        original = (subscription.status, subscription.ends_at, subscription.revision)
        first = run_billing_notifications_once(db, at=NOW)
        assert first.retried == 1
        assert db.scalar(select(BillingNotificationAttempt)).failure_code == "delivery_failed"
        stored = db.scalar(select(Subscription))
        assert (stored.status, stored.ends_at, stored.revision) == original
        second = run_billing_notifications_once(db, at=NOW + timedelta(minutes=5))
        assert second.sent == 1
        assert db.scalar(select(BillingNotificationIntent).where(
            BillingNotificationIntent.kind == "subscription_expiry_30d",
        )).status == "sent"
        third = run_billing_notifications_once(db, at=NOW + timedelta(minutes=10))
        assert third.sent == 0
        assert len(requests) == 2
        assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
        assert db.query(BillingNotificationAttempt).count() == 2