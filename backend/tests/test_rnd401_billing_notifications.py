from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AdminUser,
    BillingNotificationAttempt,
    BillingNotificationIntent,
    BillingPlan,
    PaymentOrder,
    PaymentRecoveryFinding,
    PlatformAdmin,
    RefundOrder,
    Subscription,
    Tenant,
)
from app.email import _render_billing_notification_email
from app.services.billing_notifications import (
    plan_billing_notification_intents,
    run_billing_notifications_once,
)

NOW = datetime(2028, 1, 30, 8, 0, tzinfo=timezone.utc)
PLAN_ID = "rnd401-plan"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            PlatformAdmin.__table__,
            BillingPlan.__table__,
            Subscription.__table__,
            AdminUser.__table__,
            PaymentOrder.__table__,
            PaymentRecoveryFinding.__table__,
            RefundOrder.__table__,
            BillingNotificationIntent.__table__,
            BillingNotificationAttempt.__table__,
        ],
    )
    result = sessionmaker(bind=engine, expire_on_commit=False)
    with result() as db:
        db.add_all(
            [
                Tenant(id="tenant-a", name="A", slug="tenant-a"),
                Tenant(id="tenant-b", name="B", slug="tenant-b"),
                PlatformAdmin(
                    id="platform-admin",
                    email="ops@example.test",
                    password_hash="unused",
                    status="active",
                ),
                BillingPlan(
                    id=PLAN_ID,
                    code="annual_base",
                    display_name="年度基础套餐",
                    is_active=True,
                    amount_cents=9900,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=5 * 1024**3,
                ),
                AdminUser(
                    id="owner-a",
                    tenant_id="tenant-a",
                    wecom_user_id="owner-a",
                    name="Owner A",
                    email="owner-a@example.test",
                    role="owner",
                    status="active",
                    ui_locale="zh-CN",
                ),
                AdminUser(
                    id="owner-b",
                    tenant_id="tenant-b",
                    wecom_user_id="owner-b",
                    name="Owner B",
                    email="owner-b@example.test",
                    role="owner",
                    status="active",
                    ui_locale="en",
                ),
            ]
        )
        db.commit()
    return result


def _subscription(
    tenant_id: str,
    *,
    ends_at: datetime,
    status: str = "active",
    revision: int = 1,
) -> Subscription:
    return Subscription(
        id=f"subscription-{tenant_id}",
        tenant_id=tenant_id,
        plan_id=PLAN_ID,
        status=status,
        starts_at=ends_at - timedelta(days=365),
        ends_at=ends_at,
        grace_ends_at=ends_at + timedelta(days=7),
        source="rnd401_test",
        renewal_count=max(0, revision - 1),
        revision=revision,
    )


def _payment(tenant_id: str, *, status: str, paid_at: datetime) -> PaymentOrder:
    return PaymentOrder(
        id=f"payment-{tenant_id}",
        tenant_id=tenant_id,
        plan_id=PLAN_ID,
        plan_code="annual_base",
        plan_name="年度基础套餐",
        amount_cents=9900,
        currency="CNY",
        provider="wechat_pay_native",
        provider_order_ref=f"provider-{tenant_id}",
        provider_transaction_id=f"transaction-{tenant_id}",
        provider_state="SUCCESS",
        status=status,
        idempotency_key_hash="a" * 64,
        created_at=paid_at - timedelta(minutes=5),
        expires_at=paid_at + timedelta(minutes=10),
        paid_at=paid_at,
    )


def _refund(
    tenant_id: str,
    *,
    status: str,
    requested_at: datetime,
) -> RefundOrder:
    return RefundOrder(
        id=f"refund-{tenant_id}",
        tenant_id=tenant_id,
        payment_order_id=f"payment-{tenant_id}",
        term_grant_id=f"grant-{tenant_id}",
        amount_cents=9900,
        currency="CNY",
        provider="wechat_pay_native",
        provider_ref=f"refund-provider-{tenant_id}",
        provider_state=status.upper(),
        status=status,
        reason_code="customer_request",
        approved_by_platform_admin_id="platform-admin",
        idempotency_key_hash="b" * 64,
        failure_code=("provider_abnormal" if status == "abnormal" else None),
        requested_at=requested_at,
        provider_accepted_at=requested_at,
        updated_at=requested_at,
    )


def test_leap_day_thresholds_are_utc_and_scheduler_is_idempotent(factory) -> None:
    leap_day = datetime(2028, 2, 29, 8, 0, tzinfo=timezone.utc)
    at = leap_day - timedelta(days=30)
    with factory() as db:
        db.add(_subscription("tenant-a", ends_at=leap_day))
        db.commit()

        first = plan_billing_notification_intents(db, at=at)
        db.commit()
        second = plan_billing_notification_intents(db, at=at)
        db.commit()

        intents = db.scalars(
            select(BillingNotificationIntent).order_by(
                BillingNotificationIntent.scheduled_at
            )
        ).all()
        assert first == (6, 0)
        assert second == (0, 0)
        assert len(intents) == 6
        threshold = next(
            row for row in intents if row.kind == "subscription_expiry_30d"
        )
        assert threshold.scheduled_at.replace(tzinfo=timezone.utc) == at
        assert threshold.source_revision == 1


