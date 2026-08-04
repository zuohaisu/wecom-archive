"""Deterministic RND-335 security-activity contract coverage."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import pytest

from app.audit import ACTION_CATALOG, AuditAction, AuditCategory, audit_category
from app.db.models import (
    AdminAccessRequest,
    AdminLoginIdentity,
    AdminSession,
    AdminUser,
    AppConfigStore,
    AuditLog,
    PasswordResetToken,
    RetentionConfig,
    Tenant,
    TenantWecomConfig,
)
from tests.fakes import generate_test_rsa_keypair, worker_db  # noqa: F401


@pytest.fixture()
def rsa_keys():
    return generate_test_rsa_keypair()


def _assert_safe_detail(value: object) -> None:
    forbidden = (
        "password", "token", "secret", "signed", "storage", "search",
        "message", "decrypted", "payload", "path", "email", "username",
    )
    if isinstance(value, dict):
        for key, item in value.items():
            assert not any(word in str(key).lower() for word in forbidden)
            _assert_safe_detail(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_safe_detail(item)
    elif isinstance(value, str):
        assert not any(word in value.lower() for word in forbidden)


def test_catalogue_classifies_every_required_action_and_unknown_history_as_system() -> None:
    required = {
        AuditAction.LOGIN, AuditAction.LOGIN_FAILED, AuditAction.LOGOUT,
        AuditAction.PASSWORD_RESET_REQUESTED, AuditAction.PASSWORD_RESET_COMPLETED,
        AuditAction.PASSWORD_CHANGED, AuditAction.USER_INVITED,
        AuditAction.USER_INVITE_ACCEPTED, AuditAction.USER_ENABLED,
        AuditAction.USER_DISABLED, AuditAction.USER_ROLE_CHANGED,
        AuditAction.USER_ACCESS_REQUESTED, AuditAction.USER_ACCESS_REQUEST_LINKED,
        AuditAction.USER_ACCESS_REQUEST_ACCOUNT_CREATED, AuditAction.USER_PASSWORD_RESET_INITIATED,
        AuditAction.CONFIG_CHANGED, AuditAction.RETENTION_CONFIG_CHANGED,
        AuditAction.RETENTION_CONFIG_LOCKED, AuditAction.EXPORT_APPROVAL_GRANTED,
        AuditAction.EXPORT_APPROVAL_DENIED, AuditAction.EXPORT_APPROVAL_CONSUMED,
        AuditAction.EXPORT, AuditAction.MEDIA_DOWNLOAD,
        AuditAction.PLATFORM_TENANT_ACCESSED, AuditAction.PLATFORM_TENANT_ACTIVATED,
        AuditAction.PLATFORM_TENANT_DEACTIVATED, AuditAction.DECRYPT_COMPLETED,
        AuditAction.RETENTION_MESSAGES_LOCKED,
    }
    assert required <= ACTION_CATALOG.keys()
    assert all(category in {
        AuditCategory.SECURITY, AuditCategory.ACCOUNT, AuditCategory.CONFIGURATION,
        AuditCategory.DATA_ACCESS, AuditCategory.SYSTEM,
    } for category, _object_type in ACTION_CATALOG.values())
    assert audit_category("legacy.unclassified") == AuditCategory.SYSTEM
    assert audit_category(AuditAction.PLATFORM_TENANT_ACCESSED) == AuditCategory.SECURITY
    assert audit_category(AuditAction.DECRYPT_COMPLETED) == AuditCategory.SYSTEM


def test_platform_audits_survive_a_new_session_with_admin_user_foreign_key() -> None:
    """No identity-map false positive: close the writer Session before reading."""
    from app.auth import require_platform_tenant_scope
    from app.db.models import PlatformAdmin
    from app.routers.platform import update_tenant_status
    from app.schemas.tenant_provision import TenantStatusUpdateIn

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Tenant.__table__.create(engine)
    AdminUser.__table__.create(engine)
    # Production-shape FK is intentional: a PlatformAdmin id is invalid here.
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE audit_logs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
                admin_user_id TEXT REFERENCES admin_users(id), action TEXT NOT NULL,
                object_type TEXT NOT NULL, object_id TEXT, detail JSON,
                created_at DATETIME NOT NULL
            )
        """))

    tenant = Tenant(id="tenant-rnd335", name="RND-335", slug="rnd335", is_active=True)
    platform_admin = PlatformAdmin(id="platform-rnd335", email="platform@example.test", password_hash="x")
    with Session(engine) as db:
        db.add(tenant)
        db.commit()
        require_platform_tenant_scope(tenant_id=tenant.id, platform_admin=platform_admin, db=db)
        update_tenant_status(
            tenant.id, TenantStatusUpdateIn(is_active=False), platform_admin, db
        )
        # Repeating an already-applied state is a no-op and creates no audit.
        update_tenant_status(
            tenant.id, TenantStatusUpdateIn(is_active=False), platform_admin, db
        )
        update_tenant_status(
            tenant.id, TenantStatusUpdateIn(is_active=True), platform_admin, db
        )

    with Session(engine) as fresh_db:
        rows = fresh_db.query(AuditLog).filter(AuditLog.tenant_id == tenant.id).all()
        assert {row.action for row in rows} == {
            AuditAction.PLATFORM_TENANT_ACCESSED,
            AuditAction.PLATFORM_TENANT_DEACTIVATED,
            AuditAction.PLATFORM_TENANT_ACTIVATED,
        }
        assert all(row.admin_user_id is None for row in rows)
        assert all(row.detail == {"platform_admin_id": platform_admin.id} or
                   row.detail in (
                       {"platform_admin_id": platform_admin.id, "previous_is_active": True, "is_active": False},
                       {"platform_admin_id": platform_admin.id, "previous_is_active": False, "is_active": True},
                   ) for row in rows)
        assert fresh_db.get(Tenant, tenant.id).is_active is True
    engine.dispose()


