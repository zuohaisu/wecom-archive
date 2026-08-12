from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit import AuditAction
from app.db.base import Base
from app.db.models import (
    AdminUser,
    AuditLog,
    BillingPlan,
    PaymentEvent,
    PaymentOrder,
    PlanEntitlement,
    Subscription,
    SubscriptionActivation,
    SubscriptionHistory,
    Tenant,
)
from app.services.entitlements import ANNUAL_PLAN_CODE
from app.services.payment_orders import (
    CreateOrderCommand,
    PaymentActivationPendingError,
    PaymentOrderConflictError,
    PaymentOrderNotFoundError,
    PaymentReplayConflictError,
    apply_trusted_payment,
    close_order,
    create_payment_order,
    get_checkout_url,
    get_order,
    query_and_reconcile_order,
)
from app.services.payment_provider import (
    CheckoutArtifact,
    PaymentQueryResult,
    TrustedPaymentEvent,
)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PLAN_ID = "rnd380-plan"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


class FakeProvider:
    code = "wechat_pay"
    app_id = "wx-rnd380"
    merchant_id = "1900003801"

    def __init__(self):
        self.created = []
        self.closed = []
        self.query_result = None
        self.create_error = None

    def create_payment(self, request):
        self.created.append(request)
        if self.create_error:
            raise self.create_error
        return CheckoutArtifact(
            provider_order_ref=request.provider_order_ref,
            kind="qr_code",
            value="weixin://wxpay/bizpayurl?pr=rnd380-order",
        )

    def verify_and_parse_notification(self, headers, raw_body):
        raise NotImplementedError

    def query_payment(self, provider_order_ref):
        assert self.query_result is not None
        return replace(self.query_result, provider_order_ref=provider_order_ref)

    def close_payment(self, provider_order_ref):
        self.closed.append(provider_order_ref)


def _tables():
    return [
        Tenant.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        SubscriptionHistory.__table__,
        SubscriptionActivation.__table__,
        PaymentOrder.__table__,
        PaymentEvent.__table__,
        AdminUser.__table__,
        AuditLog.__table__,
    ]


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_tables())
    result = sessionmaker(bind=engine, expire_on_commit=False)
    with result() as db:
        db.add_all(
            [
                Tenant(
                    id="tenant-a",
                    name="A",
                    slug="tenant-a",
                    lifecycle_status="active",
                ),
                Tenant(
                    id="tenant-b",
                    name="B",
                    slug="tenant-b",
                    lifecycle_status="active",
                ),
                BillingPlan(
                    id=PLAN_ID,
                    code=ANNUAL_PLAN_CODE,
                    display_name="年度基础套餐",
                    is_active=True,
                    amount_cents=9900,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=5 * 1024**3,
                ),
            ]
        )
        db.commit()
    return result


def _command(
    *,
    tenant_id: str = "tenant-a",
    key: str = "browser-idempotency-rnd380-0001",
    now: datetime = NOW,
) -> CreateOrderCommand:
    return CreateOrderCommand(
        tenant_id=tenant_id,
        plan_code=ANNUAL_PLAN_CODE,
        idempotency_key=key,
        now=now,
    )


def _event(order: PaymentOrder, **changes) -> TrustedPaymentEvent:
    values = {
        "provider": "wechat_pay",
        "provider_event_id": "event-rnd380-0001",
        "provider_order_ref": order.provider_order_ref,
        "provider_transaction_id": "42000000000000000000000380",
        "event_type": "payment_succeeded",
        "app_id": "wx-rnd380",
        "merchant_id": "1900003801",
        "state": "SUCCESS",
        "amount_cents": 9900,
        "currency": "CNY",
        "succeeded_at": NOW,
        "payload_hash": "a" * 64,
        "source": "callback",
    }
    values.update(changes)
    return TrustedPaymentEvent(**values)


def _stored_order(factory, tenant_id: str = "tenant-a") -> PaymentOrder:
    with factory() as db:
        return db.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.tenant_id == tenant_id)
            .order_by(PaymentOrder.created_at.desc())
        )


def test_order_uses_server_plan_and_hashes_browser_idempotency(factory) -> None:
    provider = FakeProvider()
    command = _command()

    first = create_payment_order(factory, provider, command)
    replay = create_payment_order(factory, provider, command)

    assert first.order_id == replay.order_id
    assert first.amount_cents == 9900
    assert first.currency == "CNY"
    assert first.status == "pending"
    assert first.qr_available is True
    assert len(provider.created) == 1
    assert provider.created[0].amount_cents == 9900
    assert provider.created[0].currency == "CNY"
    with factory() as db:
        order = db.scalar(select(PaymentOrder))
        assert order is not None
        assert order.idempotency_key_hash != command.idempotency_key
        assert len(order.idempotency_key_hash) == 64
        assert order.checkout_url.startswith("weixin://")
        assert command.idempotency_key not in " ".join(
            str(value)
            for value in (
                order.idempotency_key_hash,
                order.provider_order_ref,
                order.checkout_url,
            )
        )
        assert get_checkout_url(db, "tenant-a", order.id) == order.checkout_url


