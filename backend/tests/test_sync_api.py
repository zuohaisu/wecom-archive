"""RND-211 sync-status and manual-worker trigger contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from app.db.base import Base
from app.db.models import SyncState, Tenant, TenantWecomConfig
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, TenantWecomConfig.__table__, SyncState.__table__],
    )
    session = Session(engine)
    session.add(Tenant(id="tenant-a", name="Tenant A", slug="tenant-a", is_active=True))
    session.add(Tenant(id="tenant-b", name="Tenant B", slug="tenant-b", is_active=True))
    session.add(
        TenantWecomConfig(
            id="config-a",
            tenant_id="tenant-a",
            corp_id="corp-a",
            agent_id="1000001",
            app_secret="not-a-real-secret",
            is_active=True,
        )
    )
    session.add(
        TenantWecomConfig(
            id="config-b",
            tenant_id="tenant-b",
            corp_id="corp-b",
            agent_id="1000002",
            app_secret="not-a-real-secret",
            is_active=True,
        )
    )
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def client(db: Session):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    def _override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_sync_endpoints_require_authentication() -> None:
    from app.db.session import get_db
    from app.main import app

    def _no_session_db():
        mock = MagicMock()
        mock.query.return_value.filter.return_value.first.return_value = None
        yield mock

    app.dependency_overrides[get_db] = _no_session_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            assert test_client.get("/api/admin/sync-status").status_code == 401
            assert test_client.post("/api/admin/sync-now").status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_sync_status_is_authenticated_and_tenant_scoped(client: TestClient, db: Session) -> None:
    db.add(
        SyncState(
            tenant_id="tenant-a",
            corp_id="corp-a",
            last_seq=12,
            status="idle",
            started_at=datetime(2026, 7, 28, 1, 2, 3, tzinfo=timezone.utc),
            seq_version=4,
        )
    )
    db.add(
        SyncState(
            tenant_id="tenant-b",
            corp_id="corp-b",
            last_seq=999,
            status="error",
            error_message="sync_failed",
            seq_version=8,
        )
    )
    db.commit()

    response = client.get("/api/admin/sync-status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "idle",
        "lastSeq": 12,
        "startTime": "2026-07-28T01:02:03",
        "errorMessage": None,
        "seqVersion": 4,
    }


def test_sync_now_marks_state_and_queues_the_real_worker(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import sync

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(sync, "_run_archive_worker", lambda tenant_id, corp_id: calls.append((tenant_id, corp_id)))

    response = client.post("/api/admin/sync-now")

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "message": "started",
        "retryAfterSeconds": None,
    }
    assert calls == [("tenant-a", "corp-a")]
    row = db.query(SyncState).filter_by(tenant_id="tenant-a", corp_id="corp-a").one()
    assert row.status == "syncing"
    assert row.started_at is not None

    duplicate = client.post("/api/admin/sync-now")
    assert duplicate.status_code == 202
    assert duplicate.json()["message"] == "already_running"
    assert calls == [("tenant-a", "corp-a")]


def test_sync_now_enforces_thirty_second_cooldown(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import sync

    monkeypatch.setattr(sync, "_run_archive_worker", lambda *_: None)
    db.add(
        SyncState(
            tenant_id="tenant-a",
            corp_id="corp-a",
            last_seq=0,
            status="idle",
            started_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            seq_version=2,
        )
    )
    db.commit()

    response = client.post("/api/admin/sync-now")

    assert response.status_code == 202
    assert response.json()["accepted"] is False
    assert response.json()["message"] == "rate_limited"
    assert 1 <= response.json()["retryAfterSeconds"] <= 25


def test_manual_worker_uses_the_shared_archive_worker_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import sync

    failures: list[tuple[str, str]] = []
    monkeypatch.setattr(sync, "run_archive_worker_once", lambda **_kwargs: False)
    monkeypatch.setattr(sync, "_mark_worker_failed", lambda tenant_id, corp_id: failures.append((tenant_id, corp_id)))

    sync._run_archive_worker("tenant-sentinel", "corp-sentinel")

    assert failures == [("tenant-sentinel", "corp-sentinel")]
