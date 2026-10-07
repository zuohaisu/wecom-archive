from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit import AuditAction
from app.db.base import Base
from app.db.models import (
    AdminUser,
    AuditLog,
    BillingPlan,
    PlanEntitlement,
    PlatformAdmin,
    Subscription,
    SubscriptionActivation,
    SubscriptionHistory,
    Tenant,
)
from app.services.billing_lifecycle import (
    BillingLifecycleError,
    reconcile_tenant_billing_lifecycle,
    resume_tenant_service,
    suspend_tenant_service,
)
from app.services.entitlements import (
    ANNUAL_PLAN_CODE,
    ARCHIVE_ACCESS,
    assign_subscription,
    get_subscription_summary,
    has_entitlement,
)
from app.services.subscription_activation import (
    ActivationCommand,
    activate_or_renew_subscription,
)

NOW = datetime(2026, 8, 16, 8, 0, tzinfo=timezone.utc)
PLAN_ID = "rnd400-plan"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _tables():
    return [
        Tenant.__table__,
        PlatformAdmin.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        SubscriptionHistory.__table__,
        SubscriptionActivation.__table__,
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
                Tenant(id="tenant-a", name="A", slug="tenant-a"),
                Tenant(id="tenant-b", name="B", slug="tenant-b"),
                PlatformAdmin(
                    id="platform-admin",
                    email="platform@example.test",
                    password_hash="unused-in-domain-test",
                    status="active",
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
                PlanEntitlement(
                    id="rnd400-archive",
                    plan_id=PLAN_ID,
                    capability=ARCHIVE_ACCESS,
                    is_enabled=True,
                ),
                AdminUser(
                    id="owner-a",
                    tenant_id="tenant-a",
                    wecom_user_id="owner-a",
                    name="Owner A",
                    role="owner",
                    status="active",
                ),
                AdminUser(
                    id="owner-b",
                    tenant_id="tenant-b",
                    wecom_user_id="owner-b",
                    name="Owner B",
                    role="owner",
                    status="active",
                ),
            ]
        )
        db.commit()
    return result


def _assign(
    db: Session,
    *,
    tenant_id: str = "tenant-a",
    status: str = "active",
    starts_at: datetime = NOW - timedelta(days=365),
    ends_at: datetime = NOW,
    cancel_at_period_end: bool = False,
) -> Subscription:
    return assign_subscription(
        db,
        tenant_id=tenant_id,
        plan_code=ANNUAL_PLAN_CODE,
        status=status,
        starts_at=starts_at,
        ends_at=ends_at,
        source="rnd400_test",
        cancel_at_period_end=cancel_at_period_end,
    )


def _activate(factory, *, key: str, trusted_at: datetime = NOW):
    return activate_or_renew_subscription(
        factory,
        ActivationCommand(
            tenant_id="tenant-a",
            plan_code=ANNUAL_PLAN_CODE,
            source="wechat_pay_native",
            idempotency_key=key,
            trusted_at=trusted_at,
        ),
    )


def test_active_enters_grace_at_exact_end_and_replay_has_no_duplicate_side_effects(
    factory,
) -> None:
    with factory() as db:
        subscription = _assign(db)
        db.commit()
        first = reconcile_tenant_billing_lifecycle(db, "tenant-a", at=NOW)
        db.commit()
        second = reconcile_tenant_billing_lifecycle(db, "tenant-a", at=NOW)
        db.commit()

        assert first.subscription_changed is True
        assert first.tenant_changed is False
        assert first.subscription_status == "grace"
        assert second.subscription_changed is second.tenant_changed is False
        assert subscription.status == "grace"
        assert subscription.revision == 2
        assert db.query(SubscriptionHistory).count() == 2
        assert db.query(AuditLog).filter_by(
            action=AuditAction.SUBSCRIPTION_GRACE_STARTED
        ).count() == 1

        summary = get_subscription_summary(db, "tenant-a", at=NOW)
        assert summary is not None
        assert summary.effective_status == "grace"
        assert summary.is_entitled is True
        assert summary.grace_ends_at == NOW + timedelta(days=7)
        assert has_entitlement(db, "tenant-a", ARCHIVE_ACCESS, at=NOW) is True


