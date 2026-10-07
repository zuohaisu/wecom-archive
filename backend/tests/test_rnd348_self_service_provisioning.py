from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AdminLoginIdentity,
    AdminSession,
    AdminUser,
    AuditLog,
    PlatformAdmin,
    Tenant,
    TenantActivationCheck,
    TenantWecomConfig,
    ThirdPartyOrganizationBinding,
    WecomAuthorizationAttempt,
    WecomAuthorizationProof,
    WecomOrganizationClaim,
)
from app.db.session import get_db
from app.main import create_app
from app.routers import wecom_org_authorization
from app.services.organization_provisioning import provision_organization
from app.services.wecom_org_authorization import AuthorizedOrganization


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


class FakeProvider:
    def __init__(self):
        self.state = ""

    def build_install_url(self, state: str) -> str:
        self.state = state
        return f"https://provider.invalid/install?state={state}"

    def exchange(self, authorization_code: str) -> AuthorizedOrganization:
        return AuthorizedOrganization(
            "ww-provisioning-private",
            "官方企业名称",
            "verified-management-admin",
            "1000009",
            "private-permanent-code",
        )


def _setup(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [
        Tenant.__table__,
        TenantWecomConfig.__table__,
        AdminUser.__table__,
        AdminLoginIdentity.__table__,
        AdminSession.__table__,
        AuditLog.__table__,
        PlatformAdmin.__table__,
        TenantActivationCheck.__table__,
        ThirdPartyOrganizationBinding.__table__,
        WecomAuthorizationAttempt.__table__,
        WecomAuthorizationProof.__table__,
        WecomOrganizationClaim.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    provider = FakeProvider()
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(
        wecom_org_authorization,
        "get_wecom_org_authorization_provider",
        lambda _db: provider,
    )
    return TestClient(app), factory, provider


def _claim(client: TestClient, provider: FakeProvider) -> str:
    client.get("/api/auth/wecom/third-party/install", follow_redirects=False)
    client.get(
        "/api/auth/wecom/third-party/callback",
        params={"code": "one-use", "state": provider.state},
        follow_redirects=False,
    )
    raw_claim = client.cookies.get("wecom_org_claim")
    assert raw_claim
    return raw_claim


def test_atomic_provisioning_creates_exact_minimum_and_never_activates(monkeypatch):
    client, factory, provider = _setup(monkeypatch)
    raw_claim = _claim(client, provider)
    response = client.post(
        "/api/auth/wecom/organization-claim/confirm", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/provisioning"
    cookie = response.headers["set-cookie"]
    for private in ("ww-provisioning-private", "verified-management-admin", "private-permanent-code"):
        assert private not in cookie
        assert private not in response.headers["location"]

    with factory() as db:
        tenant = db.query(Tenant).one()
        assert tenant.name == "官方企业名称"
        assert tenant.lifecycle_status == "provisioning"
        assert "ww-provisioning-private" not in tenant.slug
        binding = db.query(ThirdPartyOrganizationBinding).one()
        assert binding.tenant_id == tenant.id
        assert binding.permanent_code_encrypted != "private-permanent-code"
        assert db.query(TenantWecomConfig).count() == 0
        owner = db.query(AdminUser).one()
        assert owner.role == "owner" and owner.status == "active"
        identity = db.query(AdminLoginIdentity).one()
        assert identity.provider == "wecom_third_party"
        assert identity.subject == "verified-management-admin"
        session = db.query(AdminSession).one()
        assert len(session.id) == 36
        assert session.session_scope == "provisioning"
        claim = db.query(WecomOrganizationClaim).one()
        assert claim.state == "consumed" and claim.provisioned_tenant_id == tenant.id
        audit = db.query(AuditLog).one()
        assert audit.action == "organization.provisioned"
        assert audit.detail == {"lifecycle_status": "provisioning"}

        again = provision_organization(db, raw_claim)
        assert again.tenant_id == tenant.id
        assert db.query(Tenant).count() == 1
        assert db.query(AdminUser).count() == 1
        assert db.query(AdminSession).count() == 1


def test_provisioning_session_can_only_reach_waiting_and_settings_surface(monkeypatch):
    client, _, provider = _setup(monkeypatch)
    _claim(client, provider)
    client.post("/api/auth/wecom/organization-claim/confirm", follow_redirects=False)
    login = client.get("/admin/login", follow_redirects=False)
    assert login.status_code == 302
    assert login.headers["location"] == "/admin/provisioning"
    assert client.get("/admin/provisioning").status_code == 200
    assert client.get("/admin/provisioning/settings").status_code == 200
    assert client.get("/admin/billing").status_code == 200
    status = client.get("/api/provisioning/status")
    assert status.status_code == 200
    assert status.json() == {
        "lifecycle_status": "provisioning",
        "archive_enabled": False,
        "activation": {
            "state": "not_started",
            "gate_results": {},
            "safe_error_code": None,
            "revision": 0,
        },
        "allowed_actions": ["view_status", "purchase_plan", "view_settings"],
    }
    assert client.get("/api/admin/settings").status_code == 401
    assert client.get("/api/admin/sync-status").status_code == 401
    assert client.get("/api/messages").status_code == 401
    assert (
        client.post(
            "/api/admin/export/execute",
            json={"approval_token": "not-used", "params": {}},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/admin/users/invite",
            json={"email": "blocked@example.test", "role": "admin"},
        ).status_code
        == 401
    )
    assert client.get("/dashboard", follow_redirects=False).status_code == 302


def test_platform_lists_tenant_but_legacy_boolean_cannot_bypass_activation_gates(monkeypatch):
    from app.auth import hash_password

    client, factory, provider = _setup(monkeypatch)
    _claim(client, provider)
    client.post("/api/auth/wecom/organization-claim/confirm", follow_redirects=False)

    platform_email = "platform-rnd348@example.test"
    platform_password = "test-password"
    with factory() as db:
        tenant_id = db.query(Tenant.id).scalar()
        db.add(
            PlatformAdmin(
                id="rnd348-platform-admin",
                email=platform_email,
                password_hash=hash_password(platform_password),
                role="superadmin",
                status="active",
            )
        )
        db.commit()

    basic = base64.b64encode(
        f"{platform_email}:{platform_password}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {basic}"}
    listed = client.get("/api/platform/tenants", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["tenants"] == [
        {
            "tenant_id": tenant_id,
            "tenant_name": "官方企业名称",
            "tenant_slug": listed.json()["tenants"][0]["tenant_slug"],
            "corp_id": None,
            "agent_id": None,
            "tenant_is_active": False,
            "config_is_active": False,
            "created_at": listed.json()["tenants"][0]["created_at"],
        }
    ]

    activated = client.patch(
        f"/api/platform/tenants/{tenant_id}",
        json={"is_active": True},
        headers=headers,
    )
    assert activated.status_code == 200
    assert activated.json()["tenant_is_active"] is False

    with factory() as db:
        tenant = db.get(Tenant, tenant_id)
        session = db.query(AdminSession).one()
        assert tenant.lifecycle_status == "provisioning"
        assert session.session_scope == "provisioning"
        assert (
            db.query(AuditLog)
            .filter(AuditLog.action == "platform.tenant_activated")
            .count()
            == 0
        )


def test_any_failure_rolls_back_tenant_owner_session_binding_and_audit(monkeypatch):
    client, factory, provider = _setup(monkeypatch)
    raw_claim = _claim(client, provider)
    with factory() as db:
        def fail_before_flush(_session, _context, _instances):
            if any(isinstance(item, AdminSession) for item in _session.new):
                raise RuntimeError("injected transaction failure")

        event.listen(db, "before_flush", fail_before_flush)
        with pytest.raises(RuntimeError, match="injected transaction failure"):
            provision_organization(db, raw_claim)
        db.rollback()
    with factory() as db:
        assert db.query(Tenant).count() == 0
        assert db.query(ThirdPartyOrganizationBinding).count() == 0
        assert db.query(AdminUser).count() == 0
        assert db.query(AdminLoginIdentity).count() == 0
        assert db.query(AdminSession).count() == 0
        assert db.query(AuditLog).count() == 0
        assert db.query(WecomOrganizationClaim).one().state == "pending"


def test_migration_maps_existing_lifecycle_and_worker_requires_tenant_context():
    backend_root = Path(__file__).resolve().parents[1]
    migration = (
        backend_root / "alembic/versions/0040_rnd348_self_service_provisioning.py"
    ).read_text()
    assert "CASE WHEN is_active THEN 'active' ELSE 'suspended' END" in migration
    assert "'provisioning', 'active', 'suspended'" in migration
    worker = (backend_root / "scripts/sync_wecom_archive_once.py").read_text()
    assert "WECOM_TENANT_ID" in worker
    assert "WECOM_CORP_ID" not in worker
    assert "WECOM_ARCHIVE_SECRET" not in worker
    assert "ThirdPartyOrganizationBinding" not in worker
