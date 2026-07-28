"""Coverage for RND-295's read-only, tenant-scoped audit-log API."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import AdminSession, AdminUser, AuditLog, Tenant


_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


def _no_session_db() -> Generator:
    """A `get_db` override for authentication-only paths without a database."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    yield db


def test_audit_log_route_is_get_only_and_unauthenticated_is_denied() -> None:
    from app.db.session import get_db
    from app.main import app

    old_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = _no_session_db
    try:
        route = next(route for route in app.routes if route.path == "/api/admin/audit-logs")
        assert route.methods == {"GET"}
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/api/admin/audit-logs").status_code == 401
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_audit_log_list_filters_pages_and_never_mutates_rows() -> None:
    from app.db.session import get_db
    from app.main import app

    engine = create_engine(os.environ["DATABASE_URL"])
    tenant_id = str(uuid.uuid4())
    other_tenant_id = str(uuid.uuid4())
    actor_id = str(uuid.uuid4())
    readonly_id = str(uuid.uuid4())
    other_actor_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    readonly_session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).replace(microsecond=0)

    with Session(engine) as db:
        old_overrides = app.dependency_overrides.copy()

        def override_db() -> Generator:
            yield db

        try:
            db.add_all(
                [
                    Tenant(id=tenant_id, name="RND-295 tenant", slug=f"rnd295-{tenant_id}"),
                    Tenant(
                        id=other_tenant_id,
                        name="RND-295 other tenant",
                        slug=f"rnd295-other-{other_tenant_id}",
                    ),
                ]
            )
            db.flush()
            db.add_all(
                [
                    AdminUser(
                        id=actor_id,
                        tenant_id=tenant_id,
                        wecom_user_id=f"rnd295-{actor_id}",
                        name="RND-295 Export Operator",
                        email="audit-operator@example.test",
                        role="admin",
                    ),
                    AdminUser(
                        id=readonly_id,
                        tenant_id=tenant_id,
                        wecom_user_id=f"rnd295-readonly-{readonly_id}",
                        name="RND-295 Readonly Auditor",
                        role="readonlyaudit",
                    ),
                    AdminUser(
                        id=other_actor_id,
                        tenant_id=other_tenant_id,
                        wecom_user_id=f"rnd295-other-{other_actor_id}",
                        name="RND-295 Other Operator",
                        role="admin",
                    ),
                ]
            )
            db.flush()
            db.add_all(
                [
                    AdminSession(
                        id=session_id,
                        tenant_id=tenant_id,
                        admin_user_id=actor_id,
                        wecom_user_id=f"rnd295-{actor_id}",
                        expires_at=now + timedelta(hours=1),
                    ),
                    AdminSession(
                        id=readonly_session_id,
                        tenant_id=tenant_id,
                        admin_user_id=readonly_id,
                        wecom_user_id=f"rnd295-readonly-{readonly_id}",
                        expires_at=now + timedelta(hours=1),
                    ),
                ]
            )
            db.add_all(
                [
                    AuditLog(
                        id=str(uuid.uuid4()),
                        tenant_id=tenant_id,
                        admin_user_id=None,
                        action="login",
                        object_type="session",
                        object_id="system-login-marker",
                        detail={"source": "scheduler"},
                        created_at=now - timedelta(hours=4),
                    ),
                    AuditLog(
                        id=str(uuid.uuid4()),
                        tenant_id=tenant_id,
                        admin_user_id=actor_id,
                        action="export",
                        object_type="report",
                        object_id="export-target-123",
                        detail={"format": "csv"},
                        created_at=now - timedelta(hours=3),
                    ),
                    AuditLog(
                        id=str(uuid.uuid4()),
                        tenant_id=tenant_id,
                        admin_user_id=actor_id,
                        action="search",
                        object_type="audit_log",
                        object_id="search-target-456",
                        detail={"query_kind": "metadata"},
                        created_at=now - timedelta(hours=2),
                    ),
                    AuditLog(
                        id=str(uuid.uuid4()),
                        tenant_id=tenant_id,
                        admin_user_id=actor_id,
                        action="config.changed",
                        object_type="tenant_config",
                        object_id="config-target-789",
                        detail={"field": "retention_days"},
                        created_at=now - timedelta(hours=1),
                    ),
                    AuditLog(
                        id=str(uuid.uuid4()),
                        tenant_id=other_tenant_id,
                        admin_user_id=other_actor_id,
                        action="export",
                        object_type="report",
                        object_id="other-tenant-target",
                        detail={"format": "json"},
                        created_at=now,
                    ),
                ]
            )
            db.commit()
            app.dependency_overrides[get_db] = override_db

            with TestClient(app, raise_server_exceptions=False) as client:
                cookies = {"session_id": session_id}
                response = client.get("/api/admin/audit-logs", cookies=cookies)
                assert response.status_code == 200
                payload = response.json()
                assert payload["total"] == 4
                assert len(payload["items"]) == 4
                assert payload["has_more"] is False
                assert [item["created_at"] for item in payload["items"]] == sorted(
                    (item["created_at"] for item in payload["items"]), reverse=True
                )
                assert payload["items"][1]["actor_name"] == "RND-295 Export Operator"

                assert client.get("/api/admin/audit-logs?action=export", cookies=cookies).json()[
                    "total"
                ] == 1
                assert client.get("/api/admin/audit-logs?operator=system", cookies=cookies).json()[
                    "items"
                ][0]["admin_user_id"] is None
                assert client.get(
                    f"/api/admin/audit-logs?operator={actor_id}", cookies=cookies
                ).json()["total"] == 3
                assert client.get(
                    "/api/admin/audit-logs?q=target-123", cookies=cookies
                ).json()["total"] == 1
                assert client.get(
                    "/api/admin/audit-logs?q=Export%20Operator", cookies=cookies
                ).json()["total"] == 3
                time_window = client.get(
                    "/api/admin/audit-logs",
                    params={
                        "from": (now - timedelta(hours=3)).isoformat(),
                        "to": (now - timedelta(hours=2)).isoformat(),
                    },
                    cookies=cookies,
                ).json()
                assert time_window["total"] == 2

                first_page = client.get(
                    "/api/admin/audit-logs?limit=2&offset=0", cookies=cookies
                ).json()
                second_page = client.get(
                    "/api/admin/audit-logs?limit=2&offset=2", cookies=cookies
                ).json()
                assert first_page["has_more"] is True
                assert len(first_page["items"]) == 2
                assert second_page["has_more"] is False
                assert len(second_page["items"]) == 2
                assert {item["id"] for item in first_page["items"]}.isdisjoint(
                    item["id"] for item in second_page["items"]
                )

                readonly_response = client.get(
                    "/api/admin/audit-logs", cookies={"session_id": readonly_session_id}
                )
                assert readonly_response.status_code == 200

                serialized_detail = json.dumps(payload["items"])
                for forbidden in ("decrypted_payload", "msgid", "message_body"):
                    assert forbidden not in serialized_detail

            assert db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id).count() == 4
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(old_overrides)
            db.rollback()
            db.query(AuditLog).filter(
                AuditLog.tenant_id.in_((tenant_id, other_tenant_id))
            ).delete(synchronize_session=False)
            db.query(AdminSession).filter(
                AdminSession.id.in_((session_id, readonly_session_id))
            ).delete(synchronize_session=False)
            db.query(AdminUser).filter(
                AdminUser.id.in_((actor_id, readonly_id, other_actor_id))
            ).delete(synchronize_session=False)
            db.query(Tenant).filter(Tenant.id.in_((tenant_id, other_tenant_id))).delete(
                synchronize_session=False
            )
            db.commit()
