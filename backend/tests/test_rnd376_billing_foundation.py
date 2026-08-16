from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    BillingPlan,
    PlanEntitlement,
    Subscription,
    SubscriptionHistory,
    Tenant,
)
from app.services.entitlements import (
    ANNUAL_PLAN_CODE,
    ARCHIVE_ACCESS,
    UNLIMITED_SEATS,
    PlanUnavailableError,
    SubscriptionAssignmentError,
    TenantNotFoundError,
    assign_subscription,
    get_entitlements,
    get_storage_quota,
    get_subscription_summary,
    has_entitlement,
)

NOW = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
PLAN_ID = "rnd376-plan"
QUOTA = 5 * 1024**3


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            BillingPlan.__table__,
            PlanEntitlement.__table__,
            Subscription.__table__,
            SubscriptionHistory.__table__,
        ],
    )
    with Session(engine) as session:
        session.add_all(
            [
                Tenant(id="tenant-a", name="A", slug="tenant-a"),
                Tenant(id="tenant-b", name="B", slug="tenant-b"),
                BillingPlan(
                    id=PLAN_ID,
                    code=ANNUAL_PLAN_CODE,
                    display_name="年度基础套餐",
                    is_active=True,
                    amount_cents=9900,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=QUOTA,
                ),
                PlanEntitlement(
                    id="entitlement-archive",
                    plan_id=PLAN_ID,
                    capability=ARCHIVE_ACCESS,
                    is_enabled=True,
                ),
                PlanEntitlement(
                    id="entitlement-seats",
                    plan_id=PLAN_ID,
                    capability=UNLIMITED_SEATS,
                    is_enabled=True,
                ),
            ]
        )
        session.commit()
        yield session


def _assign(
    db: Session,
    tenant_id: str = "tenant-a",
    *,
    status: str = "active",
    starts_at: datetime = NOW,
    ends_at: datetime = NOW + timedelta(days=365),
    source: str = "manual_test",
    renewal_count: int = 0,
) -> Subscription:
    return assign_subscription(
        db,
        tenant_id=tenant_id,
        plan_code=ANNUAL_PLAN_CODE,
        status=status,
        starts_at=starts_at,
        ends_at=ends_at,
        source=source,
        renewal_count=renewal_count,
    )


@pytest.mark.parametrize("status", ["trial", "active"])
def test_trial_and_paid_subscription_receive_authoritative_entitlements(
    db: Session, status: str
) -> None:
    _assign(db, status=status)

    summary = get_subscription_summary(db, "tenant-a", at=NOW)
    assert summary is not None
    assert summary.plan_code == ANNUAL_PLAN_CODE
    assert summary.plan_name == "年度基础套餐"
    assert summary.amount_cents == 9900
    assert summary.currency == "CNY"
    assert summary.billing_period_months == 12
    assert summary.storage_quota_bytes == QUOTA
    assert summary.is_entitled is True
    assert summary.effective_status == status
    assert get_entitlements(db, "tenant-a", at=NOW) == frozenset(
        {ARCHIVE_ACCESS, UNLIMITED_SEATS}
    )
    assert has_entitlement(db, "tenant-a", ARCHIVE_ACCESS, at=NOW) is True
    assert get_storage_quota(db, "tenant-a", at=NOW) == QUOTA


@pytest.mark.parametrize("status", ["expired", "canceled"])
def test_non_entitled_statuses_fail_closed(db: Session, status: str) -> None:
    _assign(db, status=status)

    summary = get_subscription_summary(db, "tenant-a", at=NOW)
    assert summary is not None and summary.is_entitled is False
    assert get_entitlements(db, "tenant-a", at=NOW) == frozenset()
    assert get_storage_quota(db, "tenant-a", at=NOW) == 0


def test_grace_remains_entitled_until_its_exclusive_end(db: Session) -> None:
    _assign(
        db,
        status="grace",
        starts_at=NOW - timedelta(days=366),
        ends_at=NOW - timedelta(days=1),
    )

    during = get_subscription_summary(db, "tenant-a", at=NOW)
    at_end = get_subscription_summary(
        db,
        "tenant-a",
        at=NOW + timedelta(days=6),
    )
    assert during is not None and during.effective_status == "grace"
    assert during.is_entitled is True
    assert at_end is not None and at_end.effective_status == "expired"
    assert at_end.is_entitled is False


