from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    PlanEntitlement,
    Subscription,
    SubscriptionHistory,
    Tenant,
    ThirdPartyOrganizationBinding,
)
from app.services.entitlements import ANNUAL_PLAN_CODE
from app.services.trial_subscriptions import (
    TRIAL_DURATION,
    TRIAL_SOURCE,
    TrialAlreadyUsedError,
    TrialGrantCommand,
    TrialNotEligibleError,
    grant_self_service_trial,
)

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)


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
        AdminUser.__table__,
        AuditLog.__table__,
        ThirdPartyOrganizationBinding.__table__,
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
                    id="tenant-ready",
                    name="Ready organization",
                    slug="tenant-ready",
                    is_active=False,
                    lifecycle_status="provisioning",
                ),
                Tenant(
                    id="tenant-untrusted",
                    name="Untrusted organization",
                    slug="tenant-untrusted",
                ),
                BillingPlan(
                    id="annual-plan",
                    code=ANNUAL_PLAN_CODE,
                    display_name="年度基础套餐",
                    is_active=True,
                    amount_cents=9900,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=5 * 1024**3,
                ),
                PlanEntitlement(
                    id="archive-access",
                    plan_id="annual-plan",
                    capability="archive_access",
                    is_enabled=True,
                ),
                ThirdPartyOrganizationBinding(
                    id="ready-binding",
                    tenant_id="tenant-ready",
                    corp_id="ww-ready",
                    permanent_code_encrypted="not-a-real-secret",
                    authorization_mode="admin",
                ),
                AdminUser(
                    id="ready-owner",
                    tenant_id="tenant-ready",
                    wecom_user_id="owner",
                    role="owner",
                    status="active",
                ),
            ]
        )
        db.commit()
    return result


def _command(*, tenant_id: str = "tenant-ready", trusted_at: datetime = NOW):
    return TrialGrantCommand(tenant_id=tenant_id, trusted_at=trusted_at)


def test_trusted_configuration_grants_exactly_fifteen_days_and_audits(factory) -> None:
    with factory() as db:
        result = grant_self_service_trial(db, _command())
        db.commit()

    assert result.replayed is False
    assert result.starts_at == NOW
    assert result.ends_at == NOW + TRIAL_DURATION
    assert result.subscription_revision == 1
    with factory() as db:
        subscription = db.scalar(select(Subscription))
        history = db.scalar(select(SubscriptionHistory))
        audit = db.scalar(select(AuditLog))
        assert subscription is not None
        assert subscription.status == "trial"
        assert subscription.source == TRIAL_SOURCE
        assert history is not None and history.status == "trial"
        assert audit is not None
        assert audit.action == AuditAction.SUBSCRIPTION_TRIAL_STARTED
        assert audit.admin_user_id == "ready-owner"
        assert audit.object_id == subscription.id
        assert audit.detail == {"duration_hours": 360, "plan_code": ANNUAL_PLAN_CODE}


def test_active_trial_replays_without_duplicate_history_or_audit(factory) -> None:
    with factory() as db:
        first = grant_self_service_trial(db, _command())
        db.commit()
    with factory() as db:
        replay = grant_self_service_trial(db, _command(trusted_at=NOW + timedelta(days=1)))
        db.commit()

    assert replay.replayed is True
    assert replay.subscription_id == first.subscription_id
    assert replay.starts_at == first.starts_at
    with factory() as db:
        assert db.query(SubscriptionHistory).count() == 1
        assert db.query(AuditLog).count() == 1


def test_trial_can_never_be_granted_again_after_expiry(factory) -> None:
    with factory() as db:
        grant_self_service_trial(db, _command())
        db.commit()
    with factory() as db:
        with pytest.raises(TrialAlreadyUsedError):
            grant_self_service_trial(db, _command(trusted_at=NOW + TRIAL_DURATION))
        db.rollback()

    with factory() as db:
        assert db.query(SubscriptionHistory).count() == 1
        assert db.query(AuditLog).count() == 1


def test_untrusted_organization_cannot_receive_a_trial(factory) -> None:
    with factory() as db:
        with pytest.raises(TrialNotEligibleError, match="ownership"):
            grant_self_service_trial(db, _command(tenant_id="tenant-untrusted"))
        db.rollback()

    with factory() as db:
        assert db.query(Subscription).count() == 0
        assert db.query(AuditLog).count() == 0


def test_disabled_owner_cannot_start_a_trial(factory) -> None:
    with factory() as db:
        owner = db.get(AdminUser, "ready-owner")
        assert owner is not None
        owner.status = "disabled"
        db.commit()
    with factory() as db:
        with pytest.raises(TrialNotEligibleError, match="ownership"):
            grant_self_service_trial(db, _command())
        db.rollback()

    with factory() as db:
        assert db.query(Subscription).count() == 0