def test_provider_create_failure_is_visible_without_partial_checkout(factory) -> None:
    provider = FakeProvider()
    provider.create_error = RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        create_payment_order(factory, provider, _command())

    order = _stored_order(factory)
    assert order.status == "failed"
    assert order.failure_code == "provider_create_failed"
    assert order.checkout_url is None


def test_trusted_payment_activates_once_and_duplicate_event_is_idempotent(factory) -> None:
    provider = FakeProvider()
    create_payment_order(factory, provider, _command())
    order = _stored_order(factory)
    event = _event(order)

    first = apply_trusted_payment(factory, provider, event)
    duplicate = apply_trusted_payment(factory, provider, event)

    assert first.status == duplicate.status == "succeeded"
    assert first.subscription_ends_at is not None
    with factory() as db:
        assert db.query(PaymentEvent).count() == 1
        assert db.query(Subscription).count() == 1
        assert db.query(SubscriptionHistory).count() == 1
        assert db.query(SubscriptionActivation).count() == 1
        assert db.query(AuditLog).filter_by(
            action=AuditAction.SUBSCRIPTION_ACTIVATED
        ).count() == 1
        stored = db.get(PaymentOrder, first.order_id)
        assert stored.checkout_url is None
        assert stored.provider_transaction_id == event.provider_transaction_id


def test_replayed_event_id_with_changed_payload_is_rejected(factory) -> None:
    provider = FakeProvider()
    create_payment_order(factory, provider, _command())
    order = _stored_order(factory)
    event = _event(order)
    apply_trusted_payment(factory, provider, event)

    with pytest.raises(PaymentReplayConflictError):
        apply_trusted_payment(
            factory,
            provider,
            replace(event, payload_hash="b" * 64),
        )
    with factory() as db:
        assert db.query(PaymentEvent).count() == 1
        assert db.query(SubscriptionHistory).count() == 1


@pytest.mark.parametrize(
    ("changes"),
    [
        {"app_id": "wrong-app"},
        {"merchant_id": "wrong-merchant"},
        {"amount_cents": 1},
        {"currency": "USD"},
        {"state": "NOTPAY"},
    ],
)
def test_order_rejects_wrong_identity_amount_currency_or_state(factory, changes) -> None:
    provider = FakeProvider()
    create_payment_order(factory, provider, _command())
    order = _stored_order(factory)

    with pytest.raises(PaymentOrderConflictError):
        apply_trusted_payment(factory, provider, _event(order, **changes))

    with factory() as db:
        stored = db.get(PaymentOrder, order.id)
        assert stored.status == "pending"
        assert db.query(PaymentEvent).count() == 0
        assert db.query(Subscription).count() == 0


def test_unknown_order_reference_fails_without_writes(factory) -> None:
    provider = FakeProvider()
    create_payment_order(factory, provider, _command())
    order = _stored_order(factory)

    with pytest.raises(PaymentOrderNotFoundError):
        apply_trusted_payment(
            factory,
            provider,
            _event(order, provider_order_ref="W-unknown-order"),
        )
    with factory() as db:
        assert db.query(PaymentEvent).count() == 0
        assert db.query(Subscription).count() == 0


def test_paid_activation_failure_is_durable_and_same_event_retries(factory) -> None:
    provider = FakeProvider()
    create_payment_order(factory, provider, _command())
    order = _stored_order(factory)
    event = _event(order)
    with factory() as db:
        db.get(BillingPlan, PLAN_ID).is_active = False
        db.commit()

    with pytest.raises(PaymentActivationPendingError):
        apply_trusted_payment(factory, provider, event)

    with factory() as db:
        stored = db.get(PaymentOrder, order.id)
        assert stored.status == "paid_activation_pending"
        assert stored.provider_transaction_id == event.provider_transaction_id
        assert stored.failure_code == "subscription_update_failed"
        assert db.query(PaymentEvent).count() == 1
        assert db.query(Subscription).count() == 0
        db.get(BillingPlan, PLAN_ID).is_active = True
        db.commit()

    recovered = apply_trusted_payment(factory, provider, event)
    assert recovered.status == "succeeded"
    with factory() as db:
        assert db.query(PaymentEvent).count() == 1
        assert db.query(SubscriptionHistory).count() == 1
        assert db.query(AuditLog).count() == 1


def test_expired_pending_query_closes_provider_and_order(factory) -> None:
    provider = FakeProvider()
    created = create_payment_order(factory, provider, _command())
    provider.query_result = PaymentQueryResult(
        provider="wechat_pay",
        provider_order_ref="placeholder",
        state="NOTPAY",
        status="pending",
        success=None,
    )

    result = query_and_reconcile_order(
        factory,
        provider,
        "tenant-a",
        created.order_id,
        now=created.expires_at + timedelta(seconds=1),
    )

    assert result.status == "closed"
    assert result.provider_state == "CLOSED"
    assert len(provider.closed) == 1
    with factory() as db:
        assert db.get(PaymentOrder, created.order_id).checkout_url is None


