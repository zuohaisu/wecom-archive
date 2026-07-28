"""Coverage for RND-294's fail-safe audit write hook."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.db.models import AdminSession, AdminUser, AuditLog, Tenant


_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


def test_audit_vocabulary_uses_canonical_values() -> None:
    assert AuditAction.LOGIN == "auth.login"
    assert AuditAction.LOGOUT == "auth.logout"
    assert AuditAction.PASSWORD_RESET_COMPLETED == "auth.password_reset_completed"
    assert AuditObjectType.USER == "admin_user"
    assert AuditObjectType.SESSION == "admin_session"
    assert AuditObjectType.TENANT_CONFIG == "tenant_config"


def test_write_audit_swallows_savepoint_failure() -> None:
    class BrokenSession:
        def begin_nested(self):
            raise RuntimeError("audit sink unavailable")

    write_audit(
        BrokenSession(),
        tenant_id="tenant-1",
        action=AuditAction.LOGIN,
        object_type=AuditObjectType.USER,
    )


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_write_audit_persists_structured_detail() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    with Session(engine) as db:
        try:
            db.add(Tenant(id=tenant_id, name="RND-294 test", slug=f"rnd294-{tenant_id}"))
            db.add(
                AdminUser(
                    id=user_id,
                    tenant_id=tenant_id,
                    wecom_user_id=f"rnd294-{user_id}",
                    name="RND-294 audit actor",
                    last_login_at=datetime.now(timezone.utc),
                )
            )
            db.flush()

            write_audit(
                db,
                tenant_id=tenant_id,
                admin_user_id=user_id,
                action=AuditAction.LOGIN,
                object_type=AuditObjectType.USER,
                object_id=user_id,
                detail={"ip": "127.0.0.1"},
            )
            db.commit()

            row = db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id).one()
            assert row.admin_user_id == user_id
            assert row.action == AuditAction.LOGIN
            assert row.object_type == AuditObjectType.USER
            assert row.object_id == user_id
            assert row.detail == {"ip": "127.0.0.1"}
            assert row.created_at is not None
        finally:
            db.rollback()
            db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id).delete()
            db.query(AdminUser).filter(AdminUser.id == user_id).delete()
            db.query(Tenant).filter(Tenant.id == tenant_id).delete()
            db.commit()


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_logout_writes_audit_row() -> None:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    engine = create_engine(os.environ["DATABASE_URL"])
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    with Session(engine) as db:
        old_overrides = app.dependency_overrides.copy()

        def override_db():
            yield db

        try:
            db.add(Tenant(id=tenant_id, name="RND-294 logout", slug=f"rnd294-{tenant_id}"))
            db.add(
                AdminUser(
                    id=user_id,
                    tenant_id=tenant_id,
                    wecom_user_id=f"rnd294-{user_id}",
                    name="RND-294 logout actor",
                    last_login_at=datetime.now(timezone.utc),
                )
            )
            db.add(
                AdminSession(
                    id=session_id,
                    tenant_id=tenant_id,
                    admin_user_id=user_id,
                    wecom_user_id=f"rnd294-{user_id}",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            )
            db.commit()

            app.dependency_overrides[get_db] = override_db
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/auth/logout", cookies={"session_id": session_id})

            assert response.status_code == 200
            row = (
                db.query(AuditLog)
                .filter(AuditLog.tenant_id == tenant_id, AuditLog.action == AuditAction.LOGOUT)
                .one()
            )
            assert row.admin_user_id == user_id
            assert row.object_type == AuditObjectType.SESSION
            assert row.object_id == session_id
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(old_overrides)
            db.rollback()
            db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id).delete()
            db.query(AdminSession).filter(AdminSession.id == session_id).delete()
            db.query(AdminUser).filter(AdminUser.id == user_id).delete()
            db.query(Tenant).filter(Tenant.id == tenant_id).delete()
            db.commit()
