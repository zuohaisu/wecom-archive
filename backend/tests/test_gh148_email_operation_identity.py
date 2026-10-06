"""Offline provider deduplication regressions for GH-148 QA findings."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app import email
from app.db.models import AdminUser, AuditLog, BillingNotificationIntent
from app.routers.auth import _InviteBody, invite_user
from app.services.billing_notifications import run_billing_notifications_once
from tests.test_gh148_transactional_email import LINK, RECIPIENT, WHEN, _mock_provider
from tests.test_gh148_transactional_email import configured  # noqa: F401 - fixture discovery
from tests.test_rnd285_invite_flow import _session_with_inviter
from tests.test_rnd401_billing_notifications import NOW, _payment
from tests.test_rnd401_billing_notifications import factory  # noqa: F401 - fixture discovery


class DeduplicatingProvider:
    """Resend contract: a repeated key returns the original send ID."""

    def __init__(self):
        self.requests = []
        self.sends = {}
        self.timeout_first = False

    def __call__(self, request):
        self.requests.append(request)
        key = request.headers["Idempotency-Key"]
        if key in self.sends:
            payload, provider_id = self.sends[key]
            if payload != request.content:
                return httpx.Response(409)
        else:
            provider_id = str(uuid4())
            self.sends[key] = (request.content, provider_id)
        if self.timeout_first and len(self.requests) == 1:
            raise httpx.ReadTimeout("accepted but response lost")
        return httpx.Response(200, json={"id": provider_id})


def test_distinct_payment_intents_send_identical_payloads_separately(
    factory, configured, monkeypatch,
):
    monkeypatch.setenv("ADMIN_DOMAIN", "https://billing.example.test")
    provider = DeduplicatingProvider()
    _mock_provider(monkeypatch, provider)
    with factory() as db:
        db.add_all([
            _payment(tenant, status="paid_activation_pending", paid_at=NOW)
            for tenant in ("tenant-a", "tenant-b")
        ])
        db.commit()
        result = run_billing_notifications_once(db, at=NOW)
        assert result.sent == 2
        intents = db.scalars(select(BillingNotificationIntent)).all()
        assert len(intents) == 2
        assert len({intent.subject_id for intent in intents}) == 2
        assert all(intent.status == "sent" for intent in intents)
        assert len(provider.requests) == len(provider.sends) == 2
        assert provider.requests[0].content == provider.requests[1].content
        assert len({r.headers["Idempotency-Key"] for r in provider.requests}) == 2
        assert run_billing_notifications_once(db, at=NOW).sent == 0
        assert len(provider.requests) == 2


def test_explicit_invite_resends_same_token_but_is_a_new_provider_send(
    configured, monkeypatch,
):
    provider = DeduplicatingProvider()
    _mock_provider(monkeypatch, provider)
    db, inviter, tenant_id = _session_with_inviter()
    AuditLog.__table__.create(db.get_bind())
    try:
        body = _InviteBody(
            wecom_user_id="synthetic-invitee", email=RECIPIENT,
            name="Synthetic", role="compliance",
        )
        first = invite_user(body, (inviter, tenant_id), db)
        user = db.scalar(select(AdminUser).where(AdminUser.email == RECIPIENT))
        original_token = user.invite_token
        second = invite_user(body, (inviter, tenant_id), db)
        assert first.body == second.body == b'{"ok":true}'
        assert user.invite_token == original_token
        assert user.invite_status == "pending"
        assert db.query(AdminUser).filter(AdminUser.email == RECIPIENT).count() == 1
        assert len(provider.requests) == len(provider.sends) == 2
        assert provider.requests[0].content == provider.requests[1].content
        assert len({r.headers["Idempotency-Key"] for r in provider.requests}) == 2
    finally:
        db.close()


@pytest.mark.parametrize("producer", ["invite", "billing"])
def test_operation_retry_after_remote_acceptance_does_not_send_twice(
    configured, monkeypatch, producer,
):
    provider = DeduplicatingProvider()
    provider.timeout_first = True
    _mock_provider(monkeypatch, provider)

    def send(operation_id):
        if producer == "invite":
            return email.send_invite_email(RECIPIENT, LINK, operation_id=operation_id)
        return email.send_billing_notification_email(
            RECIPIENT, "payment_activation_pending", WHEN, LINK,
            operation_id=operation_id,
        )

    assert not send("synthetic-operation-1")
    assert send("synthetic-operation-1")
    assert len(provider.requests) == 2 and len(provider.sends) == 1
    assert send("synthetic-operation-2")
    assert len(provider.sends) == 2
    keys = [r.headers["Idempotency-Key"] for r in provider.requests]
    assert keys[0] == keys[1] != keys[2]
    assert all("synthetic-operation" not in key for key in keys)