def test_successful_delivery_is_not_repeated_by_the_next_job(factory, monkeypatch) -> None:
    ends_at = NOW + timedelta(days=30)
    sent: list[tuple[str, str, datetime, str, str]] = []
    monkeypatch.setattr(
        "app.services.billing_notifications.get_wecom_oauth_settings",
        lambda: SimpleNamespace(admin_domain="billing.example.test"),
    )
    with factory() as db:
        db.add(_subscription("tenant-a", ends_at=ends_at))
        db.commit()

        def sender(*args):
            sent.append(args)
            return True

        first = run_billing_notifications_once(db, at=NOW, delivery=sender)
        second = run_billing_notifications_once(db, at=NOW, delivery=sender)

        assert first.sent == 1
        assert second.sent == 0
        assert len(sent) == 1
        assert sent[0][0] == "owner-a@example.test"
        assert sent[0][1] == "subscription_expiry_30d"
        assert db.query(BillingNotificationAttempt).count() == 1


def test_renewal_cancels_old_future_intents_without_deleting_history(factory) -> None:
    original_end = NOW + timedelta(days=40)
    with factory() as db:
        subscription = _subscription("tenant-a", ends_at=original_end)
        db.add(subscription)
        db.commit()
        assert plan_billing_notification_intents(db, at=NOW) == (6, 0)
        db.commit()

        subscription.ends_at = original_end + timedelta(days=365)
        subscription.grace_ends_at = subscription.ends_at + timedelta(days=7)
        subscription.revision += 1
        db.commit()
        scheduled, canceled = plan_billing_notification_intents(db, at=NOW)
        db.commit()

        assert (scheduled, canceled) == (6, 6)
        assert db.query(BillingNotificationIntent).filter_by(status="canceled").count() == 6
        assert db.query(BillingNotificationIntent).filter_by(status="pending").count() == 6
        assert db.query(BillingNotificationIntent).count() == 12


def test_missing_transport_is_observable_and_retry_can_later_succeed(
    factory, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.billing_notifications.get_wecom_oauth_settings",
        lambda: SimpleNamespace(admin_domain="billing.example.test"),
    )
    monkeypatch.setattr(
        "app.services.billing_notifications.email_delivery_ready",
        lambda: False,
    )
    with factory() as db:
        db.add(_subscription("tenant-a", ends_at=NOW + timedelta(days=30)))
        db.commit()
        first = run_billing_notifications_once(db, at=NOW)
        intent = db.scalar(
            select(BillingNotificationIntent).where(
                BillingNotificationIntent.kind == "subscription_expiry_30d"
            )
        )
        attempt = db.scalar(select(BillingNotificationAttempt))
        assert first.retried == 1
        assert intent is not None and intent.status == "pending"
        assert intent.next_attempt_at.replace(tzinfo=timezone.utc) == NOW + timedelta(
            minutes=5
        )
        assert attempt is not None and attempt.failure_code == "transport_unconfigured"

        second = run_billing_notifications_once(
            db,
            at=NOW + timedelta(minutes=5),
            delivery=lambda *_args: True,
        )
        db.refresh(intent)
        assert second.sent == 1
        assert intent.status == "sent"
        assert db.query(BillingNotificationAttempt).count() == 2


def test_delivery_failure_for_one_tenant_does_not_block_another(factory, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.billing_notifications.get_wecom_oauth_settings",
        lambda: SimpleNamespace(admin_domain="billing.example.test"),
    )
    with factory() as db:
        db.add_all(
            [
                _subscription("tenant-a", ends_at=NOW + timedelta(days=30)),
                _subscription("tenant-b", ends_at=NOW + timedelta(days=30)),
            ]
        )
        db.commit()

        summary = run_billing_notifications_once(
            db,
            at=NOW,
            delivery=lambda email, *_args: email.endswith("owner-b@example.test"),
        )

        assert summary.sent == 1
        assert summary.retried == 1
        states = dict(
            db.execute(
                select(
                    BillingNotificationIntent.tenant_id,
                    BillingNotificationIntent.status,
                ).where(
                    BillingNotificationIntent.kind == "subscription_expiry_30d"
                )
            ).all()
        )
        assert states == {"tenant-a": "pending", "tenant-b": "sent"}


