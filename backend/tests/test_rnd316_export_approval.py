"""Offline coverage for the RND-316 export approval gate."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db.base import Base
from app.db.models import AdminUser, ExportApprovalToken, Tenant
from app.export_approval import (
    ExportNotApprovedError,
    issue_export_approval,
    require_export_approval,
)


PARAMS = {"format": "xlsx", "ids": ["message-1"], "filters": {"days": 7}}


def _session_with_users() -> tuple[Session, AdminUser, AdminUser]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, AdminUser.__table__, ExportApprovalToken.__table__],
    )
    db = Session(engine)
    tenant_a = Tenant(id="tenant-a", slug="tenant-a", name="Tenant A")
    tenant_b = Tenant(id="tenant-b", slug="tenant-b", name="Tenant B")
    user_a = AdminUser(
        id=str(uuid.uuid4()), tenant_id=tenant_a.id, wecom_user_id="user-a",
        name="User A", password_hash=hash_password("correct-password"), role="admin",
        status="active",
    )
    user_b = AdminUser(
        id=str(uuid.uuid4()), tenant_id=tenant_b.id, wecom_user_id="user-b",
        name="User B", password_hash=hash_password("correct-password"), role="admin",
        status="active",
    )
    db.add_all([tenant_a, tenant_b, user_a, user_b])
    db.commit()
    return db, user_a, user_b


def _issue(db: Session, user: AdminUser) -> str:
    with patch("app.export_approval.write_audit"):
        raw, _ = issue_export_approval(
            db=db, admin_user_id=user.id, tenant_id=user.tenant_id, params=PARAMS
        )
    db.commit()
    return raw


def test_issue_stores_only_hash_and_records_non_sensitive_context() -> None:
    db, user, _ = _session_with_users()
    try:
        with patch("app.export_approval.write_audit") as audit:
            raw, expires_at = issue_export_approval(
                db=db, admin_user_id=user.id, tenant_id=user.tenant_id, params=PARAMS
            )
        row = db.query(ExportApprovalToken).one()
        assert row.token != raw and len(row.token) == 64
        assert expires_at > datetime.now(timezone.utc)
        detail = audit.call_args.kwargs["detail"]
        assert set(detail) == {"params_hash", "expires_at"}
        assert not {"content", "payload", "body", "decrypted"} & set(detail)
    finally:
        db.close()


@pytest.mark.parametrize(
    "case",
    ["missing", "wrong", "expired", "used", "tenant", "admin", "params"],
)
def test_require_rejects_every_invalid_approval_case(case: str) -> None:
    db, user_a, user_b = _session_with_users()
    try:
        raw = _issue(db, user_a)
        row = db.query(ExportApprovalToken).one()
        token, params, admin_id, tenant_id = raw, PARAMS, user_a.id, user_a.tenant_id
        if case == "missing":
            token = None
        elif case == "wrong":
            token = "wrong-token"
        elif case == "expired":
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        elif case == "used":
            row.used = True
            db.commit()
        elif case == "tenant":
            tenant_id = user_b.tenant_id
        elif case == "admin":
            admin_id = user_b.id
        elif case == "params":
            params = {"format": "pdf", "ids": ["message-1"]}

        with patch("app.export_approval.write_audit"), pytest.raises(ExportNotApprovedError):
            require_export_approval(
                db=db, token=token, params=params, admin_user_id=admin_id, tenant_id=tenant_id
            )
    finally:
        db.close()


def test_require_consumes_valid_token_once() -> None:
    db, user, _ = _session_with_users()
    try:
        raw = _issue(db, user)
        with patch("app.export_approval.write_audit") as audit:
            require_export_approval(
                db=db, token=raw, params=PARAMS, admin_user_id=user.id, tenant_id=user.tenant_id
            )
        assert db.query(ExportApprovalToken).one().used is True
        assert audit.call_args.kwargs["detail"] == {
            "params_hash": db.query(ExportApprovalToken).one().params_hash
        }
    finally:
        db.close()


def test_approval_and_execute_http_contract() -> None:
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    db, user_a, user_b = _session_with_users()
    old_overrides = app.dependency_overrides.copy()
    current_user = [user_a]

    def override_db():
        yield db

    def override_current_user():
        user = current_user[0]
        return user, user.tenant_id

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_current_user
    try:
        with patch("app.export_approval.write_audit"), patch("app.routers.export_approval.write_audit") as audit:
            with TestClient(app, raise_server_exceptions=False) as client:
                denied = client.post("/api/admin/export/approve", json={"password": "wrong", "params": PARAMS})
                assert denied.status_code == 401
                assert audit.call_args.kwargs["action"] == "export.approval_denied"

                approved = client.post(
                    "/api/admin/export/approve",
                    json={"password": "correct-password", "params": PARAMS},
                )
                assert approved.status_code == 200
                raw = approved.json()["approval_token"]
                assert db.query(ExportApprovalToken).filter_by(token=raw).first() is None

                assert client.post("/api/admin/export/execute", json={"approval_token": "", "params": PARAMS}).status_code == 403
                assert client.post("/api/admin/export/execute", json={"approval_token": raw, "params": {"format": "pdf"}}).status_code == 403
                assert client.post("/api/admin/export/execute", json={"approval_token": raw, "params": PARAMS}).status_code == 200
                assert client.post("/api/admin/export/execute", json={"approval_token": raw, "params": PARAMS}).status_code == 403

                second = client.post(
                    "/api/admin/export/approve",
                    json={"password": "correct-password", "params": PARAMS},
                ).json()["approval_token"]
                current_user[0] = user_b
                assert client.post("/api/admin/export/execute", json={"approval_token": second, "params": PARAMS}).status_code == 403
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        db.close()
