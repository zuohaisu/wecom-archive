from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, event, select
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
    PlanEntitlement,
    Subscription,
    SubscriptionActivation,
    SubscriptionHistory,
    Tenant,
)
from app.services.entitlements import (
    ANNUAL_PLAN_CODE,
    PlanUnavailableError,
    assign_subscription,
)
from app.services.subscription_activation import (
    ActivationCommand,
    IdempotencyConflictError,
    InvalidActivationCommandError,
    activate_or_renew_subscription,
)

PLAN_ID = "rnd384-plan"
NOW = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _tables():
    return [
        Tenant.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        SubscriptionHistory.__table__,
        SubscriptionActivation.__table__,
        AdminUser.__table__,
        AuditLog.__table__,
    ]


def _seed(factory, *, plan_id: str = PLAN_ID) -> None:
    with factory() as db:
        db.add_all(
            [
                Tenant(id="tenant-a", name="A", slug="tenant-a"),
                Tenant(id="tenant-b", name="B", slug="tenant-b"),
                BillingPlan(
                    id=plan_id,
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


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_tables())
    result = sessionmaker(bind=engine, expire_on_commit=False)
    _seed(result)
    return result


def _command(
    *,
    tenant_id: str = "tenant-a",
    key: str = "wechat-transaction-0001",
    trusted_at: datetime = NOW,
    plan_code: str = ANNUAL_PLAN_CODE,
) -> ActivationCommand:
    return ActivationCommand(
        tenant_id=tenant_id,
        plan_code=plan_code,
        source="wechat_pay_native",
        idempotency_key=key,
        trusted_at=trusted_at,
    )


def test_first_activation_uses_calendar_months_and_writes_one_atomic_audit(
    factory,
) -> None:
    leap_day = datetime(2024, 2, 29, 13, 30, tzinfo=timezone.utc)
    result = activate_or_renew_subscription(
        factory, _command(trusted_at=leap_day)
    )

    assert result.activation_kind == "activation"
    assert result.starts_at == leap_day
    assert result.ends_at == datetime(2025, 2, 28, 13, 30, tzinfo=timezone.utc)
    assert result.renewal_count == 0
    assert result.subscription_revision == 1
    assert result.replayed is False
    with factory() as db:
        subscription = db.scalar(select(Subscription))
        attempt = db.scalar(select(SubscriptionActivation))
        audit = db.scalar(select(AuditLog))
        assert subscription is not None and subscription.status == "active"
        assert attempt is not None and attempt.status == "applied"
        assert attempt.applied_renewal_count == 0
        assert audit is not None
        assert audit.action == AuditAction.SUBSCRIPTION_ACTIVATED
        assert audit.object_id == subscription.id
        assert audit.detail["plan_code"] == ANNUAL_PLAN_CODE


def test_active_term_renews_from_current_end_and_preserves_original_start(
    factory,
) -> None:
    first = activate_or_renew_subscription(
        factory,
        _command(
            key="wechat-transaction-first",
            trusted_at=datetime(2026, 1, 31, 8, 0, tzinfo=timezone.utc),
        ),
    )
    renewed = activate_or_renew_subscription(
        factory,
        _command(
            key="wechat-transaction-renew",
            trusted_at=datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
        ),
    )

    assert renewed.activation_kind == "renewal"
    assert renewed.starts_at == first.starts_at
    assert renewed.ends_at == datetime(2028, 1, 31, 8, 0, tzinfo=timezone.utc)
    assert renewed.renewal_count == 1
    assert renewed.subscription_revision == 2
    with factory() as db:
        assert db.query(Subscription).count() == 1
        assert db.query(SubscriptionHistory).count() == 2
        assert db.query(AuditLog).filter_by(
            action=AuditAction.SUBSCRIPTION_RENEWED
        ).count() == 1


def test_expired_term_reactivates_from_trusted_payment_time(factory) -> None:
    activate_or_renew_subscription(
        factory,
        _command(
            key="wechat-transaction-expired",
            trusted_at=datetime(2024, 1, 10, tzinfo=timezone.utc),
        ),
    )
    reactivated = activate_or_renew_subscription(
        factory,
        _command(
            key="wechat-transaction-reactivate",
            trusted_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        ),
    )

    assert reactivated.activation_kind == "activation"
    assert reactivated.starts_at == datetime(2026, 8, 13, tzinfo=timezone.utc)
    assert reactivated.ends_at == datetime(2027, 8, 13, tzinfo=timezone.utc)
    assert reactivated.renewal_count == 0
    assert reactivated.subscription_revision == 2


def test_future_active_term_is_not_treated_as_a_current_renewal(factory) -> None:
    with factory() as db:
        assign_subscription(
            db,
            tenant_id="tenant-a",
            plan_code=ANNUAL_PLAN_CODE,
            status="active",
            starts_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            ends_at=datetime(2028, 1, 1, tzinfo=timezone.utc),
            source="future_grant",
        )
        db.commit()

    result = activate_or_renew_subscription(factory, _command())

    assert result.activation_kind == "activation"
    assert result.starts_at == NOW
    assert result.ends_at == datetime(2027, 8, 13, 8, 0, tzinfo=timezone.utc)
    assert result.renewal_count == 0


def test_duplicate_command_replays_exact_result_without_duplicate_side_effects(
    factory,
) -> None:
    command = _command()
    first = activate_or_renew_subscription(factory, command)
    replay = activate_or_renew_subscription(factory, command)

    assert replay == type(replay)(
        **{**first.__dict__, "replayed": True}
    )
    with factory() as db:
        assert db.query(SubscriptionActivation).count() == 1
        assert db.query(SubscriptionHistory).count() == 1
        assert db.query(AuditLog).count() == 1


@pytest.mark.parametrize(
    ("changed_command"),
    [
        _command(tenant_id="tenant-b"),
        _command(trusted_at=datetime(2026, 8, 14, tzinfo=timezone.utc)),
        _command(plan_code="another-plan"),
    ],
)
def test_key_reuse_for_another_tenant_plan_or_time_is_rejected(
    factory, changed_command: ActivationCommand
) -> None:
    activate_or_renew_subscription(factory, _command())

    with pytest.raises(IdempotencyConflictError):
        activate_or_renew_subscription(factory, changed_command)
    with factory() as db:
        assert db.query(SubscriptionActivation).count() == 1
        assert db.query(SubscriptionHistory).count() == 1
        assert db.query(AuditLog).count() == 1


def test_domain_failure_is_traceable_and_leaves_no_partial_domain_state(
    factory,
) -> None:
    command = _command(plan_code="missing-plan")

    with pytest.raises(PlanUnavailableError):
        activate_or_renew_subscription(factory, command)

    with factory() as db:
        attempt = db.scalar(select(SubscriptionActivation))
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.failure_code == "plan_unavailable"
        assert db.query(Subscription).count() == 0
        assert db.query(SubscriptionHistory).count() == 0
        assert db.query(AuditLog).count() == 0


def test_admin_actor_must_belong_to_the_activation_tenant(factory) -> None:
    with factory() as db:
        db.add(
            AdminUser(
                id="tenant-b-admin",
                tenant_id="tenant-b",
                wecom_user_id="tenant-b-user",
                name="B owner",
            )
        )
        db.commit()
    command = ActivationCommand(
        **{**_command().__dict__, "admin_user_id": "tenant-b-admin"}
    )

    with pytest.raises(
        InvalidActivationCommandError,
        match="does not belong",
    ):
        activate_or_renew_subscription(factory, command)
    with factory() as db:
        attempt = db.scalar(select(SubscriptionActivation))
        assert attempt is not None and attempt.status == "failed"
        assert db.query(Subscription).count() == 0
        assert db.query(AuditLog).count() == 0


def test_audit_failure_rolls_back_domain_then_same_command_retries(factory) -> None:
    session_class = factory.class_
    injected = {"raised": False}

    def fail_audit_flush(session, _context, _instances):
        if not injected["raised"] and any(
            isinstance(item, AuditLog) for item in session.new
        ):
            injected["raised"] = True
            raise RuntimeError("injected audit failure")

    event.listen(session_class, "before_flush", fail_audit_flush)
    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            activate_or_renew_subscription(factory, _command())
    finally:
        event.remove(session_class, "before_flush", fail_audit_flush)

    with factory() as db:
        attempt = db.scalar(select(SubscriptionActivation))
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.failure_code == "internal_error"
        assert db.query(Subscription).count() == 0
        assert db.query(SubscriptionHistory).count() == 0
        assert db.query(AuditLog).count() == 0

    retried = activate_or_renew_subscription(factory, _command())
    assert retried.replayed is False
    with factory() as db:
        assert db.scalar(select(SubscriptionActivation)).status == "applied"
        assert db.query(SubscriptionHistory).count() == 1
        assert db.query(AuditLog).count() == 1


def test_raw_idempotency_key_is_never_persisted_or_audited(factory) -> None:
    raw_key = "wechat-sensitive-transaction-raw-value"
    activate_or_renew_subscription(factory, _command(key=raw_key))

    with factory() as db:
        attempt = db.scalar(select(SubscriptionActivation))
        audit = db.scalar(select(AuditLog))
        assert attempt is not None and audit is not None
        persisted = " ".join(
            str(value)
            for value in (
                attempt.idempotency_key_hash,
                attempt.command_hash,
                attempt.plan_code,
                attempt.source,
                audit.detail,
            )
        )
        assert raw_key not in persisted
        assert len(attempt.idempotency_key_hash) == 64


def _postgres_test_url() -> str | None:
    url = os.getenv("RND384_TEST_DATABASE_URL", "").strip()
    if not url:
        return None
    database_name = urlparse(url).path.lstrip("/")
    if not database_name.endswith("test"):
        raise RuntimeError("RND384_TEST_DATABASE_URL must name a database ending in test")
    return url


def _delete_postgres_tenant(factory, tenant_id: str) -> None:
    with factory() as db:
        subscriptions = db.scalars(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        ).all()
        subscription_ids = [row.id for row in subscriptions]
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
    reason="RND384_TEST_DATABASE_URL is not configured for isolated PostgreSQL proof",
)
def test_concurrent_postgresql_replay_applies_exactly_once() -> None:
    engine = create_engine(_postgres_test_url(), pool_size=4)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    command = ActivationCommand(
        tenant_id=tenant_id,
        plan_code=ANNUAL_PLAN_CODE,
        source="wechat_pay_native",
        idempotency_key=f"wechat-concurrent-{suffix}",
        trusted_at=NOW,
    )
    with factory() as db:
        db.add(Tenant(id=tenant_id, name="RND-384", slug=f"rnd384-{suffix}"))
        db.commit()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _index: activate_or_renew_subscription(factory, command),
                    range(2),
                )
            )
        assert {result.subscription_id for result in results} == {
            results[0].subscription_id
        }
        assert sorted(result.replayed for result in results) == [False, True]
        with factory() as db:
            assert db.query(SubscriptionActivation).filter_by(
                tenant_id=tenant_id
            ).count() == 1
            assert db.query(SubscriptionHistory).filter_by(
                tenant_id=tenant_id
            ).count() == 1
            assert db.query(AuditLog).filter_by(tenant_id=tenant_id).count() == 1
    finally:
        _delete_postgres_tenant(factory, tenant_id)
        engine.dispose()


