"""RND-339 agent findings API isolation, cursor, and minimization tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_reachability_audit import _make_session

NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
_SCHEMA = """
CREATE TABLE reachability_findings (
 id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL,
 archive_message_id INTEGER NOT NULL, reason_code TEXT NOT NULL, status TEXT NOT NULL,
 first_seen DATETIME NOT NULL, last_seen DATETIME NOT NULL, resolved_at DATETIME,
 occurrence_count INTEGER NOT NULL, first_run_id INTEGER NOT NULL, last_run_id INTEGER NOT NULL,
 algorithm_version TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(tenant_id, archive_message_id, reason_code, algorithm_version)
);
"""
_SENTINELS = (
    "SENTINEL_MESSAGE_BODY", "SENTINEL_STRUCTURED_CONTENT", "SENTINEL_PASSWORD",
    "SENTINEL_PASSWORD_HASH", "SENTINEL_TOKEN", "SENTINEL_SECRET", "SENTINEL_SIGNED_URL",
    "SENTINEL_STORAGE_KEY", "/sentinel/fs/path", "SENTINEL_SEARCH_TEXT", "SENTINEL_TRACEBACK",
    "SENTINEL_SENDER", "SENTINEL_RECIPIENT", "SENTINEL_ROOM", "SENTINEL_RAW_MSGID",
)


@pytest.fixture()
def db() -> Session:
    session = _make_session()
    for statement in _SCHEMA.strip().split(";"):
        if statement.strip():
            session.execute(text(statement))
    session.commit()
    yield session
    session.close()


def _seed(db: Session, *, tenant="tenant-a", public_id="finding-a", status="active", seen=NOW, reason="sender_null_or_unresolvable"):
    db.execute(
        text("""INSERT INTO reachability_findings
        (public_id,tenant_id,archive_message_id,reason_code,status,first_seen,last_seen,resolved_at,
         occurrence_count,first_run_id,last_run_id,algorithm_version)
        VALUES (:public,:tenant,:message,:reason,:status,:first,:last,:resolved,1,1,1,'reachability-v1')"""),
        {"public": public_id, "tenant": tenant, "message": sum(map(ord, public_id)), "reason": reason, "status": status,
         "first": seen, "last": seen, "resolved": seen if status == "resolved" else None},
    )
    db.commit()


def _assert_no_sentinels(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert not any(s in str(key) for s in _SENTINELS)
            _assert_no_sentinels(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_sentinels(item)
    else:
        assert not any(s in str(value) for s in _SENTINELS)


def _client(db: Session, tenant="tenant-a"):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), tenant)
    return app


def test_auth_default_active_filters_and_safe_allowlist(db: Session) -> None:
    from app.db.session import get_db
    from app.main import app as base_app
    _seed(db, public_id="active-a")
    _seed(db, public_id="resolved-a", status="resolved")
    try:
        def override_db():
            yield db
        base_app.dependency_overrides[get_db] = override_db
        with TestClient(base_app, raise_server_exceptions=False) as client:
            assert client.get("/api/admin/reachability-findings").status_code == 401
        app = _client(db)
        with TestClient(app) as client:
            body = client.get("/api/admin/reachability-findings").json()
            assert [item["public_finding_id"] for item in body["items"]] == ["active-a"]
            assert set(body["items"][0]) == {
                "public_finding_id", "reason", "status", "first_seen_at", "last_seen_at",
                "resolved_at", "occurrence_count", "algorithm_version", "remediation_code",
            }
            _assert_no_sentinels(body)
            assert client.get("/api/admin/reachability-findings?status=resolved").json()["items"][0]["public_finding_id"] == "resolved-a"
            assert client.get("/api/admin/reachability-findings?reason=not-a-reason").status_code == 422
            assert client.get("/api/admin/reachability-findings?status=bad").status_code == 422
            assert client.get("/api/admin/reachability-findings?limit=101").status_code == 422
            assert client.get("/api/admin/reachability-findings?tenant_id=tenant-b").status_code == 422
    finally:
        base_app.dependency_overrides.clear()


def test_cursor_is_opaque_filter_bound_stable_and_tenant_scoped(db: Session) -> None:
    from app.main import app
    for public in ("finding-c", "finding-b", "finding-a"):
        _seed(db, public_id=public)
    _seed(db, tenant="tenant-b", public_id="tenant-b-finding")
    app = _client(db)
    try:
        with TestClient(app) as client:
            first = client.get("/api/admin/reachability-findings?limit=2").json()
            cursor = first["next_cursor"]
            assert cursor and "tenant-a" not in cursor and "archive_message_id" not in cursor
            # A concurrent newest insertion remains before the cursor and
            # cannot duplicate or displace the original second page.
            _seed(db, public_id="finding-z", seen=datetime(2026, 8, 4, tzinfo=timezone.utc))
            second = client.get(f"/api/admin/reachability-findings?limit=2&cursor={cursor}").json()
            ids = [item["public_finding_id"] for item in first["items"] + second["items"]]
            assert len(ids) == len(set(ids))
            assert {"finding-a", "finding-b", "finding-c"}.issubset(ids)
            assert "tenant-b-finding" not in ids
            assert client.get(f"/api/admin/reachability-findings?status=resolved&cursor={cursor}").status_code == 422
            assert client.get("/api/admin/reachability-findings?cursor=not-a-cursor").status_code == 422
        # Tenant B cannot replay A's encrypted cursor: filter binding fails closed.
        app.dependency_overrides.clear()
        app = _client(db, "tenant-b")
        with TestClient(app) as client:
            assert client.get(f"/api/admin/reachability-findings?cursor={cursor}").status_code == 422
    finally:
        app.dependency_overrides.clear()