def _persistent_audit_engine():
    """SQLite production-shape business tables plus an FK-enforced audit sink."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    for table in (
        Tenant.__table__, TenantWecomConfig.__table__, AdminUser.__table__, AdminSession.__table__,
        PasswordResetToken.__table__, AppConfigStore.__table__, RetentionConfig.__table__,
        AdminLoginIdentity.__table__, AdminAccessRequest.__table__,
    ):
        table.create(engine)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE audit_logs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
                admin_user_id TEXT REFERENCES admin_users(id), action TEXT NOT NULL,
                object_type TEXT NOT NULL, object_id TEXT, detail JSON,
                created_at DATETIME NOT NULL
            )
        """))
    return engine


def _audit_rows(engine, tenant_id: str) -> list[AuditLog]:
    with Session(engine) as fresh_db:
        return fresh_db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id).all()


def test_audit_api_category_and_system_filters_are_tenant_scoped() -> None:
    from datetime import timedelta

    from fastapi.testclient import TestClient
    from app.db.session import get_db
    from app.main import app

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Tenant.__table__.create(engine)
    AdminUser.__table__.create(engine)
    AdminSession.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE audit_logs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, admin_user_id TEXT,
                action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT,
                detail JSON, created_at DATETIME NOT NULL
            )
        """))
    db = Session(engine)
    now = datetime.now(timezone.utc)
    tenant = Tenant(id="tenant-filter", name="Filter", slug="filter", is_active=True)
    actor = AdminUser(id="actor-filter", tenant_id=tenant.id, wecom_user_id="actor", role="admin", status="active")
    session = AdminSession(
        id="session-filter", tenant_id=tenant.id, admin_user_id=actor.id,
        wecom_user_id=actor.wecom_user_id, expires_at=now + timedelta(hours=1), is_revoked=False,
    )
    other = Tenant(id="tenant-other", name="Other", slug="other", is_active=True)
    db.add_all([tenant, actor, session, other])
    db.add_all([
        AuditLog(id="security", tenant_id=tenant.id, admin_user_id=actor.id,
                 action=AuditAction.LOGIN, object_type="admin_user", created_at=now),
        AuditLog(id="system", tenant_id=tenant.id, action=AuditAction.DECRYPT_COMPLETED,
                 object_type="key_version", created_at=now - timedelta(seconds=1)),
        AuditLog(id="legacy", tenant_id=tenant.id, action="legacy.unknown",
                 object_type="legacy", created_at=now - timedelta(seconds=2)),
        AuditLog(id="other", tenant_id=other.id, action=AuditAction.LOGIN,
                 object_type="admin_user", created_at=now),
    ])
    db.commit()

    def override_db():
        yield db

    old_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            cookies = {"session_id": session.id}
            # No new filters preserves the legacy tenant-scoped collection;
            # category is additive to each serialized item.
            baseline = client.get("/api/admin/audit-logs", cookies=cookies)
            assert baseline.status_code == 200
            assert baseline.json()["total"] == 3
            assert {item["category"] for item in baseline.json()["items"]} == {
                AuditCategory.SECURITY, AuditCategory.SYSTEM,
            }
            first_page = client.get("/api/admin/audit-logs?limit=1&offset=0", cookies=cookies)
            second_page = client.get("/api/admin/audit-logs?limit=1&offset=1", cookies=cookies)
            assert first_page.json()["total"] == second_page.json()["total"] == 3
            assert first_page.json()["has_more"] is True
            assert first_page.json()["items"][0]["id"] != second_page.json()["items"][0]["id"]

            hidden = client.get("/api/admin/audit-logs?include_system=false", cookies=cookies)
            assert hidden.status_code == 200
            assert hidden.json()["total"] == 1
            assert [item["category"] for item in hidden.json()["items"]] == [AuditCategory.SECURITY]

            system = client.get("/api/admin/audit-logs?category=system", cookies=cookies)
            assert system.status_code == 200
            assert system.json()["total"] == 2  # explicit + unknown-history fallback
            assert {item["category"] for item in system.json()["items"]} == {AuditCategory.SYSTEM}

            combined = client.get(
                "/api/admin/audit-logs?category=security,system&include_system=false",
                cookies=cookies,
            )
            assert combined.json()["total"] == 1
            # include_system=true never broadens an explicit category.
            security_only = client.get(
                "/api/admin/audit-logs?category=security&include_system=true",
                cookies=cookies,
            )
            assert security_only.json()["total"] == 1
            filtered_page = client.get(
                "/api/admin/audit-logs?category=system&limit=1&offset=1",
                cookies=cookies,
            )
            assert filtered_page.json()["total"] == 2
            assert filtered_page.json()["has_more"] is False
            assert len(filtered_page.json()["items"]) == 1
            assert client.get("/api/admin/audit-logs?category=bad", cookies=cookies).status_code == 422
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        db.close()
        engine.dispose()


def test_persisted_human_actions_are_exactly_once_and_minimized(monkeypatch) -> None:
    """Drive real writers, close the writer Session, then inspect AuditLog."""
    from app.auth import create_password_reset_token, hash_password
    from app.config.resolver import invalidate
    from app.routers.auth import (
        _AcceptBody,
        _ResetBody,
        _create_pending_invite,
        accept_invite,
        password_forgot,
        password_reset,
    )
    from app.routers.retention import put_retention_config
    from app.routers.settings import _ChangePasswordBody, change_password, put_settings
    from app.routers.users import _StatusUpdate, admin_reset_password, update_user_status
    from app.schemas.retention import RetentionConfigUpdate
    from app.schemas.settings import SettingsUpdateIn

    engine = _persistent_audit_engine()
    tenant_id = "tenant-human"
    actor_id = "actor-human"
    target_id = "target-human"
    invite_token = None
    reset_token = None
    secret_decoy = (
        "rnd335-secret-decoy signed-url-decoy storage-key-decoy search-text-decoy "
        "message-text-decoy path-decoy"
    )
    password_decoy = "rnd335-password-decoy"
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    # Replace delivery only; the database writes and audit writer remain real.
    monkeypatch.setattr("app.email.send_invite_email", lambda *_: True)
    monkeypatch.setattr("app.email.send_password_reset_email", lambda *_: True)
    monkeypatch.setattr("app.routers.users.send_password_reset_email", lambda *_: True)
    invalidate()

    with Session(engine) as db:
        tenant = Tenant(id=tenant_id, name="Human", slug="default", is_active=True)
        actor = AdminUser(
            id=actor_id, tenant_id=tenant_id, wecom_user_id="actor", email="actor@example.test",
            role="admin", status="active", password_hash=hash_password("actor-old-password"),
        )
        target = AdminUser(
            id=target_id, tenant_id=tenant_id, wecom_user_id="target", email="target@example.test",
            role="admin", status="active", password_hash=hash_password("target-old-password"),
        )
        db.add(tenant)
        db.commit()
        db.add_all([actor, target])
        db.commit()

        _create_pending_invite(
            db, tenant_id=tenant_id, admin_user_id=actor_id, email="invite@example.test",
            name="Invitee", role="admin", wecom_user_id="invitee",
        )
        invited = db.query(AdminUser).filter(AdminUser.wecom_user_id == "invitee").one()
        invite_token = invited.invite_token
        invited_id = invited.id
        _create_pending_invite(
            db, tenant_id=tenant_id, admin_user_id=actor_id, email="invite@example.test",
            name="Invitee", role="admin", wecom_user_id="invitee",
        )
        accept_invite(_AcceptBody(token=invite_token, password=password_decoy), db)

        update_user_status(target_id, _StatusUpdate(status="disabled"), (actor, tenant_id), db)
        update_user_status(target_id, _StatusUpdate(status="disabled"), (actor, tenant_id), db)
        update_user_status(target_id, _StatusUpdate(status="active"), (actor, tenant_id), db)
        admin_reset_password(target_id, (actor, tenant_id), db)

        password_forgot(type("Forgot", (), {"email": target.email})(), db)
        reset_token = create_password_reset_token(db, target)
        db.commit()
        password_reset(_ResetBody(token=reset_token, password="target-reset-password"), db)
        change_password(
            _ChangePasswordBody(old_password="target-reset-password", new_password="target-new-password"),
            (target, tenant_id), db,
        )

        put_settings(
            SettingsUpdateIn(updates={"smtp_password": secret_decoy}), (actor, tenant_id), db
        )
        put_retention_config(
            RetentionConfigUpdate(retention_days=365, lock=False), (actor, tenant_id), db
        )
        put_retention_config(
            RetentionConfigUpdate(retention_days=365, lock=True), (actor, tenant_id), db
        )
        audit_count = db.query(AuditLog).count()
        put_settings(SettingsUpdateIn(updates={"smtp_password": ""}), (actor, tenant_id), db)
        put_settings(SettingsUpdateIn(updates={"smtp_password": secret_decoy}), (actor, tenant_id), db)
        with pytest.raises(Exception) as locked:
            put_retention_config(
                RetentionConfigUpdate(retention_days=365, lock=False), (actor, tenant_id), db
            )
        assert getattr(locked.value, "status_code", None) == 423
        other = Tenant(id="tenant-human-other", name="Other", slug="other", is_active=True)
        db.add(other)
        db.commit()
        other_target = AdminUser(
            id="other-target", tenant_id=other.id, wecom_user_id="other-target",
            role="admin", status="active",
        )
        db.add(other_target)
        db.commit()
        with pytest.raises(Exception) as cross_tenant:
            update_user_status(other_target.id, _StatusUpdate(status="disabled"), (actor, tenant_id), db)
        with pytest.raises(Exception) as self_disable:
            update_user_status(actor_id, _StatusUpdate(status="disabled"), (actor, tenant_id), db)
        assert getattr(cross_tenant.value, "status_code", None) == 404
        assert getattr(self_disable.value, "status_code", None) == 400
        assert db.query(AuditLog).count() == audit_count

    rows = _audit_rows(engine, tenant_id)
    actions = [row.action for row in rows]
    assert actions.count(AuditAction.USER_INVITED) == 2
    for action in (
        AuditAction.USER_INVITE_ACCEPTED, AuditAction.USER_DISABLED, AuditAction.USER_ENABLED,
        AuditAction.USER_PASSWORD_RESET_INITIATED, AuditAction.PASSWORD_RESET_REQUESTED,
        AuditAction.PASSWORD_RESET_COMPLETED, AuditAction.PASSWORD_CHANGED,
        AuditAction.CONFIG_CHANGED, AuditAction.RETENTION_CONFIG_CHANGED,
        AuditAction.RETENTION_CONFIG_LOCKED,
    ):
        assert actions.count(action) == 1
    assert all(row.tenant_id == tenant_id for row in rows)
    expected_actor_target = {
        AuditAction.USER_INVITED: (actor_id, invited_id),
        AuditAction.USER_INVITE_ACCEPTED: (invited_id, invited_id),
        AuditAction.USER_DISABLED: (actor_id, target_id),
        AuditAction.USER_ENABLED: (actor_id, target_id),
        AuditAction.USER_PASSWORD_RESET_INITIATED: (actor_id, target_id),
        AuditAction.PASSWORD_RESET_REQUESTED: (target_id, target_id),
        AuditAction.PASSWORD_RESET_COMPLETED: (target_id, target_id),
        AuditAction.PASSWORD_CHANGED: (target_id, target_id),
    }
    for action, (expected_actor, expected_target) in expected_actor_target.items():
        matching = [row for row in rows if row.action == action]
        assert all(row.admin_user_id == expected_actor and row.object_id == expected_target for row in matching)

    def assert_decoys_absent(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                assert_decoys_absent(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                assert_decoys_absent(item)
        elif isinstance(value, str):
            for decoy in (invite_token, reset_token, secret_decoy, password_decoy):
                assert decoy not in value

    for row in rows:
        assert_decoys_absent({"object_id": row.object_id, "detail": row.detail})
    # Inspect the real API serializer too, not only ORM rows.
    from app.routers.audit import list_audit_logs
    with Session(engine) as fresh_db:
        fresh_actor = fresh_db.get(AdminUser, actor_id)
        api_result = list_audit_logs(
            action=None, category=None, include_system=None, object_type=None,
            operator=None, q=None, from_=None, to=None, limit=50, offset=0,
            db=fresh_db, auth=(fresh_actor, tenant_id),
        )
        assert_decoys_absent(api_result.model_dump())
    engine.dispose()


def test_persisted_password_login_failure_is_minimized_and_missing_tenant_writes_nothing(monkeypatch) -> None:
    from fastapi import HTTPException
    from app.auth import hash_password
    from app.routers.auth import _PasswordLoginBody, password_login

    engine = _persistent_audit_engine()
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("ADMIN_USERNAME", "bootstrap")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("bootstrap-password"))
    with Session(engine) as db:
        db.add(Tenant(id="tenant-login", name="Login", slug="default", is_active=True))
        db.commit()
        assert password_login(_PasswordLoginBody(username="bootstrap", password="bootstrap-password"), db).status_code == 200
        with pytest.raises(HTTPException) as known_wrong:
            password_login(_PasswordLoginBody(username="bootstrap", password="wrong-password"), db)
        with pytest.raises(HTTPException) as rejected:
            password_login(_PasswordLoginBody(username="unknown-account", password="wrong-password"), db)
        assert known_wrong.value.status_code == rejected.value.status_code == 401
        assert known_wrong.value.detail == rejected.value.detail == "Invalid credentials"

    from starlette.requests import Request
    from app.routers.auth import auth_logout
    with Session(engine) as db:
        session_id = db.query(AdminSession.id).filter(AdminSession.tenant_id == "tenant-login").scalar()
        auth_logout(
            Request({"type": "http", "headers": [(b"cookie", f"session_id={session_id}".encode())]}), db
        )

    rows = _audit_rows(engine, "tenant-login")
    assert [row.action for row in rows].count(AuditAction.LOGIN) == 1
    assert [row.action for row in rows].count(AuditAction.LOGOUT) == 1
    failed = [row for row in rows if row.action == AuditAction.LOGIN_FAILED]
    assert len(failed) == 2
    assert all(row.admin_user_id is None and row.object_id is None and row.detail is None for row in failed)

    missing_engine = _persistent_audit_engine()
    with Session(missing_engine) as db:
        with pytest.raises(HTTPException) as missing:
            password_login(_PasswordLoginBody(username="unknown-account", password="wrong-password"), db)
        assert missing.value.status_code == 500
        assert db.query(AuditLog).count() == 0
    missing_engine.dispose()
    engine.dispose()


def test_wecom_login_persists_a_minimal_login_audit(monkeypatch) -> None:
    from types import SimpleNamespace
    import app.routers.auth as auth_router

    engine = _persistent_audit_engine()
    with Session(engine) as db:
        tenant = Tenant(id="tenant-wecom", name="WeCom", slug="wecom", is_active=True)
        db.add(tenant)
        db.commit()
        db.add(TenantWecomConfig(
            id="config-wecom", tenant_id=tenant.id, corp_id="corp-rnd335",
            agent_id="agent", app_secret="unused", is_active=True,
        ))
        db.add(
            AdminUser(
                id="wecom-user",
                tenant_id=tenant.id,
                wecom_user_id="wecom-user",
                role="compliance",
                status="active",
            )
        )
        # This engine enforces real FKs (PRAGMA foreign_keys=ON above), so
        # the referencing row below needs the AdminUser row flushed first —
        # SQLAlchemy's automatic insert-dependency sort doesn't reliably
        # order this on its own (confirmed: fails without this flush, even
        # though both rows are added in the same session before commit).
        db.flush()
        # RND-321: the callback now resolves identity via AdminLoginIdentity,
        # not by scanning AdminUser.wecom_user_id — without this binding the
        # scan would be treated as unbound and produce an access request.
        db.add(
            AdminLoginIdentity(
                id="login-identity-wecom-user",
                tenant_id=tenant.id,
                provider="wecom",
                subject="wecom-user",
                admin_user_id="wecom-user",
            )
        )
        db.commit()

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, url, **_kwargs):
                if url.endswith("getuserinfo"):
                    return SimpleNamespace(status_code=200, json=lambda: {"errcode": 0, "UserId": "wecom-user"})
                return SimpleNamespace(status_code=200, json=lambda: {
                    "errcode": 0, "userid": "wecom-user", "status": 1, "name": "WeCom User",
                })

        monkeypatch.setattr(auth_router, "get_wecom_oauth_settings", lambda: SimpleNamespace(
            wecom_corp_id="corp-rnd335", wecom_oauth_secret="not-persisted",
        ))
        monkeypatch.setattr(auth_router, "get_wecom_token", lambda *_: "not-persisted")
        monkeypatch.setattr(auth_router.httpx, "Client", FakeClient)
        response = auth_router._resolve_and_sign_wecom_session("code-decoy", db)
        assert response.status_code == 302

    rows = _audit_rows(engine, "tenant-wecom")
    assert len(rows) == 1
    assert rows[0].action == AuditAction.LOGIN
    assert rows[0].object_type == "admin_user"
    assert "code-decoy" not in str(rows[0].detail)
    engine.dispose()


def test_empty_decrypt_poll_writes_no_audit_but_nonempty_batch_writes_one(worker_db, rsa_keys, monkeypatch) -> None:
    from app.services import decrypt_worker
    from app.services.decrypt_worker import run_decrypt_once
    from tests.fakes import FakeWecomSdk, _TENANT_A, insert_archive_message, rsa_encrypt_key_b64

    private_key, public_key = rsa_keys
    writer = MagicMock()
    monkeypatch.setattr(decrypt_worker, "write_audit", writer)
    run_decrypt_once(worker_db, _TENANT_A, "fake", private_key, 7, sdk=FakeWecomSdk())
    writer.assert_not_called()

    record = insert_archive_message(
        worker_db, tenant_id=_TENANT_A, decrypt_status="pending", publickey_ver=7,
        encrypt_random_key=rsa_encrypt_key_b64(public_key, "symmetric"),
        encrypt_chat_msg="payload", seq=99,
    )
    sdk = FakeWecomSdk()
    sdk.set_decrypt_response("payload", {
        "msgtype": "text", "from": "staff", "tolist": [], "roomid": "",
        "msgtime": 1, "text": {"content": "not-audit-detail"},
    })
    run_decrypt_once(worker_db, _TENANT_A, "fake", private_key, 7, sdk=sdk)
    writer.assert_called_once()
    detail = writer.call_args.kwargs["detail"]
    assert detail["scanned"] == 1
    assert detail["publickey_ver"] == 7
    assert record.id is not None


def test_decrypt_density_is_persisted_for_empty_repair_and_failure_paths(
    worker_db, rsa_keys, monkeypatch
) -> None:
    """The worker's real writer is counted in its SQLite audit sink."""
    from app.services import decrypt_worker
    from app.services.decrypt_worker import run_decrypt_once
    from tests.fakes import FakeWecomSdk, _TENANT_A, insert_archive_message, rsa_encrypt_key_b64

    worker_db.execute(text("""
        CREATE TABLE audit_logs (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, admin_user_id TEXT,
            action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT,
            detail JSON, created_at DATETIME NOT NULL
        )
    """))
    worker_db.commit()
    private_key, _public_key = rsa_keys
    assert worker_db.query(AuditLog).count() == 0
    run_decrypt_once(worker_db, _TENANT_A, "fake", private_key, 11, sdk=FakeWecomSdk())
    assert worker_db.query(AuditLog).count() == 0

    monkeypatch.setattr(decrypt_worker, "repair_missing_recipients", lambda *_: 1)
    run_decrypt_once(worker_db, _TENANT_A, "fake", private_key, 11, sdk=FakeWecomSdk())
    assert worker_db.query(AuditLog).filter_by(action=AuditAction.DECRYPT_COMPLETED).count() == 1

    monkeypatch.setattr(decrypt_worker, "repair_missing_recipients", lambda *_: 0)
    monkeypatch.setattr(decrypt_worker, "reconcile_pending_revocations", lambda *_: 1)
    run_decrypt_once(worker_db, _TENANT_A, "fake", private_key, 11, sdk=FakeWecomSdk())
    assert worker_db.query(AuditLog).filter_by(action=AuditAction.DECRYPT_COMPLETED).count() == 2

    monkeypatch.setattr(decrypt_worker, "reconcile_pending_revocations", lambda *_: 0)
    insert_archive_message(
        worker_db, tenant_id=_TENANT_A, decrypt_status="pending",
        encrypt_random_key="", encrypt_chat_msg="", seq=101,
    )
    run_decrypt_once(worker_db, _TENANT_A, "fake", private_key, 11, sdk=FakeWecomSdk())
    rows = worker_db.query(AuditLog).filter_by(action=AuditAction.DECRYPT_COMPLETED).all()
    assert len(rows) == 3
    assert rows[-1].detail["failed"] == 1

    private_key, public_key = rsa_keys
    insert_archive_message(
        worker_db, tenant_id=_TENANT_A, decrypt_status="pending", publickey_ver=11,
        encrypt_random_key=rsa_encrypt_key_b64(public_key, "symmetric"),
        encrypt_chat_msg="success-payload", seq=102,
    )
    sdk = FakeWecomSdk()
    sdk.set_decrypt_response("success-payload", {
        "msgtype": "text", "from": "staff", "tolist": [], "roomid": "",
        "msgtime": 1, "text": {"content": "message-text-decoy"},
    })
    run_decrypt_once(worker_db, _TENANT_A, "fake", private_key, 11, sdk=sdk)
    success_rows = worker_db.query(AuditLog).filter_by(action=AuditAction.DECRYPT_COMPLETED).all()
    assert len(success_rows) == 4
    assert success_rows[-1].detail["success"] == 1
    assert success_rows[-1].detail["scanned"] >= 1