def test_grace_end_expires_and_freezes_at_the_exact_exclusive_boundary(factory) -> None:
    with factory() as db:
        _assign(db)
        db.commit()
        reconcile_tenant_billing_lifecycle(db, "tenant-a", at=NOW)
        db.commit()

        boundary = NOW + timedelta(days=7)
        result = reconcile_tenant_billing_lifecycle(
            db,
            "tenant-a",
            at=boundary,
        )
        db.commit()
        replay = reconcile_tenant_billing_lifecycle(
            db,
            "tenant-a",
            at=boundary,
        )
        db.commit()

        subscription = db.scalar(select(Subscription))
        tenant = db.get(Tenant, "tenant-a")
        assert result.subscription_changed is result.tenant_changed is True
        assert replay.subscription_changed is replay.tenant_changed is False
        assert subscription is not None and subscription.status == "expired"
        assert subscription.revision == 3
        assert tenant is not None and tenant.lifecycle_status == "frozen"
        assert tenant.frozen_at is not None
        assert tenant.frozen_at.replace(tzinfo=timezone.utc) == boundary
        assert tenant.lifecycle_revision == 2
        assert db.query(SubscriptionHistory).count() == 3
        actions = [row.action for row in db.query(AuditLog).all()]
        assert actions.count(AuditAction.SUBSCRIPTION_EXPIRED) == 1
        assert actions.count(AuditAction.TENANT_BILLING_FROZEN) == 1
        assert has_entitlement(db, "tenant-a", ARCHIVE_ACCESS, at=boundary) is False


def test_grace_renewal_extends_from_original_end_and_restores_billing_freeze(
    factory,
) -> None:
    original_end = NOW - timedelta(days=2)
    with factory() as db:
        _assign(
            db,
            starts_at=datetime(2025, 8, 14, 8, 0, tzinfo=timezone.utc),
            ends_at=original_end,
            cancel_at_period_end=True,
        )
        tenant = db.get(Tenant, "tenant-a")
        assert tenant is not None
        tenant.lifecycle_status = "frozen"
        tenant.frozen_at = NOW - timedelta(days=1)
        db.commit()

    result = _activate(factory, key="rnd400-grace-renewal")

    assert result.activation_kind == "renewal"
    assert result.ends_at == datetime(2027, 8, 14, 8, 0, tzinfo=timezone.utc)
    with factory() as db:
        subscription = db.scalar(select(Subscription))
        tenant = db.get(Tenant, "tenant-a")
        assert subscription is not None and subscription.status == "active"
        assert subscription.cancel_at_period_end is False
        assert tenant is not None and tenant.lifecycle_status == "active"
        assert tenant.frozen_at is None
        assert db.query(AuditLog).filter_by(
            action=AuditAction.TENANT_BILLING_RESTORED
        ).count() == 1