def test_expiry_and_future_start_are_effective_at_read_time(db: Session) -> None:
    _assign(
        db,
        starts_at=NOW - timedelta(days=365),
        ends_at=NOW,
    )
    grace = get_subscription_summary(db, "tenant-a", at=NOW)
    assert grace is not None
    assert grace.stored_status == "active"
    assert grace.effective_status == "grace"
    assert grace.is_entitled is True

    expired = get_subscription_summary(db, "tenant-a", at=NOW + timedelta(days=7))
    assert expired is not None and expired.effective_status == "expired"
    assert expired.is_entitled is False

    _assign(
        db,
        starts_at=NOW + timedelta(days=1),
        ends_at=NOW + timedelta(days=366),
    )
    future = get_subscription_summary(db, "tenant-a", at=NOW)
    assert future is not None
    assert future.stored_status == "active"
    assert future.is_entitled is False


def test_assignment_reuses_one_current_row_and_appends_history(db: Session) -> None:
    first = _assign(db, status="trial", source="trial_grant")
    db.commit()
    second = _assign(
        db,
        status="active",
        source="trusted_payment",
        renewal_count=1,
    )
    db.commit()

    assert second.id == first.id
    assert second.revision == 2
    assert db.scalars(select(Subscription)).all() == [second]
    history = db.scalars(
        select(SubscriptionHistory).order_by(SubscriptionHistory.revision)
    ).all()
    assert [(row.status, row.revision, row.change_kind) for row in history] == [
        ("trial", 1, "assigned"),
        ("active", 2, "reassigned"),
    ]
    assert history[1].source == "trusted_payment"
    assert history[1].renewal_count == 1


def test_database_rejects_two_current_subscriptions_for_one_tenant(
    db: Session,
) -> None:
    _assign(db)
    db.commit()
    db.add(
        Subscription(
            id="conflicting-current",
            tenant_id="tenant-a",
            plan_id=PLAN_ID,
            status="active",
            starts_at=NOW,
            ends_at=NOW + timedelta(days=30),
            source="direct_tamper",
            renewal_count=0,
            revision=1,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_tenant_isolation_and_missing_subscription_fail_closed(db: Session) -> None:
    _assign(db, "tenant-a")

    assert has_entitlement(db, "tenant-a", ARCHIVE_ACCESS, at=NOW) is True
    assert has_entitlement(db, "tenant-b", ARCHIVE_ACCESS, at=NOW) is False
    assert get_storage_quota(db, "tenant-b", at=NOW) == 0
    assert get_subscription_summary(db, "tenant-b", at=NOW) is None


def test_inactive_plan_blocks_assignment_but_not_existing_term(db: Session) -> None:
    _assign(db)
    db.commit()
    plan = db.get(BillingPlan, PLAN_ID)
    assert plan is not None
    plan.is_active = False
    db.commit()

    assert has_entitlement(db, "tenant-a", ARCHIVE_ACCESS, at=NOW) is True
    with pytest.raises(PlanUnavailableError):
        _assign(db, "tenant-b")


def test_assignment_rejects_untrusted_or_invalid_commercial_input(
    db: Session,
) -> None:
    with pytest.raises(PlanUnavailableError):
        assign_subscription(
            db,
            tenant_id="tenant-a",
            plan_code="client-invented-plan",
            status="active",
            starts_at=NOW,
            ends_at=NOW + timedelta(days=1),
            source="browser",
        )
    with pytest.raises(TenantNotFoundError):
        _assign(db, "client-invented-tenant")
    with pytest.raises(SubscriptionAssignmentError):
        _assign(db, status="client-paid")
    with pytest.raises(SubscriptionAssignmentError):
        _assign(db, ends_at=NOW)
    with pytest.raises(TypeError):
        assign_subscription(  # type: ignore[call-arg]
            db,
            tenant_id="tenant-a",
            plan_code=ANNUAL_PLAN_CODE,
            status="active",
            starts_at=NOW,
            ends_at=NOW + timedelta(days=1),
            source="browser",
            amount_cents=1,
        )