def test_delivery_backoff_doubles_and_stops_after_fixed_attempt_budget(
    factory, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.billing_notifications.get_wecom_oauth_settings",
        lambda: SimpleNamespace(admin_domain="billing.example.test"),
    )
    with factory() as db:
        db.add(_subscription("tenant-a", ends_at=NOW + timedelta(days=30)))
        db.commit()
        elapsed_minutes = (0, 5, 15, 35, 75)
        for elapsed in elapsed_minutes:
            run_billing_notifications_once(
                db,
                at=NOW + timedelta(minutes=elapsed),
                delivery=lambda *_args: False,
            )

        intent = db.scalar(
            select(BillingNotificationIntent).where(
                BillingNotificationIntent.kind == "subscription_expiry_30d"
            )
        )
        attempts = db.scalars(
            select(BillingNotificationAttempt)
            .where(BillingNotificationAttempt.intent_id == intent.id)
            .order_by(BillingNotificationAttempt.attempt_no)
        ).all()
        assert intent is not None and intent.status == "failed"
        assert [row.attempt_no for row in attempts] == [1, 2, 3, 4, 5]
        assert [
            row.attempted_at.replace(tzinfo=timezone.utc) for row in attempts
        ] == [NOW + timedelta(minutes=value) for value in elapsed_minutes]


def test_payment_and_refund_anomaly_intents_cancel_when_state_changes(factory) -> None:
    with factory() as db:
        payment = _payment(
            "tenant-a", status="paid_activation_pending", paid_at=NOW
        )
        refund = _refund("tenant-b", status="processing", requested_at=NOW)
        db.add_all([payment, refund])
        db.commit()

        assert plan_billing_notification_intents(db, at=NOW) == (2, 0)
        db.commit()
        refund_timeout = db.scalar(
            select(BillingNotificationIntent).where(
                BillingNotificationIntent.kind == "refund_processing_timeout"
            )
        )
        assert refund_timeout is not None
        assert refund_timeout.scheduled_at.replace(tzinfo=timezone.utc) == NOW + timedelta(
            hours=24
        )

        payment.status = "succeeded"
        payment.activation_id = "activation-a"
        payment.activated_at = NOW + timedelta(minutes=1)
        refund.status = "abnormal"
        refund.provider_state = "ABNORMAL"
        refund.failure_code = "provider_abnormal"
        refund.updated_at = NOW + timedelta(hours=1)
        db.commit()

        scheduled, canceled = plan_billing_notification_intents(
            db, at=NOW + timedelta(hours=1)
        )
        db.commit()
        assert (scheduled, canceled) == (1, 2)
        assert db.query(BillingNotificationIntent).filter_by(
            kind="refund_abnormal", status="pending"
        ).count() == 1
        assert db.query(BillingNotificationIntent).filter_by(status="canceled").count() == 2


def test_open_payment_recovery_finding_creates_and_cancels_global_operations_intent(factory) -> None:
    with factory() as db:
        db.add(
            PaymentRecoveryFinding(
                id="finding-rnd390",
                provider="wechat_pay",
                tenant_id=None,
                payment_order_id=None,
                kind="payment_callback_signature_failure",
                severity="critical",
                status="open",
                dedupe_key="f" * 64,
                occurrence_count=1,
                first_detected_at=NOW,
                last_detected_at=NOW,
            )
        )
        db.commit()

        assert plan_billing_notification_intents(db, at=NOW) == (1, 0)
        db.commit()
        intent = db.scalar(
            select(BillingNotificationIntent).where(
                BillingNotificationIntent.subject_type == "payment_recovery_finding"
            )
        )
        assert intent is not None
        assert intent.tenant_id is None
        assert intent.audience == "operations"

        db.get(PaymentRecoveryFinding, "finding-rnd390").status = "resolved"
        db.commit()
        assert plan_billing_notification_intents(db, at=NOW + timedelta(minutes=5)) == (0, 1)
        db.commit()
        db.refresh(intent)
        assert intent.status == "canceled"


def test_frozen_notice_waits_for_authoritative_tenant_projection(factory, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.billing_notifications.get_wecom_oauth_settings",
        lambda: SimpleNamespace(admin_domain="billing.example.test"),
    )
    grace_end = NOW
    with factory() as db:
        subscription = _subscription(
            "tenant-a",
            ends_at=grace_end - timedelta(days=7),
            status="expired",
            revision=3,
        )
        db.add(subscription)
        db.commit()
        first = run_billing_notifications_once(
            db, at=NOW, delivery=lambda *_args: True
        )
        frozen_intent = db.scalar(
            select(BillingNotificationIntent).where(
                BillingNotificationIntent.kind == "tenant_frozen"
            )
        )
        assert first.deferred == 1
        assert frozen_intent is not None and frozen_intent.status == "pending"

        tenant = db.get(Tenant, "tenant-a")
        tenant.lifecycle_status = "frozen"
        db.commit()
        second = run_billing_notifications_once(
            db,
            at=NOW + timedelta(minutes=5),
            delivery=lambda *_args: True,
        )
        db.refresh(frozen_intent)
        assert second.sent == 1
        assert frozen_intent.status == "sent"


def test_email_template_contains_only_fixed_state_time_and_safe_link() -> None:
    subject, body = _render_billing_notification_email(
        "refund_abnormal",
        NOW,
        "https://billing.example.test/platform/operations",
        "zh-CN",
    )
    assert subject == "退款状态异常"
    assert NOW.isoformat() in body
    assert "https://billing.example.test/platform/operations" in body
    for forbidden in ("transaction", "CorpID", "UserID", "secret", "openid"):
        assert forbidden not in body