@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason="RND384_TEST_DATABASE_URL is not configured for isolated PostgreSQL proof",
)
def test_distinct_concurrent_postgresql_payments_each_add_one_term() -> None:
    engine = create_engine(_postgres_test_url(), pool_size=4)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    with factory() as db:
        db.add(Tenant(id=tenant_id, name="RND-384", slug=f"rnd384-{suffix}"))
        db.commit()
    commands = [
        ActivationCommand(
            tenant_id=tenant_id,
            plan_code=ANNUAL_PLAN_CODE,
            source="wechat_pay_native",
            idempotency_key=f"wechat-distinct-{suffix}-{index}",
            trusted_at=NOW,
        )
        for index in range(2)
    ]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda item: activate_or_renew_subscription(factory, item),
                    commands,
                )
            )
        assert {result.activation_kind for result in results} == {
            "activation",
            "renewal",
        }
        with factory() as db:
            subscription = db.scalar(
                select(Subscription).where(Subscription.tenant_id == tenant_id)
            )
            assert subscription is not None
            assert subscription.ends_at == datetime(
                2028, 8, 13, 8, 0, tzinfo=timezone.utc
            )
            assert subscription.renewal_count == 1
            assert db.query(SubscriptionActivation).filter_by(
                tenant_id=tenant_id
            ).count() == 2
            assert db.query(SubscriptionHistory).filter_by(
                tenant_id=tenant_id
            ).count() == 2
            assert db.query(AuditLog).filter_by(tenant_id=tenant_id).count() == 2
    finally:
        _delete_postgres_tenant(factory, tenant_id)
        engine.dispose()