def test_expired_renewal_restarts_at_payment_but_never_clears_manual_suspension(
    factory,
) -> None:
    with factory() as db:
        _assign(
            db,
            status="expired",
            starts_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ends_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        db.commit()
        suspend_tenant_service(
            db,
            "tenant-a",
            platform_admin_id="platform-admin",
            reason_code="security_review",
            at=NOW - timedelta(hours=1),
        )
        db.commit()

    result = _activate(factory, key="rnd400-expired-reactivation")

    assert result.activation_kind == "activation"
    assert result.starts_at == NOW
    assert result.ends_at == datetime(2027, 8, 16, 8, 0, tzinfo=timezone.utc)
    with factory() as db:
        subscription = db.scalar(select(Subscription))
        tenant = db.get(Tenant, "tenant-a")
        assert subscription is not None and subscription.status == "active"
        assert tenant is not None and tenant.lifecycle_status == "suspended"
        assert tenant.suspension_reason == "security_review"
        assert db.query(AuditLog).filter_by(
            action=AuditAction.TENANT_BILLING_RESTORED
        ).count() == 0


def test_manual_suspend_and_resume_are_idempotent_audited_and_reproject_state(
    factory,
) -> None:
    with factory() as db:
        _assign(db, ends_at=NOW + timedelta(days=30))
        db.commit()
        suspended = suspend_tenant_service(
            db,
            "tenant-a",
            platform_admin_id="platform-admin",
            reason_code="risk_review",
            at=NOW,
        )
        db.commit()
        replay = suspend_tenant_service(
            db,
            "tenant-a",
            platform_admin_id="platform-admin",
            reason_code="risk_review",
            at=NOW + timedelta(minutes=1),
        )
        db.commit()

        assert suspended.changed is True and replay.changed is False
        tenant = db.get(Tenant, "tenant-a")
        assert tenant is not None
        assert tenant.lifecycle_status == "suspended"
        assert tenant.suspension_previous_status == "active"
        assert tenant.lifecycle_revision == 2
        assert db.query(AuditLog).filter_by(
            action=AuditAction.PLATFORM_TENANT_SUSPENDED
        ).count() == 1

        resumed = resume_tenant_service(
            db,
            "tenant-a",
            platform_admin_id="platform-admin",
            at=NOW + timedelta(minutes=2),
        )
        db.commit()
        assert resumed.lifecycle_status == "active" and resumed.changed is True
        assert tenant.lifecycle_revision == 3
        assert tenant.suspended_at is None
        assert tenant.suspension_reason is None
        assert tenant.suspended_by_platform_admin_id is None
        assert tenant.suspension_previous_status is None
        assert db.query(AuditLog).filter_by(
            action=AuditAction.PLATFORM_TENANT_RESUMED
        ).count() == 1


def test_resume_after_subscription_expiry_returns_to_frozen_not_active(factory) -> None:
    with factory() as db:
        _assign(
            db,
            status="expired",
            starts_at=NOW - timedelta(days=400),
            ends_at=NOW - timedelta(days=35),
        )
        db.commit()
        suspend_tenant_service(
            db,
            "tenant-a",
            platform_admin_id="platform-admin",
            reason_code="security_review",
            at=NOW,
        )
        db.commit()
        result = resume_tenant_service(
            db,
            "tenant-a",
            platform_admin_id="platform-admin",
            at=NOW + timedelta(minutes=1),
        )
        db.commit()

        tenant = db.get(Tenant, "tenant-a")
        assert result.lifecycle_status == "frozen"
        assert tenant is not None and tenant.lifecycle_status == "frozen"
        assert tenant.frozen_at is not None
        assert tenant.frozen_at.replace(tzinfo=timezone.utc) == NOW + timedelta(
            minutes=1
        )


def test_mutations_reject_naive_time_without_side_effects(factory) -> None:
    with factory() as db:
        _assign(db)
        db.commit()
        with pytest.raises(BillingLifecycleError, match="timezone"):
            reconcile_tenant_billing_lifecycle(
                db,
                "tenant-a",
                at=datetime(2026, 8, 16, 8, 0),
            )
        assert db.query(SubscriptionHistory).count() == 1
        assert db.query(AuditLog).count() == 0


def _postgres_test_url() -> str | None:
    url = os.getenv("RND400_TEST_DATABASE_URL", "").strip()
    if not url:
        return None
    if not urlparse(url).path.lstrip("/").endswith("test"):
        raise RuntimeError("RND400_TEST_DATABASE_URL must name a database ending in test")
    return url


@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason="RND400_TEST_DATABASE_URL is not configured for isolated PostgreSQL proof",
)
def test_concurrent_postgresql_reconcile_applies_one_transition() -> None:
    engine = create_engine(_postgres_test_url(), pool_size=4)
    postgres_factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    with postgres_factory() as db:
        db.add(Tenant(id=tenant_id, name="RND-400", slug=f"rnd400-{suffix}"))
        assign_subscription(
            db,
            tenant_id=tenant_id,
            plan_code=ANNUAL_PLAN_CODE,
            status="active",
            starts_at=NOW - timedelta(days=365),
            ends_at=NOW,
            source="rnd400_concurrency",
        )
        db.commit()

    def reconcile() -> bool:
        with postgres_factory() as db:
            result = reconcile_tenant_billing_lifecycle(db, tenant_id, at=NOW)
            db.commit()
            return result.subscription_changed

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            changed = list(executor.map(lambda _index: reconcile(), range(2)))
        assert changed.count(True) == 1
        with postgres_factory() as db:
            subscription = db.scalar(
                select(Subscription).where(Subscription.tenant_id == tenant_id)
            )
            assert subscription is not None
            assert subscription.status == "grace" and subscription.revision == 2
            assert db.query(SubscriptionHistory).filter_by(
                tenant_id=tenant_id
            ).count() == 2
    finally:
        with postgres_factory() as db:
            db.query(AuditLog).filter_by(tenant_id=tenant_id).delete()
            db.query(SubscriptionHistory).filter_by(tenant_id=tenant_id).delete()
            db.query(Subscription).filter_by(tenant_id=tenant_id).delete()
            db.query(Tenant).filter_by(id=tenant_id).delete()
            db.commit()
        engine.dispose()