def test_decrypt_repair_and_failure_branches_emit_at_most_one_safe_event(
    worker_db, rsa_keys, monkeypatch
) -> None:
    from app.services import decrypt_worker
    from app.services.decrypt_worker import run_decrypt_once
    from tests.fakes import FakeWecomSdk, _TENANT_A, insert_archive_message

    private_key, _public_key = rsa_keys
    writer = MagicMock()
    monkeypatch.setattr(decrypt_worker, "write_audit", writer)
    monkeypatch.setattr(decrypt_worker, "repair_missing_recipients", lambda *_: 1)
    monkeypatch.setattr(decrypt_worker, "reconcile_pending_revocations", lambda *_: 0)

    # Repair work alone is reportable even with scanned=0.
    run_decrypt_once(worker_db, _TENANT_A, "fake", private_key, 9, sdk=FakeWecomSdk())
    writer.assert_called_once()
    assert writer.call_args.kwargs["detail"]["recipients_repaired"] == 1

    writer.reset_mock()
    monkeypatch.setattr(decrypt_worker, "repair_missing_recipients", lambda *_: 0)
    insert_archive_message(
        worker_db, tenant_id=_TENANT_A, decrypt_status="pending",
        encrypt_random_key="", encrypt_chat_msg="", seq=100,
    )
    run_decrypt_once(worker_db, _TENANT_A, "fake", private_key, 9, sdk=FakeWecomSdk())
    writer.assert_called_once()
    detail = writer.call_args.kwargs["detail"]
    assert detail["scanned"] == detail["failed"] == 1
    assert set(detail) <= {
        "publickey_ver", "scanned", "success", "failed", "unsupported",
        "recipients_repaired", "recipient_upsert_failed", "revocations_reconciled",
        "revoke_reconcile_failed", "key_mismatch", "rsa_failed", "sigsegv",
        "isolation_other", "malformed_input",
    }


def test_new_detail_shapes_are_allowlisted_and_free_of_sensitive_fields() -> None:
    for detail in (
        {"changed_keys": ["smtp_host", "smtp_port"]},
        {"old": {"retention_days": 30, "is_locked": False},
         "new": {"retention_days": 365, "is_locked": True}},
        {"platform_admin_id": "platform-rnd335", "previous_is_active": True, "is_active": False},
        {"publickey_ver": 7, "scanned": 1, "failed": 0},
        {"locked_count": 1, "cutoff": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()},
    ):
        _assert_safe_detail(detail)
