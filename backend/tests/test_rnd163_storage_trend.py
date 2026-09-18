"""RND-163: storage trend, daily growth, and depletion estimate tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    ArchiveMessage,
    BillingPlan,
    MediaFile,
    PlanEntitlement,
    Subscription,
    Tenant,
    TenantStorageDaily,
)
from app.services.entitlements import ANNUAL_PLAN_CODE
from app.services.storage_trend import (
    compute_daily_growth,
    project_days_until_full,
    storage_trend,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
GIB = 1024**3
DAY = timedelta(days=1)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    fts_index = next(
        index
        for index in ArchiveMessage.__table__.indexes
        if index.name == "ix_archive_messages_content_text_fts"
    )
    ArchiveMessage.__table__.indexes.remove(fts_index)
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                Tenant.__table__,
                BillingPlan.__table__,
                PlanEntitlement.__table__,
                Subscription.__table__,
                ArchiveMessage.__table__,
                MediaFile.__table__,
                TenantStorageDaily.__table__,
            ],
        )
    finally:
        ArchiveMessage.__table__.indexes.add(fts_index)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Tenant(id="tenant-a", name="A", slug="a"))
        session.add(Tenant(id="tenant-b", name="B", slug="b"))
        session.add(BillingPlan(
            id="plan-1", code=ANNUAL_PLAN_CODE, display_name="年度基础套餐", is_active=True,
            amount_cents=9900, currency="CNY", billing_period_months=12,
            storage_quota_bytes=5 * GIB,
        ))
        session.add(Subscription(
            id="sub-a", tenant_id="tenant-a", plan_id="plan-1", status="active",
            starts_at=NOW - DAY, ends_at=NOW + timedelta(days=365),
            source="test", renewal_count=0, revision=1,
        ))
        session.commit()
    return factory


def _rollup(db, *, tenant_id="tenant-a", days_ago, used, anchor=None):
    db.add(TenantStorageDaily(
        tenant_id=tenant_id,
        # Service-level tests pass at=NOW to storage_trend, so their window
        # is anchored to the same frozen NOW. The HTTP endpoint under test
        # anchors its window to the REAL current UTC date (the router does
        # not accept `at`), so that test must seed relative to the real
        # date via `anchor=` — frozen seeds age out of the sliding window
        # as the calendar drifts and silently shrink measured_points.
        usage_date=((anchor or NOW.date()) - timedelta(days=days_ago)),
        used_bytes=used,
    ))


def test_projection_math() -> None:
    assert project_days_until_full(500, 1000, 100) == 5
    assert project_days_until_full(1000, 1000, 100) == 0
    assert project_days_until_full(1100, 1000, 100) == 0
    assert project_days_until_full(None, 1000, 100) is None
    assert project_days_until_full(500, 1000, None) is None
    assert project_days_until_full(500, 1000, -10) is None
    assert project_days_until_full(500, 0, 100) is None
    assert compute_daily_growth(__import__('app.services.storage_trend', fromlist=['TrendPoint']).TrendPoint("2026-01-01", 100), __import__('app.services.storage_trend', fromlist=['TrendPoint']).TrendPoint("2026-01-11", 200), 10) == 10


def test_trend_builds_series_and_estimate_from_rollup(db) -> None:
    with db() as session:
        for days_ago, used in ((29, 1 * GIB), (20, 2 * GIB), (10, 3 * GIB), (0, 4 * GIB)):
            _rollup(session, days_ago=days_ago, used=used)
        session.commit()
        trend = storage_trend(
            session, "tenant-a", range_days=30,
            quota_bytes=5 * GIB, used_bytes=4 * GIB, measured_at=NOW, at=NOW,
        )
        assert trend.measured_points == 4
        assert trend.series[0].date == (NOW.date() - timedelta(days=29)).isoformat()
        assert trend.series[-1].bytes == 4 * GIB
        # (4GiB - 1GiB) / 30 days ≈ 102.4 MiB/day → days until 5GiB ≈ 9.
        assert trend.avg_daily_growth_bytes is not None
        assert trend.days_until_full is not None and 1 <= trend.days_until_full <= 12
        assert trend.estimate_available is True
        assert trend.quota_bytes == 5 * GIB


def test_trend_needs_history_and_positive_growth(db) -> None:
    with db() as session:
        _rollup(session, days_ago=0, used=100)
        session.commit()
        one_point = storage_trend(session, "tenant-a", range_days=30, quota_bytes=1000, used_bytes=100, measured_at=NOW, at=NOW)
        assert one_point.measured_points == 1
        assert one_point.avg_daily_growth_bytes is None
        assert one_point.days_until_full is None

        # Flat / shrinking history never yields a depletion estimate.
        _rollup(session, days_ago=10, used=200)
        _rollup(session, days_ago=5, used=150)
        session.commit()
        shrinking = storage_trend(session, "tenant-a", range_days=30, quota_bytes=1000, used_bytes=100, measured_at=NOW, at=NOW)
        assert shrinking.avg_daily_growth_bytes is None
        assert shrinking.days_until_full is None


def test_trend_is_tenant_isolated(db) -> None:
    with db() as session:
        _rollup(session, tenant_id="tenant-a", days_ago=10, used=100)
        _rollup(session, tenant_id="tenant-a", days_ago=0, used=200)
        _rollup(session, tenant_id="tenant-b", days_ago=10, used=999)
        _rollup(session, tenant_id="tenant-b", days_ago=0, used=999)
        session.commit()
        trend_a = storage_trend(session, "tenant-a", range_days=30, quota_bytes=1000, used_bytes=200, measured_at=NOW, at=NOW)
        trend_b = storage_trend(session, "tenant-b", range_days=30, quota_bytes=1000, used_bytes=999, measured_at=NOW, at=NOW)
        assert trend_a.series[0].bytes == 100 and trend_a.series[-1].bytes == 200
        assert trend_b.series[0].bytes == 999
        assert trend_a.days_until_full is not None
        # Flat usage (no growth) never yields a depletion estimate.
        assert trend_b.days_until_full is None


# ---------------------------------------------------------------------------
# HTTP API contract
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_capacity_trend_endpoint_is_tenant_scoped(client, db) -> None:
    from app.auth import get_billing_viewer
    from app.db.session import get_db
    from app.main import app

    def _db_gen():
        with db() as session:
            yield session

    context = SimpleNamespace(tenant_id="tenant-a", session_scope="admin", role="owner")
    app.dependency_overrides[get_billing_viewer] = lambda: context
    app.dependency_overrides[get_db] = _db_gen
    try:
        # GH-151: the endpoint anchors its window to the real UTC today, so
        # seed with the same anchor; frozen-NOW seeds aged out of the window
        # after 2026-09-13 and dropped measured_points to 2 (< 3).
        endpoint_today = datetime.now(timezone.utc).date()
        with db() as session:
            _rollup(session, days_ago=20, used=GIB, anchor=endpoint_today)
            _rollup(session, days_ago=10, used=2 * GIB, anchor=endpoint_today)
            _rollup(session, days_ago=0, used=3 * GIB, anchor=endpoint_today)
            session.add(MediaFile(
                id=1, sdkfileid="sdk-1", archive_message_id=1, tenant_id="tenant-a",
                file_type="image", download_status="downloaded", file_size=3 * GIB,
                storage_backend="local", storage_ref="x",
            ))
            session.commit()
        response = client.get("/api/billing/capacity/trend?range=30")
        assert response.status_code == 200
        body = response.json()
        assert body["range_days"] == 30
        assert body["measured_points"] >= 3
        assert body["quota_bytes"] == 5 * GIB
        assert body["estimate_available"] is True
        assert body["days_until_full"] is not None
        assert body["series"][-1]["bytes"] == 3 * GIB
    finally:
        app.dependency_overrides.clear()
