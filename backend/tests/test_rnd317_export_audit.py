"""Offline coverage for RND-317's export audit hook and recording route."""
from __future__ import annotations

import builtins
import hashlib
import json
import sys
from types import ModuleType
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.audit import (
    AuditAction,
    AuditObjectType,
    record_export_audit,
)


def test_record_export_audit_uses_non_sensitive_hashed_scope() -> None:
    db = Mock()
    scope = {"conversation_ids": ["room-a"], "msgtype": "text"}

    with patch("app.audit.write_audit") as write:
        record_export_audit(
            db,
            tenant_id="tenant-a",
            admin_user_id="admin-a",
            export_format="pdf",
            record_count=3,
            scope=scope,
            approval_ref="a" * 64,
            gate_enforced=True,
        )

    detail = write.call_args.kwargs["detail"]
    expected = hashlib.sha256(
        json.dumps(
            {"format": "pdf", "scope": scope},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert write.call_args.kwargs["tenant_id"] == "tenant-a"
    assert write.call_args.kwargs["admin_user_id"] == "admin-a"
    assert write.call_args.kwargs["action"] == AuditAction.EXPORT
    assert write.call_args.kwargs["object_type"] == AuditObjectType.EXPORT
    assert detail == {
        "format": "pdf",
        "record_count": 3,
        "params_hash": expected,
        "approval_ref": "a" * 64,
        "gate_enforced": True,
    }
    assert not {"content", "payload", "body", "decrypted", "content_text"} & set(detail)


def test_record_endpoint_records_tenant_scoped_count_without_gate() -> None:
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    user = type("User", (), {"id": "admin-a", "tenant_id": "tenant-a"})()
    db = Mock()
    old_overrides = app.dependency_overrides.copy()

    def override_db():
        yield db

    def override_current_user():
        return user, user.tenant_id

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_current_user
    real_import = builtins.__import__

    def import_without_approval(name, *args, **kwargs):
        if name == "app.export_approval":
            raise ImportError("RND-316 not deployed")
        return real_import(name, *args, **kwargs)

    try:
        with patch("builtins.__import__", side_effect=import_without_approval), patch(
            "app.routers.export_audit._count_records", return_value=2
        ), patch("app.routers.export_audit.record_export_audit") as audit:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/admin/export/record",
                    json={"format": "csv", "scope": {"msgtype": "text"}},
                )

        assert response.status_code == 200
        assert response.json() == {
            "recorded": True,
            "format": "csv",
            "record_count": 2,
            "gate_enforced": False,
        }
        assert audit.call_args.kwargs["tenant_id"] == "tenant-a"
        assert audit.call_args.kwargs["scope"] == {"msgtype": "text"}
        assert audit.call_args.kwargs["gate_enforced"] is False
        db.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)


def test_record_endpoint_consumes_matching_approval_params() -> None:
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    user = type("User", (), {"id": "admin-a", "tenant_id": "tenant-a"})()
    db = Mock()
    old_overrides = app.dependency_overrides.copy()
    approval = ModuleType("app.export_approval")

    class ExportNotApprovedError(Exception):
        pass

    require = Mock()
    approval.ExportNotApprovedError = ExportNotApprovedError
    approval.require_export_approval = require

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: (user, user.tenant_id)
    try:
        with patch.dict(sys.modules, {"app.export_approval": approval}), patch(
            "app.routers.export_audit._count_records", return_value=1
        ), patch("app.routers.export_audit.record_export_audit") as audit:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/admin/export/record",
                    json={
                        "format": "pdf",
                        "scope": {"conversation_ids": ["room-a"]},
                        "approval_token": "approved-token",
                    },
                )

        assert response.status_code == 200
        assert response.json()["gate_enforced"] is True
        assert require.call_args.kwargs["params"] == {
            "format": "pdf",
            "scope": {"conversation_ids": ["room-a"]},
        }
        assert audit.call_args.kwargs["approval_ref"] == hashlib.sha256(
            b"approved-token"
        ).hexdigest()
        assert audit.call_args.kwargs["gate_enforced"] is True

        require.side_effect = ExportNotApprovedError("invalid approval token")
        with patch.dict(sys.modules, {"app.export_approval": approval}), patch(
            "app.routers.export_audit._count_records", return_value=1
        ), patch("app.routers.export_audit.record_export_audit"):
            with TestClient(app, raise_server_exceptions=False) as client:
                rejected = client.post(
                    "/api/admin/export/record",
                    json={"approval_token": "invalid-token"},
                )
        assert rejected.status_code == 403
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
