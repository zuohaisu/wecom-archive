"""RBAC vocabulary and dependency scaffold coverage for RND-280 (F0-5)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import ADMIN_ROLES, require_role


def test_admin_roles_match_the_locked_f0_vocabulary() -> None:
    assert ADMIN_ROLES == ("owner", "admin", "compliance", "legal", "readonlyaudit")


def test_require_role_allows_listed_roles_and_rejects_other_roles() -> None:
    allowed_auth = (SimpleNamespace(role="admin"), "tenant-x")
    readonly_auth = (SimpleNamespace(role="readonlyaudit"), "tenant-x")

    assert require_role("admin", "owner")(allowed_auth) is allowed_auth
    with pytest.raises(HTTPException) as exc_info:
        require_role("admin", "owner")(readonly_auth)
    assert exc_info.value.status_code == 403


def test_require_role_without_arguments_allows_every_admin_role() -> None:
    auth = (SimpleNamespace(role="readonlyaudit"), "tenant-x")

    assert require_role()(auth) is auth


def test_auth_me_returns_contract_db_default_role_and_unauthenticated_shape() -> None:
    # Reuse the SQLite contract schema so role is supplied by its DB default.
    from tests.test_http_contract import _make_session
    from app.db.session import get_db
    from app.main import app

    db = _make_session()
    try:
        db.execute(
            text(
                "INSERT INTO admin_users (id, tenant_id, wecom_user_id, name) "
                "VALUES ('user-001', 'tenant-a', 'staff_alice', 'Alice')"
            )
        )
        db.execute(
            text(
                "INSERT INTO admin_sessions "
                "(id, admin_user_id, tenant_id, wecom_user_id, expires_at) "
                "VALUES ('session-001', 'user-001', 'tenant-a', 'staff_alice', "
                "'2099-01-01T00:00:00+00:00')"
            )
        )
        db.commit()

        def override_db():
            yield db

        app.dependency_overrides[get_db] = override_db
        with TestClient(app, raise_server_exceptions=False) as client:
            unauthenticated = client.get("/api/auth/me")
            assert unauthenticated.json() == {"authenticated": False}

            client.cookies.set("session_id", "session-001")
            authenticated = client.get("/api/auth/me")
            assert authenticated.status_code == 200
            assert authenticated.json()["role"] == "admin"
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_require_role_is_attached_only_to_authorized_admin_routes() -> None:
    routers_dir = Path(__file__).parents[1] / "app" / "routers"
    for router_file in routers_dir.rglob("*.py"):
        source = router_file.read_text()
        if router_file.name in {"audit.py", "external_contacts.py", "media_library.py", "settings.py"}:
            assert "Depends(require_role())" in source
        elif router_file.name in {"retention.py", "onboarding.py"}:
            assert "Depends(require_role())" in source
            assert 'Depends(require_role("admin", "owner"))' in source
        elif router_file.name == "users.py":
            # RND-321: +3 — access-request list/link/create-account.
            assert source.count('Depends(require_role("admin", "owner"))') == 6
        elif router_file.name == "auth.py":
            assert source.count('Depends(require_role("admin", "owner"))') == 2
        elif router_file.name == "exports.py":
            assert source.count('Depends(require_role("owner"))') == 6
        else:
            assert "Depends(require_role" not in source
