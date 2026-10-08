"""Offline contract coverage for RND-415 platform operator accounts."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator
from urllib.parse import parse_qs, urlparse

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit import AuditAction
from app.auth import PLATFORM_SESSION_COOKIE, hash_password, verify_password
from app.db.base import Base
from app.db.models import (
    AuditLog,
    PlatformAdmin,
    PlatformAdminInvitation,
    PlatformAdminPasswordHistory,
    PlatformAdminSession,
    Tenant,
)
from app.db.session import get_db
from app.main import create_app


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0067_rnd415_platform_operator_accounts.py"
)


def _tables():
    return [
        Tenant.__table__,
        PlatformAdmin.__table__,
        PlatformAdminSession.__table__,
        PlatformAdminPasswordHistory.__table__,
        PlatformAdminInvitation.__table__,
        AuditLog.__table__,
    ]


def _basic() -> dict[str, str]:
    encoded = base64.b64encode(b"operator@example.test:CurrentPass123").decode()
    return {"Authorization": f"Basic {encoded}"}


@pytest.fixture()
def platform_client() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_tables())
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            (
                Tenant(id="tenant-default", name="Default", slug="default"),
                PlatformAdmin(
                    id="platform-operator",
                    name="Current Operator",
                    email="operator@example.test",
                    password_hash=hash_password("CurrentPass123"),
                    status="active",
                ),
            )
        )
        db.commit()

    app = create_app()

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_platform_settings_pages_are_authenticated_and_use_platform_design_system(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, _factory = platform_client

    unauthenticated = client.get("/platform/settings", follow_redirects=False)
    assert unauthenticated.status_code == 302
    assert unauthenticated.headers["location"] == "/platform/login"

    page = client.get("/platform/settings", headers=_basic())
    assert page.status_code == 200
    assert 'data-plane="platform"' in page.text
    assert "/web/static/platform-console-orange.css" in page.text
    assert "/web/static/platform-accounts.css" in page.text
    assert "运营账号" in page.text
    assert 'href="/platform/settings"' in page.text
    assert 'id="password-form"' in page.text
    assert 'id="operator-rows"' in page.text

    invite_page = client.get("/platform/settings/operators/new", headers=_basic())
    assert invite_page.status_code == 200
    assert "管理员不会接触或设置其密码" in invite_page.text
    assert 'id="operator-invite-form"' in invite_page.text

    login = client.post(
        "/platform/login",
        json={"email": "operator@example.test", "password": "CurrentPass123"},
    )
    assert login.status_code == 200
    directory = client.get("/api/platform/operators")
    assert directory.status_code == 200
    assert len(directory.json()["operators"]) == 1
    operator = directory.json()["operators"][0]
    assert {key: operator[key] for key in ("id", "name", "email", "role", "status")} == {
        "id": "platform-operator",
        "name": "Current Operator",
        "email": "operator@example.test",
        "role": "super_admin",
        "status": "active",
    }
    assert operator["last_login_at"] is not None

    # The public shell must not reflect an invitation bearer token into HTML.
    activation = client.get("/platform/accept-invite?token=secret-decoy")
    assert activation.status_code == 200
    assert "secret-decoy" not in activation.text
    assert "/web/static/platform-operator-accept.js" in activation.text


def test_password_change_revokes_other_sessions_keeps_current_and_writes_safe_history_audit(
    platform_client: tuple[TestClient, sessionmaker],
) -> None:
    client, factory = platform_client
    now = datetime.now(timezone.utc)
    with factory() as db:
        db.add_all(
            (
                PlatformAdminSession(
                    id="current-session",
                    platform_admin_id="platform-operator",
                    expires_at=now + timedelta(hours=1),
                    is_revoked=False,
                ),
                PlatformAdminSession(
                    id="other-session",
                    platform_admin_id="platform-operator",
                    expires_at=now + timedelta(hours=1),
                    is_revoked=False,
                ),
            )
        )
        db.commit()

    client.cookies.set(PLATFORM_SESSION_COOKIE, "current-session")
    response = client.post(
        "/api/platform/account/password",
        json={
            "current_password": "CurrentPass123",
            "new_password": "NextPassword456",
            "new_password_confirmation": "NextPassword456",
            "revoke_other_sessions": True,
            "reason_code": "routine_rotation",
            "note": "quarterly security rotation",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "revoked_other_sessions": True}
    assert "CurrentPass123" not in response.text
    assert "NextPassword456" not in response.text
    assert "pbkdf2" not in response.text
    with factory() as db:
        operator = db.get(PlatformAdmin, "platform-operator")
        assert operator is not None
        assert verify_password("NextPassword456", operator.password_hash)
        assert not verify_password("CurrentPass123", operator.password_hash)
        assert db.get(PlatformAdminSession, "current-session").is_revoked is False
        assert db.get(PlatformAdminSession, "other-session").is_revoked is True
        history = db.query(PlatformAdminPasswordHistory).one()
        assert verify_password("CurrentPass123", history.password_hash)
        audit = db.query(AuditLog).filter_by(
            action=AuditAction.PLATFORM_OPERATOR_PASSWORD_CHANGED
        ).one()
        assert audit.detail == {
            "platform_admin_id": "platform-operator",
            "reason_code": "routine_rotation",
            "note": "quarterly security rotation",
            "revoked_other_sessions": True,
        }
        assert "CurrentPass123" not in str(audit.detail)
        assert "NextPassword456" not in str(audit.detail)
        assert "pbkdf2" not in str(audit.detail)

    reused = client.post(
        "/api/platform/account/password",
        json={
            "current_password": "NextPassword456",
            "new_password": "CurrentPass123",
            "new_password_confirmation": "CurrentPass123",
            "revoke_other_sessions": False,
            "reason_code": "routine_rotation",
            "note": "test reuse guard",
        },
    )
    assert reused.status_code == 400
    assert reused.json() == {"detail": "recent_password_reused"}
    assert "CurrentPass123" not in reused.text


@pytest.mark.parametrize(
    ("current_password", "new_password", "detail"),
    [
        ("wrong-password", "NextPassword456", "invalid_current_password"),
        ("CurrentPass123", "short", "weak_password"),
        ("CurrentPass123", "alllowercase123", "weak_password"),
    ],
)
def test_password_change_rejects_invalid_inputs_without_reflecting_passwords(
    platform_client: tuple[TestClient, sessionmaker],
    current_password: str,
    new_password: str,
    detail: str,
) -> None:
    client, _factory = platform_client
    response = client.post(
        "/api/platform/account/password",
        headers=_basic(),
        json={
            "current_password": current_password,
            "new_password": new_password,
            "new_password_confirmation": new_password,
            "revoke_other_sessions": False,
            "reason_code": "security_maintenance",
            "note": "test validation",
        },
    )

    assert response.status_code == (401 if detail == "invalid_current_password" else 400)
    assert response.json() == {"detail": detail}
    assert current_password not in response.text
    assert new_password not in response.text


def test_operator_invitation_is_hashed_mailed_once_and_consumed_once(
    platform_client: tuple[TestClient, sessionmaker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = platform_client
    delivered: list[tuple[str, str]] = []
    monkeypatch.setenv("INVITE_BASE_URL", "https://platform.example.test")
    monkeypatch.setattr(
        "app.services.platform_accounts.send_invite_email",
        lambda to_email, accept_link: delivered.append((to_email, accept_link)) or True,
    )

    response = client.post(
        "/api/platform/operators/invitations",
        headers=_basic(),
        json={
            "name": "Invited Operator",
            "email": "INVITED@EXAMPLE.TEST",
            "reason_code": "staffing_change",
            "note": "on-call coverage",
        },
    )

    assert response.status_code == 201
    result = response.json()
    assert result["email"] == "invited@example.test"
    assert result["role"] == "super_admin"
    assert result["status"] == "pending"
    assert result["audit_id"]
    assert "token" not in result
    assert len(delivered) == 1
    assert delivered[0][0] == "invited@example.test"
    raw_token = parse_qs(urlparse(delivered[0][1]).query)["token"][0]
    assert raw_token not in response.text

    with factory() as db:
        operator = db.query(PlatformAdmin).filter_by(email="invited@example.test").one()
        assert operator.status == "pending"
        assert operator.password_hash is None
        invitation = db.query(PlatformAdminInvitation).filter_by(
            platform_admin_id=operator.id
        ).one()
        assert invitation.token_hash != raw_token
        assert invitation.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
        audit = db.query(AuditLog).filter_by(
            action=AuditAction.PLATFORM_OPERATOR_INVITED
        ).one()
        assert audit.id == result["audit_id"]
        assert raw_token not in str(audit.detail)

    accepted = client.post(
        "/api/platform/operators/accept-invite",
        json={
            "token": raw_token,
            "password": "AcceptedPassword456",
            "password_confirmation": "AcceptedPassword456",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
    assert raw_token not in accepted.text
    assert "AcceptedPassword456" not in accepted.text

    with factory() as db:
        operator = db.query(PlatformAdmin).filter_by(email="invited@example.test").one()
        assert operator.status == "active"
        assert verify_password("AcceptedPassword456", operator.password_hash)
        invitation = db.query(PlatformAdminInvitation).filter_by(
            platform_admin_id=operator.id
        ).one()
        assert invitation.used_at is not None
        assert db.query(AuditLog).filter_by(
            action=AuditAction.PLATFORM_OPERATOR_INVITE_ACCEPTED
        ).count() == 1

    replay = client.post(
        "/api/platform/operators/accept-invite",
        json={
            "token": raw_token,
            "password": "DifferentPassword456",
            "password_confirmation": "DifferentPassword456",
        },
    )
    assert replay.status_code == 400
    assert replay.json() == {"detail": "invalid_or_expired_invitation"}
    assert raw_token not in replay.text


def test_failed_delivery_rolls_back_operator_invitation(
    platform_client: tuple[TestClient, sessionmaker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = platform_client
    monkeypatch.setenv("INVITE_BASE_URL", "https://platform.example.test")
    monkeypatch.setattr("app.services.platform_accounts.send_invite_email", lambda *_args: False)

    response = client.post(
        "/api/platform/operators/invitations",
        headers=_basic(),
        json={
            "name": "Undelivered Operator",
            "email": "undelivered@example.test",
            "reason_code": "staffing_change",
            "note": "must not persist without delivery",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "platform_invitation_delivery_failed"}
    with factory() as db:
        assert db.query(PlatformAdmin).filter_by(email="undelivered@example.test").count() == 0
        assert db.query(PlatformAdminInvitation).count() == 0
        assert db.query(AuditLog).filter_by(
            action=AuditAction.PLATFORM_OPERATOR_INVITED
        ).count() == 0


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd415_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rnd415_migration_roundtrip_and_pending_downgrade_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE platform_admins ("
                "id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, "
                "role TEXT NOT NULL DEFAULT 'superadmin', status TEXT NOT NULL DEFAULT 'active', "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, last_active_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO platform_admins (id, email, password_hash) "
                "VALUES ('legacy-admin', 'legacy@example.test', 'legacy-hash')"
            )
        )
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(connection))
        )
        migration.upgrade()
        tables = set(inspect(connection).get_table_names())
        assert {
            "platform_admin_password_history",
            "platform_admin_invitations",
        }.issubset(tables)
        columns = {
            column["name"] for column in inspect(connection).get_columns("platform_admins")
        }
        assert {"name", "last_login_at"}.issubset(columns)
        assert connection.execute(
            text("SELECT password_hash FROM platform_admins WHERE id = 'legacy-admin'")
        ).scalar_one() == "legacy-hash"

        connection.execute(
            text("UPDATE platform_admins SET status = 'pending' WHERE id = 'legacy-admin'")
        )
        with pytest.raises(RuntimeError, match="pending platform operator invitations"):
            migration.downgrade()
        connection.execute(
            text("UPDATE platform_admins SET status = 'active' WHERE id = 'legacy-admin'")
        )
        migration.downgrade()
        post_columns = {
            column["name"] for column in inspect(connection).get_columns("platform_admins")
        }
        assert "name" not in post_columns
        assert "last_login_at" not in post_columns
        assert "platform_admin_invitations" not in inspect(connection).get_table_names()


def test_rnd415_migration_extends_current_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0067"
    assert migration.down_revision == "0066"
