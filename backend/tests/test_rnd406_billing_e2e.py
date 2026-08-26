"""RND-406 T14: purchase / renew / refund / expire / freeze / recovery E2E.

Each test below drives one of the thirteen non-negotiable scenarios from the
Linear issue end to end through the real FastAPI app (real HTTP routes, real
service functions, real sqlite-backed persistence). The only faked boundary
is the WeChat Pay/refund provider network call (``CombinedFakeProvider``) —
exactly the "provider fixture" the ticket's own scope sanctions in place of a
live WeChat Pay sandbox merchant, which this non-prod evidence never touches.
No production code changes ship with this ticket; this file and its QA
report (``tasks/RND-406-e2e-evidence.md``) are the entire deliverable.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db.base import Base
from app.db.models import (
    AdminSession,
    AdminUser,
    ArchiveMessage,
    AuditLog,
    BillingPlan,
    Contact,
    ManualFinancialTransaction,
    MediaFile,
    PaymentEvent,
    PaymentOrder,
    PaymentRecoveryFinding,
    PlanEntitlement,
    PlatformAdmin,
    RefundEvent,
    RefundOrder,
    Subscription,
    SubscriptionActivation,
    SubscriptionHistory,
    SubscriptionTermGrant,
    Tenant,
    TenantStorageDaily,
)
from app.db.session import get_db
from app.main import create_app
from app.routers import billing
from app.routers import refunds as refunds_router
from app.services.billing_lifecycle import (
    reconcile_tenant_billing_lifecycle,
    restore_tenant_after_paid_subscription,
)
from app.services.billing_lifecycle_batch import run_lifecycle_batch_once
from app.services.entitlements import ANNUAL_PLAN_CODE, ARCHIVE_ACCESS, UNLIMITED_SEATS
from app.services.payment_provider import (
    CheckoutArtifact,
    RefundSubmissionResult,
    TrustedPaymentEvent,
    TrustedRefundEvent,
)
from app.services.service_access import (
    DENY_FROZEN,
    INTERACTIVE,
    OWNER_BILLING,
    WORKER_EXPORT,
    WORKER_MEDIA,
    WORKER_SYNC,
    tenant_service_allows,
    tenant_service_denial,
)

# Real wall-clock time, captured once at collection. Several call paths this
# suite exercises compute against actual current time regardless of any
# trusted_at the test supplies — AdminSession expiry (app.auth), and the
# Owner subscription-overview GET's effective-status read (unlike the
# webhook/reconcile paths, which take an explicit `at`/`trusted_at`). Anchoring
# NOW to real time keeps every day-scale offset below (2/3/7/10 days) safely
# clear of both boundaries at once instead of drifting apart over calendar time.
NOW = datetime.now(timezone.utc)
FAR_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _utc(value):
    """Normalize a value read back from sqlite, which drops tzinfo."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _plus_months(value, months):
    """Mirror app.services.subscription_activation._add_calendar_months
    exactly, so expected dates track real NOW instead of a stale literal."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


PLAN_ID = "rnd406-plan"
PLAN_AMOUNT_CENTS = 9900
PLATFORM_ADMIN_ID = "platform-admin-rnd406"
PLATFORM_ADMIN_EMAIL = "rnd406-ops@example.test"
PLATFORM_ADMIN_PASSWORD = "rnd406-test-password"
PLATFORM_AUTH = (PLATFORM_ADMIN_EMAIL, PLATFORM_ADMIN_PASSWORD)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


class CombinedFakeProvider:
    """Controllable double for the full ``PaymentProvider`` protocol.

    Mirrors the idiom already established by ``test_rnd380_billing_api.py``
    and ``test_rnd399_refund_domain.py``: the caller arms an attribute
    (``notification`` / ``refund_notification_event`` / ``refund_query_event``
    / ``payment_query_result``) before driving the corresponding HTTP call.
    This *is* the "provider fixture" RND-406's scope sanctions in place of a
    real WeChat Pay sandbox merchant.
    """

    code = "wechat_pay"
    app_id = "wx-rnd406"
    merchant_id = "1900004060"

    def __init__(self):
        self.notification = None
        self.refund_notification_event = None
        self.refund_query_event = None
        self.payment_query_result = None
        self.query_payment_calls = 0
        self.query_refund_calls = 0
        self.refund_requests = []

    def create_payment(self, request):
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="qr_code",
            value="weixin://wxpay/bizpayurl?pr=rnd406",
        )

    def verify_and_parse_notification(self, headers, raw_body):
        assert self.notification is not None
        return self.notification

    def query_payment(self, provider_order_ref):
        self.query_payment_calls += 1
        assert self.payment_query_result is not None
        return self.payment_query_result

    def close_payment(self, provider_order_ref):
        raise NotImplementedError

    def create_refund(self, request):
        self.refund_requests.append(request)
        return RefundSubmissionResult(
            provider=self.code,
            provider_ref=request.provider_ref,
            provider_refund_id=f"wechat-refund-{len(self.refund_requests)}",
            provider_order_ref=request.provider_order_ref,
            provider_transaction_id=request.provider_transaction_id,
            state="PROCESSING",
            amount_cents=request.amount_cents,
            total_amount_cents=request.total_amount_cents,
            currency=request.currency,
            accepted_at=NOW,
        )

    def query_refund(self, provider_ref):
        self.query_refund_calls += 1
        assert self.refund_query_event is not None
        return self.refund_query_event

    def verify_and_parse_refund_notification(self, headers, raw_body):
        assert self.refund_notification_event is not None
        return self.refund_notification_event


def _tables():
    return [
        Tenant.__table__,
        PlatformAdmin.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        SubscriptionHistory.__table__,
        SubscriptionActivation.__table__,
        PaymentOrder.__table__,
        PaymentRecoveryFinding.__table__,
        PaymentEvent.__table__,
        SubscriptionTermGrant.__table__,
        RefundOrder.__table__,
        RefundEvent.__table__,
        AdminUser.__table__,
        AdminSession.__table__,
        AuditLog.__table__,
        MediaFile.__table__,
        TenantStorageDaily.__table__,
        ArchiveMessage.__table__,
        Contact.__table__,
        ManualFinancialTransaction.__table__,
    ]


def _engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _new_factory():
    """Fresh sqlite-backed engine with every table this suite needs.

    ArchiveMessage's postgres-only trigram/FTS index is dropped for DDL
    (sqlite has no to_tsvector) and restored immediately after, matching
    the established shim in test_platform_operations.py.
    """
    engine = _engine()
    fts_index = next(
        index
        for index in ArchiveMessage.__table__.indexes
        if index.name == "ix_archive_messages_content_text_fts"
    )
    ArchiveMessage.__table__.indexes.remove(fts_index)
    try:
        Base.metadata.create_all(engine, tables=_tables())
    finally:
        ArchiveMessage.__table__.indexes.add(fts_index)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_plan(db):
    db.add(
        BillingPlan(
            id=PLAN_ID,
            code=ANNUAL_PLAN_CODE,
            display_name="年度基础套餐",
            is_active=True,
            amount_cents=PLAN_AMOUNT_CENTS,
            currency="CNY",
            billing_period_months=12,
            storage_quota_bytes=5 * 1024**3,
        )
    )
    db.add(
        PlanEntitlement(
            id=f"{PLAN_ID}-unlimited",
            plan_id=PLAN_ID,
            capability=UNLIMITED_SEATS,
            is_enabled=True,
        )
    )
    db.add(
        PlanEntitlement(
            id=f"{PLAN_ID}-archive",
            plan_id=PLAN_ID,
            capability=ARCHIVE_ACCESS,
            is_enabled=True,
        )
    )


def _seed_platform_admin(db):
    db.add(
        PlatformAdmin(
            id=PLATFORM_ADMIN_ID,
            email=PLATFORM_ADMIN_EMAIL,
            password_hash=hash_password(PLATFORM_ADMIN_PASSWORD),
            status="active",
        )
    )


def _seed_tenant(db, tenant_id, *, lifecycle_status="provisioning", is_active=False):
    db.add(
        Tenant(
            id=tenant_id,
            name=tenant_id,
            slug=tenant_id,
            lifecycle_status=lifecycle_status,
            is_active=is_active,
        )
    )


def _seed_owner(db, tenant_id, *, owner_id, session_id, session_scope="admin"):
    db.add(
        AdminUser(
            id=owner_id,
            tenant_id=tenant_id,
            wecom_user_id=owner_id,
            name="Owner",
            role="owner",
            status="active",
        )
    )
    db.add(
        AdminSession(
            id=session_id,
            admin_user_id=owner_id,
            tenant_id=tenant_id,
            wecom_user_id=owner_id,
            session_scope=session_scope,
            # Session expiry is checked against real wall-clock time by
            # get_billing_context, not the synthetic NOW this file uses for
            # subscription/lifecycle math — so this must be a real future date.
            expires_at=FAR_FUTURE,
            is_revoked=False,
        )
    )


def _seed_subscription(
    db,
    tenant_id,
    *,
    status,
    starts_at,
    ends_at,
    grace_ends_at=None,
    renewal_count=0,
    cancel_at_period_end=False,
):
    db.add(
        Subscription(
            id=f"sub-{tenant_id}",
            tenant_id=tenant_id,
            plan_id=PLAN_ID,
            status=status,
            starts_at=starts_at,
            ends_at=ends_at,
            grace_ends_at=grace_ends_at or (ends_at + timedelta(days=7)),
            cancel_at_period_end=cancel_at_period_end,
            source="test_seed",
            renewal_count=renewal_count,
            revision=1,
        )
    )


def _app(monkeypatch, factory, provider):
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[billing.get_payment_provider] = lambda: provider
    app.dependency_overrides[billing.get_wechat_payment_provider] = lambda: provider
    app.dependency_overrides[refunds_router.get_refund_provider] = lambda: provider
    monkeypatch.setattr(billing, "get_wechat_payment_provider", lambda: provider)
    monkeypatch.setattr(billing, "wechat_pay_is_enabled", lambda: True)
    return TestClient(app)


def _client_with_session(client, session_id):
    scoped = TestClient(client.app)
    scoped.cookies.set("session_id", session_id)
    return scoped


def _subscription(factory, tenant_id) -> Subscription:
    with factory() as db:
        row = db.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id))
        db.expunge_all()
    if row is not None:
        # sqlite drops tzinfo on round-trip; normalize so callers can compare
        # directly against the tz-aware literals used to seed/assert state.
        row.starts_at = _utc(row.starts_at)
        row.ends_at = _utc(row.ends_at)
        row.grace_ends_at = _utc(row.grace_ends_at)
    return row


def _tenant(factory, tenant_id) -> Tenant:
    with factory() as db:
        row = db.get(Tenant, tenant_id)
        db.expunge_all()
        return row


def _create_order(client, idempotency_key, *, plan_code=ANNUAL_PLAN_CODE):
    return client.post(
        "/api/billing/orders",
        headers={"Idempotency-Key": idempotency_key},
        json={"plan_code": plan_code},
    )


def _notify_payment_success(
    client,
    provider,
    factory,
    *,
    order_id,
    event_id,
    succeeded_at,
    amount_cents=PLAN_AMOUNT_CENTS,
):
    with factory() as db:
        order = db.get(PaymentOrder, order_id)
        provider_order_ref = order.provider_order_ref
    provider.notification = TrustedPaymentEvent(
        provider=provider.code,
        provider_event_id=event_id,
        provider_order_ref=provider_order_ref,
        provider_transaction_id=f"txn-{event_id}",
        event_type="payment_succeeded",
        app_id=provider.app_id,
        merchant_id=provider.merchant_id,
        state="SUCCESS",
        amount_cents=amount_cents,
        currency="CNY",
        succeeded_at=succeeded_at,
        payload_hash="a" * 64,
        source="callback",
    )
    response = client.post(
        "/api/payments/wechat/notify",
        content=b'{"encrypted":"opaque"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 204, response.text
    return response


def _pay(
    owner_client,
    provider,
    factory,
    *,
    idempotency_key,
    event_id,
    succeeded_at,
    amount_cents=PLAN_AMOUNT_CENTS,
):
    created = _create_order(owner_client, idempotency_key)
    assert created.status_code == 201, created.text
    order_id = created.json()["order_id"]
    _notify_payment_success(
        owner_client,
        provider,
        factory,
        order_id=order_id,
        event_id=event_id,
        succeeded_at=succeeded_at,
        amount_cents=amount_cents,
    )
    return order_id


def _submit_refund(owner_client_unused, base_client, *, tenant_id, payment_order_id, idempotency_key):
    return base_client.post(
        f"/api/platform/operations/tenants/{tenant_id}/refunds",
        auth=PLATFORM_AUTH,
        headers={"Idempotency-Key": idempotency_key},
        json={
            "payment_order_id": payment_order_id,
            "reason_code": "customer_request",
            "confirmation": tenant_id,
        },
    )


def _refund_event(factory, provider, *, refund_id, state, event_id, occurred_at, source="callback"):
    with factory() as db:
        refund = db.get(RefundOrder, refund_id)
        payment = db.get(PaymentOrder, refund.payment_order_id)
        return TrustedRefundEvent(
            provider=provider.code,
            provider_event_id=event_id,
            provider_ref=refund.provider_ref,
            provider_refund_id=refund.provider_refund_id,
            provider_order_ref=payment.provider_order_ref,
            provider_transaction_id=payment.provider_transaction_id,
            merchant_id=provider.merchant_id,
            state=state,
            source=source,
            amount_cents=refund.amount_cents,
            total_amount_cents=payment.amount_cents,
            currency=refund.currency,
            payload_hash="b" * 64,
            occurred_at=occurred_at,
        )


def _notify_refund(base_client, provider, event) -> None:
    provider.refund_notification_event = event
    response = base_client.post(
        "/api/refunds/wechat/notify",
        content=b'{"encrypted":"opaque"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 204, response.text


# ---------------------------------------------------------------------------
# Scenario 1 — 新客户扫码支付成功后开通套餐并恢复服务
# ---------------------------------------------------------------------------


def test_scenario1_new_customer_purchase_activates_and_restores_service(monkeypatch) -> None:
    # The WeCom-authorization → provisioning → self-test → explicit /activate
    # chain that promotes a brand-new signup from "provisioning" is RND-389's
    # own dedicated E2E surface, not this ticket's. RND-406 proves the
    # billing half of "new customer pays": before payment, an onboarded
    # tenant with no Subscription yet has no usable paid service; a
    # successful payment opens the plan (开通套餐) and that service becomes
    # usable (恢复服务) — the exact transition test_rnd380_billing_api.py's
    # capacity test also demonstrates, driven here through the full
    # purchase → webhook HTTP path instead of a direct service call.
    factory = _new_factory()
    with factory() as db:
        _seed_plan(db)
        _seed_tenant(db, "tenant-new", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-new", owner_id="owner-new", session_id="session-new")
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-new")

    before_capacity = owner.get("/api/billing/capacity").json()
    before_overview = owner.get("/api/billing/subscription").json()
    assert before_capacity["state"] == "unavailable"
    assert before_overview["is_entitled"] is False

    order_id = _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario1-purchase",
        event_id="rnd406-s1-event",
        succeeded_at=NOW,
    )

    with factory() as db:
        order = db.get(PaymentOrder, order_id)
        assert order.status == "succeeded"
        activation = db.get(SubscriptionActivation, order.activation_id)
        assert activation.activation_kind == "activation"
        assert _utc(activation.applied_starts_at) == NOW
        assert _utc(activation.applied_ends_at) == _plus_months(NOW, 12)

    subscription = _subscription(factory, "tenant-new")
    tenant = _tenant(factory, "tenant-new")
    assert subscription.status == "active"
    assert tenant.lifecycle_status == "active"
    assert tenant.is_active is True

    after_capacity = owner.get("/api/billing/capacity").json()
    after_overview = owner.get("/api/billing/subscription").json()
    assert after_capacity["state"] == "normal"
    assert after_capacity["can_accept_new_media"] is True
    assert after_overview["is_entitled"] is True
    assert after_overview["display_state"] == "paid_active"
    assert after_overview["tenant_lifecycle_status"] == "active"


# ---------------------------------------------------------------------------
# Scenario 2 — 有效订阅续费从当前 ends_at 顺延
# ---------------------------------------------------------------------------


def test_scenario2_renewal_while_active_extends_current_ends_at(monkeypatch) -> None:
    factory = _new_factory()
    starts_at = datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc)
    ends_at = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    with factory() as db:
        _seed_plan(db)
        _seed_tenant(db, "tenant-active", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-active", owner_id="owner-active", session_id="session-active")
        _seed_subscription(
            db,
            "tenant-active",
            status="active",
            starts_at=starts_at,
            ends_at=ends_at,
            renewal_count=0,
        )
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-active")

    renewal_time = datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc)
    _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario2-renewal",
        event_id="rnd406-s2-event",
        succeeded_at=renewal_time,
    )

    subscription = _subscription(factory, "tenant-active")
    assert subscription.starts_at == starts_at  # unchanged
    assert subscription.ends_at == datetime(2027, 6, 1, 8, 0, tzinfo=timezone.utc)  # +12mo from OLD ends_at
    assert subscription.renewal_count == 1


# ---------------------------------------------------------------------------
# Scenario 3 — 已到期订阅续费从新支付成功时间重新起算
# ---------------------------------------------------------------------------


def test_scenario3_renewal_after_expired_restarts_from_payment_time(monkeypatch) -> None:
    factory = _new_factory()
    with factory() as db:
        _seed_plan(db)
        # A tenant that already lapsed into frozen (RND-402) can still pay to
        # unfreeze; its stored subscription status is authoritatively "expired".
        _seed_tenant(db, "tenant-expired", lifecycle_status="frozen", is_active=False)
        _seed_owner(db, "tenant-expired", owner_id="owner-expired", session_id="session-expired")
        _seed_subscription(
            db,
            "tenant-expired",
            status="expired",
            starts_at=datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc),
            ends_at=datetime(2025, 1, 1, 8, 0, tzinfo=timezone.utc),
            grace_ends_at=datetime(2025, 1, 8, 8, 0, tzinfo=timezone.utc),
            renewal_count=3,
        )
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-expired")

    _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario3-renewal",
        event_id="rnd406-s3-event",
        succeeded_at=NOW,
    )

    subscription = _subscription(factory, "tenant-expired")
    assert subscription.status == "active"
    assert subscription.starts_at == NOW  # restarted from the payment time
    assert subscription.ends_at == _plus_months(NOW, 12)
    assert subscription.renewal_count == 0  # a fresh activation, not a renewal

    with factory() as db:
        activation = db.scalar(
            select(SubscriptionActivation).where(
                SubscriptionActivation.tenant_id == "tenant-expired"
            )
        )
        assert activation.activation_kind == "activation"


# ---------------------------------------------------------------------------
# Scenario 4 — 退款申请被受理但未 SUCCESS 时不得回退订阅
# ---------------------------------------------------------------------------


def test_scenario4_refund_accepted_not_success_does_not_roll_back(monkeypatch) -> None:
    factory = _new_factory()
    with factory() as db:
        _seed_plan(db)
        _seed_platform_admin(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="provisioning", is_active=False)
        _seed_owner(
            db,
            "tenant-alpha",
            owner_id="owner-alpha",
            session_id="session-alpha",
            session_scope="provisioning",
        )
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")

    order_id = _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario4-purchase",
        event_id="rnd406-s4-event",
        succeeded_at=NOW,
    )
    before = _subscription(factory, "tenant-alpha")

    submitted = _submit_refund(
        owner,
        base,
        tenant_id="tenant-alpha",
        payment_order_id=order_id,
        idempotency_key="rnd406-scenario4-refund",
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["status"] == "processing"

    after = _subscription(factory, "tenant-alpha")
    assert after.status == before.status
    assert after.starts_at == before.starts_at
    assert after.ends_at == before.ends_at
    assert after.revision == before.revision


# ---------------------------------------------------------------------------
# Scenario 5 — Provider 确认退款 SUCCESS 后只撤销对应支付授予的期限
# ---------------------------------------------------------------------------


def test_scenario5_refund_success_revokes_only_that_payments_term(monkeypatch) -> None:
    factory = _new_factory()
    with factory() as db:
        _seed_plan(db)
        _seed_platform_admin(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="provisioning", is_active=False)
        _seed_owner(
            db,
            "tenant-alpha",
            owner_id="owner-alpha",
            session_id="session-alpha",
            session_scope="provisioning",
        )
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")

    order_id = _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario5-purchase",
        event_id="rnd406-s5-event",
        succeeded_at=NOW,
    )
    submitted = _submit_refund(
        owner,
        base,
        tenant_id="tenant-alpha",
        payment_order_id=order_id,
        idempotency_key="rnd406-scenario5-refund",
    )
    assert submitted.status_code == 202, submitted.text
    refund_id = submitted.json()["refund_id"]

    event = _refund_event(
        factory,
        provider,
        refund_id=refund_id,
        state="SUCCESS",
        event_id="rnd406-s5-refund-event",
        occurred_at=NOW + timedelta(hours=1),
    )
    _notify_refund(base, provider, event)

    with factory() as db:
        refund = db.get(RefundOrder, refund_id)
        assert refund.status == "succeeded"
        assert refund.entitlement_reversed_at is not None
        grant = db.scalar(
            select(SubscriptionTermGrant).where(SubscriptionTermGrant.payment_order_id == order_id)
        )
        assert grant.status == "reversed"

    subscription = _subscription(factory, "tenant-alpha")
    # No subscription pre-dated this purchase, so a clean reversal cancels the
    # entitlement projection rather than deleting evidence rows.
    assert subscription.status == "canceled"


# ---------------------------------------------------------------------------
# Scenario 6 — 已存在后续续费且期限无法安全回退时进入人工恢复队列，不自动猜测
# ---------------------------------------------------------------------------


def test_scenario6_refund_success_after_later_renewal_goes_to_manual_recovery(monkeypatch) -> None:
    factory = _new_factory()
    with factory() as db:
        _seed_plan(db)
        _seed_platform_admin(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-alpha", owner_id="owner-alpha", session_id="session-alpha")
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")

    first_order_id = _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario6-purchase",
        event_id="rnd406-s6-event-1",
        succeeded_at=NOW,
    )
    _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario6-renewal",
        event_id="rnd406-s6-event-2",
        succeeded_at=NOW + timedelta(days=45),  # still well within the first term
    )
    moved_subscription = _subscription(factory, "tenant-alpha")
    assert moved_subscription.renewal_count == 1
    assert moved_subscription.ends_at == _plus_months(_plus_months(NOW, 12), 12)

    # Refund the FIRST payment; its term grant snapshot no longer matches the
    # subscription (the second payment moved ends_at/renewal_count further).
    submitted = _submit_refund(
        owner,
        base,
        tenant_id="tenant-alpha",
        payment_order_id=first_order_id,
        idempotency_key="rnd406-scenario6-refund",
    )
    assert submitted.status_code == 202, submitted.text
    refund_id = submitted.json()["refund_id"]

    event = _refund_event(
        factory,
        provider,
        refund_id=refund_id,
        state="SUCCESS",
        event_id="rnd406-s6-refund-event",
        occurred_at=NOW + timedelta(days=46),
    )
    _notify_refund(base, provider, event)

    with factory() as db:
        refund = db.get(RefundOrder, refund_id)
        assert refund.status == "manual_recovery_required"
        assert refund.failure_code == "later_or_ambiguous_subscription_change"

    # No guessed rollback: the subscription is exactly as the renewal left it.
    untouched = _subscription(factory, "tenant-alpha")
    assert untouched.ends_at == moved_subscription.ends_at
    assert untouched.renewal_count == moved_subscription.renewal_count
    assert untouched.revision == moved_subscription.revision


# ---------------------------------------------------------------------------
# Scenario 7 — 到期后进入 7 天宽限期；宽限期内继续归档接收并提示续费
# ---------------------------------------------------------------------------


def test_scenario7_expiry_enters_seven_day_grace_and_keeps_archiving(monkeypatch) -> None:
    factory = _new_factory()
    ends_at = NOW - timedelta(days=2)
    with factory() as db:
        _seed_plan(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-alpha", owner_id="owner-alpha", session_id="session-alpha")
        _seed_subscription(
            db,
            "tenant-alpha",
            status="active",
            starts_at=ends_at - timedelta(days=363),
            ends_at=ends_at,
            grace_ends_at=ends_at + timedelta(days=7),
        )
        db.commit()

    with factory() as db:
        result = reconcile_tenant_billing_lifecycle(db, "tenant-alpha", at=NOW)
        db.commit()
    assert result.subscription_status == "grace"
    assert result.tenant_lifecycle_status == "active"  # grace folded into active

    subscription = _subscription(factory, "tenant-alpha")
    tenant = _tenant(factory, "tenant-alpha")
    assert subscription.status == "grace"
    assert tenant.lifecycle_status == "active"

    # Archiving (worker_sync) and interactive access continue during grace —
    # the projection is "active" so the single authoritative policy allows it.
    assert tenant_service_allows(tenant.lifecycle_status, WORKER_SYNC) is True
    assert tenant_service_allows(tenant.lifecycle_status, INTERACTIVE) is True

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")
    overview = owner.get("/api/billing/subscription").json()
    assert overview["effective_status"] == "grace"
    assert overview["is_entitled"] is True  # renewal-due prompt, not a lockout


# ---------------------------------------------------------------------------
# Scenario 8 — 宽限期结束进入 frozen；普通用户、同步、媒体和导出受限；Owner 仍可续费
# ---------------------------------------------------------------------------


def test_scenario8_grace_end_freezes_and_restricts_service(monkeypatch) -> None:
    factory = _new_factory()
    ends_at = NOW - timedelta(days=10)
    with factory() as db:
        _seed_plan(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-alpha", owner_id="owner-alpha", session_id="session-alpha")
        _seed_subscription(
            db,
            "tenant-alpha",
            status="active",
            starts_at=ends_at - timedelta(days=363),
            ends_at=ends_at,
            grace_ends_at=ends_at + timedelta(days=7),  # already in the past too
        )
        db.commit()

    with factory() as db:
        result = reconcile_tenant_billing_lifecycle(db, "tenant-alpha", at=NOW)
        db.commit()
    assert result.tenant_lifecycle_status == "frozen"

    tenant = _tenant(factory, "tenant-alpha")
    assert tenant.lifecycle_status == "frozen"
    for capability in (INTERACTIVE, WORKER_SYNC, WORKER_MEDIA, WORKER_EXPORT):
        assert tenant_service_denial("frozen", capability) == DENY_FROZEN
    assert tenant_service_denial("frozen", OWNER_BILLING) is None

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")
    page = owner.get("/admin/billing")
    assert page.status_code == 200
    renewal_order = _create_order(owner, "rnd406-scenario8-renewal-order")
    assert renewal_order.status_code == 201


# ---------------------------------------------------------------------------
# Scenario 9 — frozen Tenant 续费后恢复；superadmin 手工 suspended 不得被支付自动解除
# ---------------------------------------------------------------------------


def test_scenario9a_frozen_tenant_renews_and_restores(monkeypatch) -> None:
    factory = _new_factory()
    ends_at = NOW - timedelta(days=10)
    with factory() as db:
        _seed_plan(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="frozen", is_active=False)
        _seed_owner(db, "tenant-alpha", owner_id="owner-alpha", session_id="session-alpha")
        _seed_subscription(
            db,
            "tenant-alpha",
            status="expired",
            starts_at=ends_at - timedelta(days=363),
            ends_at=ends_at,
            grace_ends_at=ends_at + timedelta(days=7),
        )
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")

    _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario9a-renewal",
        event_id="rnd406-s9a-event",
        succeeded_at=NOW,
    )

    tenant = _tenant(factory, "tenant-alpha")
    assert tenant.lifecycle_status == "active"
    assert tenant.is_active is True


def test_scenario9b_manual_suspension_is_never_cleared_by_payment(monkeypatch) -> None:
    factory = _new_factory()
    with factory() as db:
        _seed_plan(db)
        _seed_platform_admin(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-alpha", owner_id="owner-alpha", session_id="session-alpha")
        _seed_subscription(
            db,
            "tenant-alpha",
            status="active",
            starts_at=NOW - timedelta(days=30),
            ends_at=NOW + timedelta(days=335),
        )
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")

    suspend = base.patch(
        "/api/platform/operations/tenants/tenant-alpha/service",
        auth=PLATFORM_AUTH,
        headers={"Idempotency-Key": "rnd406-scenario9b-suspend"},
        json={
            "lifecycle_status": "suspended",
            "reason_code": "manual_review",
            "confirmation": "tenant-alpha",
        },
    )
    assert suspend.status_code == 200, suspend.text
    assert suspend.json()["lifecycle_status"] == "suspended"

    # The suspended tenant's Owner cannot even create a payable order —
    # the write surface is closed before any payment could race the freeze.
    blocked_order = _create_order(owner, "rnd406-scenario9b-blocked-order")
    assert blocked_order.status_code == 403

    # Belt-and-suspenders: even a payment already in flight when suspension
    # lands must not clear it — the restore helper is a hard no-op here.
    with factory() as db:
        tenant = db.get(Tenant, "tenant-alpha")
        subscription = db.scalar(
            select(Subscription).where(Subscription.tenant_id == "tenant-alpha")
        )
        changed = restore_tenant_after_paid_subscription(
            db, tenant, subscription, at=NOW + timedelta(hours=1)
        )
        db.commit()
        assert changed is False

    tenant = _tenant(factory, "tenant-alpha")
    assert tenant.lifecycle_status == "suspended"


# ---------------------------------------------------------------------------
# Scenario 10 — Owner“到期不续费”不提前终止、不自动退款
# ---------------------------------------------------------------------------


def test_scenario10_owner_cancel_at_period_end_does_not_end_early_or_refund(monkeypatch) -> None:
    factory = _new_factory()
    ends_at = NOW + timedelta(days=200)
    with factory() as db:
        _seed_plan(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-alpha", owner_id="owner-alpha", session_id="session-alpha")
        _seed_subscription(
            db,
            "tenant-alpha",
            status="active",
            starts_at=NOW - timedelta(days=165),
            ends_at=ends_at,
        )
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")

    set_true = owner.post("/api/billing/subscription/cancel-intent", json={"enabled": True})
    assert set_true.status_code == 200
    body = set_true.json()
    assert body["cancel_at_period_end"] is True
    assert datetime.fromisoformat(body["ends_at"]) == ends_at

    subscription = _subscription(factory, "tenant-alpha")
    tenant = _tenant(factory, "tenant-alpha")
    assert subscription.ends_at == ends_at
    assert subscription.status == "active"
    assert tenant.lifecycle_status == "active"
    with factory() as db:
        assert db.query(RefundOrder).count() == 0


# ---------------------------------------------------------------------------
# Scenario 11 — 支付查询、日终对账、退款查询/通知及异常恢复均可幂等重放
# ---------------------------------------------------------------------------


def test_scenario11_idempotent_replay_across_query_notify_and_batch(monkeypatch) -> None:
    factory = _new_factory()
    with factory() as db:
        _seed_plan(db)
        _seed_platform_admin(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-alpha", owner_id="owner-alpha", session_id="session-alpha")
        # A second, already-due tenant exclusively for the lifecycle-batch
        # idempotent-replay check below.
        _seed_tenant(db, "tenant-batch", lifecycle_status="active", is_active=True)
        _seed_subscription(
            db,
            "tenant-batch",
            status="active",
            starts_at=NOW - timedelta(days=370),
            ends_at=NOW - timedelta(days=10),
            grace_ends_at=NOW - timedelta(days=3),
        )
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")

    # (a) payment webhook replay: same provider_event_id twice must not
    # double-activate or double-write PaymentEvent.
    order_id = _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario11-purchase",
        event_id="rnd406-s11-payment-event",
        succeeded_at=NOW,
    )
    once = _subscription(factory, "tenant-alpha")
    _notify_payment_success(
        owner,
        provider,
        factory,
        order_id=order_id,
        event_id="rnd406-s11-payment-event",  # identical event id: a true replay
        succeeded_at=NOW,
    )
    twice = _subscription(factory, "tenant-alpha")
    assert once.ends_at == twice.ends_at
    assert once.revision == twice.revision
    with factory() as db:
        assert db.query(PaymentEvent).filter(PaymentEvent.order_id == order_id).count() == 1

    # (b) order refresh after success short-circuits without ever calling the
    # provider's query endpoint.
    first_refresh = owner.post(f"/api/billing/orders/{order_id}/refresh")
    second_refresh = owner.post(f"/api/billing/orders/{order_id}/refresh")
    assert first_refresh.status_code == second_refresh.status_code == 200
    assert first_refresh.json()["status"] == second_refresh.json()["status"] == "succeeded"
    assert provider.query_payment_calls == 0

    # (c) refund query replay: identical Idempotency-Key short-circuits the
    # second call before it ever reaches the provider.
    submitted = _submit_refund(
        owner,
        base,
        tenant_id="tenant-alpha",
        payment_order_id=order_id,
        idempotency_key="rnd406-scenario11-refund",
    )
    refund_id = submitted.json()["refund_id"]
    provider.refund_query_event = _refund_event(
        factory,
        provider,
        refund_id=refund_id,
        state="SUCCESS",
        event_id="rnd406-s11-refund-query-event",
        occurred_at=NOW + timedelta(hours=1),
        source="query",
    )
    query_key = "rnd406-scenario11-refund-query"
    first_query = base.post(
        f"/api/platform/operations/tenants/tenant-alpha/refunds/{refund_id}/query",
        auth=PLATFORM_AUTH,
        headers={"Idempotency-Key": query_key},
        json={"reason_code": "reconciliation_recovery", "confirmation": "tenant-alpha"},
    )
    second_query = base.post(
        f"/api/platform/operations/tenants/tenant-alpha/refunds/{refund_id}/query",
        auth=PLATFORM_AUTH,
        headers={"Idempotency-Key": query_key},
        json={"reason_code": "reconciliation_recovery", "confirmation": "tenant-alpha"},
    )
    assert first_query.status_code == second_query.status_code == 200
    assert first_query.json()["status"] == second_query.json()["status"] == "succeeded"
    assert provider.query_refund_calls == 1  # the replay never re-queried the provider

    # (d) lifecycle batch replay: running it twice at the same instant is a
    # true no-op the second time, and only the due tenant transitions.
    with factory() as db:
        first_batch = run_lifecycle_batch_once(db, now=NOW)
    assert first_batch.changed >= 1
    assert "active->frozen" in first_batch.tenant_transitions
    with factory() as db:
        second_batch = run_lifecycle_batch_once(db, now=NOW)
    assert second_batch.changed == 0
    assert second_batch.unchanged == second_batch.scanned


# ---------------------------------------------------------------------------
# Scenario 12 — Owner 客户端与 superadmin 均显示一致的套餐/订阅/支付/退款/异常状态
# ---------------------------------------------------------------------------


def test_scenario12_owner_and_superadmin_views_agree(monkeypatch) -> None:
    factory = _new_factory()
    ends_at = NOW - timedelta(days=2)
    with factory() as db:
        _seed_plan(db)
        _seed_platform_admin(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-alpha", owner_id="owner-alpha", session_id="session-alpha")
        _seed_subscription(
            db,
            "tenant-alpha",
            status="active",
            starts_at=ends_at - timedelta(days=363),
            ends_at=ends_at,
            grace_ends_at=ends_at + timedelta(days=7),
            cancel_at_period_end=True,
        )
        db.commit()
    with factory() as db:
        reconcile_tenant_billing_lifecycle(db, "tenant-alpha", at=NOW)
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner = _client_with_session(base, "session-alpha")

    owner_view = owner.get("/api/billing/subscription").json()
    detail = base.get(
        "/api/platform/operations/tenants/tenant-alpha", auth=PLATFORM_AUTH
    ).json()

    assert owner_view["tenant_lifecycle_status"] == detail["lifecycle_status"] == "active"
    assert owner_view["effective_status"] == detail["subscription_status"] == "grace"
    assert datetime.fromisoformat(owner_view["ends_at"]) == datetime.fromisoformat(
        detail["subscription_ends_at"]
    )
    assert owner_view["cancel_at_period_end"] == detail["cancel_at_period_end"] is True

    # Refund status agreement.
    order_id = _pay(
        owner,
        provider,
        factory,
        idempotency_key="rnd406-scenario12-purchase",
        event_id="rnd406-s12-event",
        succeeded_at=NOW,
    )
    submitted = _submit_refund(
        owner,
        base,
        tenant_id="tenant-alpha",
        payment_order_id=order_id,
        idempotency_key="rnd406-scenario12-refund",
    )
    refund_id = submitted.json()["refund_id"]
    owner_refund = owner.get("/api/billing/refunds/latest").json()
    detail_after = base.get(
        "/api/platform/operations/tenants/tenant-alpha", auth=PLATFORM_AUTH
    ).json()
    superadmin_refund = next(
        row for row in detail_after["refund_orders"] if row["refund_id"] == refund_id
    )
    assert owner_refund["status"] == superadmin_refund["status"] == "processing"


# ---------------------------------------------------------------------------
# Scenario 13 — 至少两个 Tenant 并行验证数据和授权隔离
# ---------------------------------------------------------------------------


def test_scenario13_two_tenants_do_not_cross_contaminate(monkeypatch) -> None:
    factory = _new_factory()
    frozen_ends_at = NOW - timedelta(days=10)
    active_ends_at = NOW + timedelta(days=200)
    with factory() as db:
        _seed_plan(db)
        _seed_platform_admin(db)
        _seed_tenant(db, "tenant-alpha", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-alpha", owner_id="owner-alpha", session_id="session-alpha")
        _seed_subscription(
            db,
            "tenant-alpha",
            status="active",
            starts_at=frozen_ends_at - timedelta(days=363),
            ends_at=frozen_ends_at,
            grace_ends_at=frozen_ends_at + timedelta(days=7),
        )
        _seed_tenant(db, "tenant-beta", lifecycle_status="active", is_active=True)
        _seed_owner(db, "tenant-beta", owner_id="owner-beta", session_id="session-beta")
        _seed_subscription(
            db,
            "tenant-beta",
            status="active",
            starts_at=NOW - timedelta(days=165),
            ends_at=active_ends_at,
        )
        db.commit()

    provider = CombinedFakeProvider()
    base = _app(monkeypatch, factory, provider)
    owner_alpha = _client_with_session(base, "session-alpha")
    owner_beta = _client_with_session(base, "session-beta")

    # alpha lapses into frozen; beta cleanly renews. Interleaved deliberately.
    with factory() as db:
        batch = run_lifecycle_batch_once(db, now=NOW)
    assert batch.tenant_transitions.get("active->frozen") == 1

    _pay(
        owner_beta,
        provider,
        factory,
        idempotency_key="rnd406-scenario13-beta-renewal",
        event_id="rnd406-s13-beta-event",
        succeeded_at=NOW,
    )

    alpha_tenant = _tenant(factory, "tenant-alpha")
    beta_tenant = _tenant(factory, "tenant-beta")
    alpha_sub = _subscription(factory, "tenant-alpha")
    beta_sub = _subscription(factory, "tenant-beta")

    assert alpha_tenant.lifecycle_status == "frozen"
    assert beta_tenant.lifecycle_status == "active"
    assert beta_sub.renewal_count == 1
    assert beta_sub.ends_at > active_ends_at  # renewal extended it further out
    # alpha's expiry timeline is untouched by beta's renewal.
    assert alpha_sub.ends_at == frozen_ends_at

    # Owner-alpha billing surface still reflects frozen; only OWNER_BILLING
    # is reachable, never beta's data.
    alpha_overview = owner_alpha.get("/api/billing/subscription").json()
    beta_overview = owner_beta.get("/api/billing/subscription").json()
    assert alpha_overview["tenant_lifecycle_status"] == "frozen"
    assert beta_overview["tenant_lifecycle_status"] == "active"
    assert alpha_overview["ends_at"] != beta_overview["ends_at"]

    # Superadmin filtered lists are correctly scoped, never cross-leaking.
    frozen_list = base.get(
        "/api/platform/operations/tenants",
        auth=PLATFORM_AUTH,
        params={"lifecycle_status": "frozen"},
    ).json()
    active_list = base.get(
        "/api/platform/operations/tenants",
        auth=PLATFORM_AUTH,
        params={"lifecycle_status": "active"},
    ).json()
    frozen_ids = {row["tenant_id"] for row in frozen_list["items"]}
    active_ids = {row["tenant_id"] for row in active_list["items"]}
    assert frozen_ids == {"tenant-alpha"}
    assert "tenant-beta" in active_ids
    assert "tenant-alpha" not in active_ids