def test_manual_close_is_tenant_scoped_and_paid_order_cannot_close(factory) -> None:
    provider = FakeProvider()
    created = create_payment_order(factory, provider, _command())

    with factory() as db:
        with pytest.raises(PaymentOrderNotFoundError):
            get_order(db, "tenant-b", created.order_id)
    closed = close_order(
        factory,
        provider,
        "tenant-a",
        created.order_id,
        now=NOW,
    )
    assert closed.status == "closed"

    second = create_payment_order(
        factory,
        provider,
        _command(key="browser-idempotency-rnd380-0002"),
    )
    with factory() as db:
        order = db.get(PaymentOrder, second.order_id)
    apply_trusted_payment(factory, provider, _event(order, provider_event_id="event-2"))
    with pytest.raises(PaymentOrderConflictError):
        close_order(factory, provider, "tenant-a", second.order_id, now=NOW)


def test_two_distinct_payments_each_add_exactly_one_year(factory) -> None:
    provider = FakeProvider()
    create_payment_order(factory, provider, _command())
    first_order = _stored_order(factory)
    first_result = apply_trusted_payment(factory, provider, _event(first_order))
    second = create_payment_order(
        factory,
        provider,
        _command(key="browser-idempotency-rnd380-0002"),
    )
    with factory() as db:
        second_order = db.get(PaymentOrder, second.order_id)
    second_result = apply_trusted_payment(
        factory,
        provider,
        _event(
            second_order,
            provider_event_id="event-rnd380-0002",
            provider_transaction_id="42000000000000000000000381",
            payload_hash="b" * 64,
        ),
    )

    assert first_result.status == second_result.status == "succeeded"
    with factory() as db:
        subscription = db.scalar(select(Subscription))
        assert subscription.renewal_count == 1
        assert subscription.revision == 2
        assert db.query(PaymentOrder).count() == 2
        assert db.query(PaymentEvent).count() == 2
        assert db.query(SubscriptionHistory).count() == 2


def _postgres_test_url() -> str | None:
    url = os.getenv("RND380_TEST_DATABASE_URL", "").strip()
    if not url:
        return None
    database_name = urlparse(url).path.lstrip("/")
    if not database_name.endswith("test"):
        raise RuntimeError("RND380_TEST_DATABASE_URL must name a database ending in test")
    return url


def _delete_postgres_tenant(factory, tenant_id: str) -> None:
    with factory() as db:
        order_ids = db.scalars(
            select(PaymentOrder.id).where(PaymentOrder.tenant_id == tenant_id)
        ).all()
        subscription_ids = db.scalars(
            select(Subscription.id).where(Subscription.tenant_id == tenant_id)
        ).all()
        if order_ids:
            db.query(PaymentEvent).filter(
                PaymentEvent.order_id.in_(order_ids)
            ).delete(synchronize_session=False)
        db.query(PaymentOrder).filter_by(tenant_id=tenant_id).delete()
        db.query(AuditLog).filter_by(tenant_id=tenant_id).delete()
        db.query(SubscriptionActivation).filter_by(tenant_id=tenant_id).delete()
        db.query(SubscriptionHistory).filter_by(tenant_id=tenant_id).delete()
        if subscription_ids:
            db.query(Subscription).filter(
                Subscription.id.in_(subscription_ids)
            ).delete(synchronize_session=False)
        db.query(Tenant).filter_by(id=tenant_id).delete()
        db.commit()


@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason="RND380_TEST_DATABASE_URL is not configured for isolated PostgreSQL proof",
)
def test_concurrent_postgresql_callback_replay_activates_exactly_once() -> None:
    engine = create_engine(_postgres_test_url(), pool_size=4)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    provider = FakeProvider()
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    with factory() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="RND-380",
                slug=f"rnd380-{suffix}",
                lifecycle_status="active",
            )
        )
        db.commit()
    try:
        created = create_payment_order(
            factory,
            provider,
            _command(
                tenant_id=tenant_id,
                key=f"browser-concurrent-{suffix}",
            ),
        )
        with factory() as db:
            order = db.get(PaymentOrder, created.order_id)
            event = _event(
                order,
                provider_event_id=f"event-{suffix}",
                provider_transaction_id=f"tx-{suffix}",
                payload_hash=uuid.uuid4().hex * 2,
            )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _index: apply_trusted_payment(factory, provider, event),
                    range(2),
                )
            )
        assert {result.status for result in results} == {"succeeded"}
        with factory() as db:
            assert db.query(PaymentEvent).filter_by(order_id=created.order_id).count() == 1
            assert db.query(SubscriptionActivation).filter_by(
                tenant_id=tenant_id
            ).count() == 1
            assert db.query(SubscriptionHistory).filter_by(
                tenant_id=tenant_id
            ).count() == 1
            assert db.query(AuditLog).filter_by(tenant_id=tenant_id).count() == 1
    finally:
        _delete_postgres_tenant(factory, tenant_id)
