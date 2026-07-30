"""RND-305 platform-admin authentication and audited tenant-scope coverage."""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi.security import HTTPBasicCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import (
    PLATFORM_TENANT_ACCESS_ACTION,
    PLATFORM_TENANT_OBJECT_TYPE,
    require_platform_admin,
    require_platform_tenant_scope,
)
from app.db.models import AuditLog, PlatformAdmin, Tenant
from app.db.session import get_db


_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


def _platform_admin() -> PlatformAdmin:
    return PlatformAdmin(
        id="platform-admin-1",
        email="platform-admin@example.test",
        password_hash="not-used-by-mocked-verifier",
        status="active",
    )


def test_require_platform_admin_accepts_valid_basic_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _platform_admin()
    verifier = MagicMock(return_value=admin)
    monkeypatch.setattr("app.auth.verify_platform_admin", verifier)

    db = MagicMock()
    result = require_platform_admin(
        credentials=HTTPBasicCredentials(username=admin.email, password="fixed-test-password"),
        db=db,
    )

    assert result is admin
    verifier.assert_called_once_with(db, "platform-admin@example.test", "fixed-test-password")


@pytest.mark.parametrize(
    "credentials",
    [None, HTTPBasicCredentials(username="platform-admin@example.test", password="wrong")],
)
def test_require_platform_admin_rejects_missing_or_invalid_credentials(
    monkeypatch: pytest.MonkeyPatch, credentials: HTTPBasicCredentials | None
) -> None:
    verifier = MagicMock(return_value=None)
    monkeypatch.setattr("app.auth.verify_platform_admin", verifier)

    with pytest.raises(HTTPException) as raised:
        require_platform_admin(credentials=credentials, db=MagicMock())

    assert raised.value.status_code == 401
    if credentials is None:
        verifier.assert_not_called()
    else:
        verifier.assert_called_once()


@pytest.mark.parametrize("tenant_role", ["admin", "owner"])
def test_tenant_admin_session_cookie_cannot_satisfy_platform_admin_dependency(
    tenant_role: str,
) -> None:
    """No tenant admin role is considered by the Basic-only resolver."""
    app = FastAPI()

    @app.get("/scope-check")
    def scope_check(_: PlatformAdmin = Depends(require_platform_admin)) -> dict[str, bool]:
        return {"allowed": True}

    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        with TestClient(app) as client:
            # This represents a tenant admin session. It must not be a
            # credential for the separate, tenant-less PlatformAdmin identity.
            client.cookies.set("session_id", f"tenant-{tenant_role}-session")
            response = client.get("/scope-check")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_platform_tenant_scope_uses_explicit_tenant_and_writes_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _platform_admin()
    audit_writer = MagicMock()
    monkeypatch.setattr("app.auth.write_audit", audit_writer)
    db = MagicMock()

    scope = require_platform_tenant_scope(
        tenant_id="target-tenant-9", platform_admin=admin, db=db
    )

    assert scope.platform_admin is admin
    assert scope.tenant_id == "target-tenant-9"
    audit_writer.assert_called_once_with(
        db,
        tenant_id="target-tenant-9",
        action=PLATFORM_TENANT_ACCESS_ACTION,
        object_type=PLATFORM_TENANT_OBJECT_TYPE,
        object_id="target-tenant-9",
        detail={
            "platform_admin_id": admin.id,
            "platform_admin_email": admin.email,
        },
    )


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_platform_tenant_scope_persists_auditlog_with_platform_identity() -> None:
    """The scope primitive records the isolated actor in AuditLog.detail."""
    engine = create_engine(os.environ["DATABASE_URL"])
    tenant_id = str(uuid.uuid4())
    admin_id = str(uuid.uuid4())
    with Session(engine) as db:
        try:
            db.add(Tenant(id=tenant_id, name="RND-305 tenant", slug=f"rnd305-{tenant_id}"))
            admin = PlatformAdmin(
                id=admin_id,
                email=f"rnd305-{admin_id}@example.test",
                password_hash="test-only-hash",
                status="active",
            )
            db.add(admin)
            db.flush()

            require_platform_tenant_scope(tenant_id=tenant_id, platform_admin=admin, db=db)
            db.commit()

            row = db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id).one()
            assert row.action == PLATFORM_TENANT_ACCESS_ACTION
            assert row.object_type == PLATFORM_TENANT_OBJECT_TYPE
            assert row.object_id == tenant_id
            assert row.admin_user_id is None
            assert row.detail == {
                "platform_admin_id": admin_id,
                "platform_admin_email": admin.email,
            }
        finally:
            db.rollback()
            db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id).delete()
            db.query(PlatformAdmin).filter(PlatformAdmin.id == admin_id).delete()
            db.query(Tenant).filter(Tenant.id == tenant_id).delete()
            db.commit()
    engine.dispose()
