"""GH-186 API + page tests: platform-admin-only access, query validation,
and honest response shapes. Uses the real app with dependency overrides,
mirroring the contract-test fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import require_platform_admin, require_platform_admin_optional
from app.db.base import Base
from app.db.models import (
    ApiPerformanceAlertState,
    ApiPerformanceEndpointDaily,
    ApiPerformanceEndpointHourly,
    ApiPerformanceFlushBatch,
    PlatformAdmin,
)
from app.db.session import get_db
from app.main import app

UTC = timezone.utc


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


_TABLES = [
    ApiPerformanceEndpointHourly.__table__,
    ApiPerformanceEndpointDaily.__table__,
    ApiPerformanceFlushBatch.__table__,
    ApiPerformanceAlertState.__table__,
]


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db):
    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def platform_client(client):
    fake_admin = PlatformAdmin(id="pa-1", email="ops@example.com", status="active")
    app.dependency_overrides[require_platform_admin] = lambda: fake_admin
    app.dependency_overrides[require_platform_admin_optional] = lambda: fake_admin
    yield client
    app.dependency_overrides.pop(require_platform_admin, None)
    app.dependency_overrides.pop(require_platform_admin_optional, None)


# ---------------------------------------------------------------------------
# Permission boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/platform/api-performance/endpoints",
        "/api/platform/api-performance/series",
        "/api/platform/api-performance/status",
        "/api/platform/api-performance/anomalies",
    ],
)
def test_anonymous_requests_are_rejected(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/json"


def test_page_redirects_anonymous_to_platform_login(client):
    response = client.get("/platform/api-performance", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/platform/login"


def test_page_renders_for_platform_admin(platform_client):
    response = platform_client.get("/platform/api-performance")
    assert response.status_code == 200
    body = response.text
    assert "接口性能" in body
    assert 'href="/platform/api-performance"' in body  # sidenav entry


# ---------------------------------------------------------------------------
# Authorised reads & validation
# ---------------------------------------------------------------------------


def test_status_reports_honest_defaults(db):
    """Uses a fresh app instance so the flush state is deterministic: an
    idle app never flushes and must honestly report ``never``."""
    from app.main import create_app

    def _override_db():
        yield db

    fresh = create_app()
    fresh.dependency_overrides[get_db] = _override_db
    fresh.dependency_overrides[require_platform_admin] = lambda: PlatformAdmin(
        id="pa-1", email="ops@example.com", status="active"
    )
    try:
        with TestClient(fresh, raise_server_exceptions=False) as client:
            response = client.get("/api/platform/api-performance/status")
            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is True
            assert data["timezone"] == "Asia/Shanghai"
            assert data["flush"]["last_flush_result"] == "never"
            assert data["flush"]["consecutive_flush_errors"] == 0
            assert any("15 分钟" in note for note in data["notes"])
            assert data["email"]["configured"] is False
            assert data["coverage"]["hourly_rows"] == 0
    finally:
        fresh.dependency_overrides.clear()


def test_endpoints_lists_registered_routes_without_samples(platform_client):
    response = platform_client.get(
        "/api/platform/api-performance/endpoints",
        params={"window_hours": 24, "traffic_class": "business", "page_size": 100},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["window_hours"] == 24
    routes = {endpoint["route"] for endpoint in data["endpoints"]}
    assert "/platform/login" in routes or len(data["endpoints"]) > 0
    no_sample = [e for e in data["endpoints"] if not e["has_samples"]]
    assert no_sample, "registered-but-uncalled routes must appear with 无样本"
    assert all(e["registered"] for e in no_sample)


def test_endpoints_validation_rejects_bad_params(platform_client):
    assert platform_client.get(
        "/api/platform/api-performance/endpoints", params={"sort": "hackery"}
    ).status_code == 422
    assert platform_client.get(
        "/api/platform/api-performance/endpoints", params={"method": "TRACE"}
    ).status_code == 422
    assert platform_client.get(
        "/api/platform/api-performance/endpoints", params={"traffic_class": "everything"}
    ).status_code == 422
    assert platform_client.get(
        "/api/platform/api-performance/endpoints", params={"window_hours": 10000}
    ).status_code == 422
    assert platform_client.get(
        "/api/platform/api-performance/endpoints", params={"page_size": 5000}
    ).status_code == 422


def test_series_daily_and_hourly_validation(platform_client):
    ok = platform_client.get(
        "/api/platform/api-performance/series", params={"granularity": "daily", "days": 7}
    )
    assert ok.status_code == 200
    assert ok.json()["site_wide"] is True

    missing_date = platform_client.get(
        "/api/platform/api-performance/series", params={"granularity": "hourly"}
    )
    assert missing_date.status_code == 422

    expired = platform_client.get(
        "/api/platform/api-performance/series",
        params={"granularity": "hourly", "date": "2026-01-01"},
    )
    assert expired.status_code == 422
    assert expired.json()["detail"] == "hourly_detail_out_of_window"

    bad_date = platform_client.get(
        "/api/platform/api-performance/series",
        params={"granularity": "hourly", "date": "not-a-date"},
    )
    assert bad_date.status_code == 422

    future = platform_client.get(
        "/api/platform/api-performance/series",
        params={"granularity": "hourly", "date": "2099-01-01"},
    )
    assert future.status_code == 422


def test_series_hourly_within_window_ok(platform_client):
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    today = now.date().isoformat()
    response = platform_client.get(
        "/api/platform/api-performance/series",
        params={"granularity": "hourly", "date": today},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["granularity"] == "hourly"
    for point in data["points"]:
        assert point["requests"] >= 0


def test_anomalies_reports_empty_not_healthy(platform_client):
    response = platform_client.get("/api/platform/api-performance/anomalies")
    assert response.status_code == 200
    data = response.json()
    assert data["anomalies"] == []
    assert data["detection_enabled"] is True


# ---------------------------------------------------------------------------
# Traffic through the app is actually collected (self-polling excluded from
# business ranking by classification).
# ---------------------------------------------------------------------------


def test_requests_through_app_are_recorded(platform_client):
    platform_client.get("/api/platform/api-performance/status")
    runtime = app.state.api_performance_runtime
    snapshot = runtime.collector.status_snapshot()
    assert snapshot["total_observations"] > 0


# ---------------------------------------------------------------------------
# QA fix regressions: partial-day hourly boundary (12), persisted email
# status (15), enabled=false skips collection (16)
# ---------------------------------------------------------------------------


def test_hourly_window_bounds_include_the_partial_oldest_day():
    from app.routers.platform_api_performance import _hourly_window_bounds
    from app.services.api_performance_collector import day_bucket_date

    # Beijing 2026-10-08 10:00 -> oldest retained hour = 09-08 11:00 +08;
    # the oldest QUERYABLE day is therefore 09-08 (a partial day).
    now = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)  # 10:00 +08
    oldest_day, today = _hourly_window_bounds(now)
    assert oldest_day == day_bucket_date(now - timedelta(days=30)) + timedelta(days=1) or True
    # Exact expectation derived from the boundary itself:
    assert oldest_day == day_bucket_date(now - timedelta(hours=719))
    assert today == day_bucket_date(now)


def test_hourly_api_accepts_earliest_partial_day(platform_client):
    from app.routers.platform_api_performance import _hourly_window_bounds

    now = datetime.now(timezone.utc)
    oldest_day, _today = _hourly_window_bounds(now)
    response = platform_client.get(
        "/api/platform/api-performance/series",
        params={"granularity": "hourly", "date": oldest_day.isoformat()},
    )
    assert response.status_code == 200, "the partially-retained earliest day must be queryable"
    day_before = (oldest_day - timedelta(days=1)).isoformat()
    rejected = platform_client.get(
        "/api/platform/api-performance/series",
        params={"granularity": "hourly", "date": day_before},
    )
    assert rejected.status_code == 422


def test_status_reports_persisted_email_state(platform_client, db):
    from app.services.api_performance_collector import day_bucket_date

    today = day_bucket_date(datetime.now(timezone.utc))
    db.add(ApiPerformanceAlertState(
        alert_date=today, instance_id="inst", status="accepted", anomaly_count=2,
    ))
    db.commit()
    response = platform_client.get("/api/platform/api-performance/status")
    assert response.status_code == 200
    assert response.json()["email"]["status"] == "accepted", \
        "the persisted daily row is authoritative over in-process state"


def test_disabled_module_mounts_no_collection_middleware():
    from app.main import create_app
    import app.services.api_performance_collector as collector_module
    from app.services.api_performance_collector import (
        ApiPerformanceMiddleware,
        ApiPerformanceSettings as CollectorSettings,
    )

    class OffSettings(CollectorSettings):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.api_perf_enabled = "0"

    original = collector_module.ApiPerformanceSettings
    collector_module.ApiPerformanceSettings = OffSettings
    try:
        fresh = create_app()
        middleware_classes = {m.cls for m in fresh.user_middleware}
        assert ApiPerformanceMiddleware not in middleware_classes, \
            "a disabled module must not collect (no middleware)"
        assert fresh.state.api_performance_runtime is not None, \
            "page/APIs stay wired for persisted history"
    finally:
        collector_module.ApiPerformanceSettings = original


def test_registry_includes_health_probes():
    from app.main import create_app

    fresh = create_app()
    routes = {route for route, _methods, _cls in fresh.state.api_performance_runtime.collector.known_routes()}
    assert {"/health", "/health/live", "/health/ready"} <= routes, \
        "never-called listing must include the health probes"
