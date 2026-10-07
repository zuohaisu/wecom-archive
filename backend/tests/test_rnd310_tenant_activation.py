"""RND-310: Tenant activation/deactivation endpoint tests."""

from __future__ import annotations

import base64
import os
import tempfile
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

_db_file = tempfile.mktemp(suffix=".db")
engine = create_engine(f"sqlite:///{_db_file}", connect_args={"check_same_thread": False})
os.environ.setdefault('FIELD_ENCRYPTION_KEY', Fernet.generate_key().decode())


@pytest.fixture(scope="module", autouse=True)
def setup_database():
    from app.db.models import (
        AuditLog,
        Base,
        PlatformAdmin as PAModel,
        Tenant as TModel,
        TenantActivationCheck,
        TenantWecomConfig as TWModel,
        ThirdPartyOrganizationBinding,
    )

    Base.metadata.create_all(
        bind=engine,
        tables=[
            PAModel.__table__,
            TModel.__table__,
            TWModel.__table__,
            TenantActivationCheck.__table__,
            ThirdPartyOrganizationBinding.__table__,
            AuditLog.__table__,
        ],
    )
    
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, admin_user_id TEXT,
            action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT,
            detail TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""))
    
    yield
    
    if os.path.exists(_db_file):
        os.remove(_db_file)


_TEST_SECRET = "test-secret-rnd310"
_TEST_PRIVATE_KEY_PEM = "-----BEGIN PRIVATE KEY-----\ntest-key-rnd310\n-----END PRIVATE KEY-----"
_BASIC_AUTH_TOKEN = base64.b64encode(b"platform@example.test:test-password").decode()


@pytest.fixture
def db():
    return sessionmaker(bind=engine)()


@pytest.fixture
def client(db):
    from app.main import app
    from app.db.session import get_db
    
    def override_db():
        yield db
        
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def platform_admin(db):
    from app.auth import hash_password
    from app.db.models import PlatformAdmin as PAModel
    
    email = "platform@example.test"
    existing = db.query(PAModel).filter(PAModel.email == email).first()
    if existing:
        return existing
    
    pa = PAModel(id=str(uuid4()), email=email, password_hash=hash_password("test-password"), role="superadmin", status="active")
    db.add(pa)
    db.commit()
    return pa


def _create_tenant(db: Session, lifecycle_status: str = "active"):
    from app.db.models import Tenant, TenantWecomConfig

    tenant = Tenant(
        id=str(uuid4()),
        name=f"Test Corp {uuid4().hex[:8]}",
        slug=f"test-corp-{uuid4().hex[:8]}",
        lifecycle_status=lifecycle_status,
    )
    config = TenantWecomConfig(
        id=str(uuid4()),
        tenant_id=tenant.id,
        corp_id=f"ww{uuid4().hex[:8]}",
        agent_id=str(uuid4().int % 100000000),
        is_active=True,
    )
    config.set_credentials(_TEST_SECRET, _TEST_PRIVATE_KEY_PEM)

    db.add(tenant)
    db.add(config)
    db.commit()
    db.refresh(tenant)
    return tenant, config


class TestTenantActivationEndpoint:
    def test_legacy_false_suspends_active_tenant(self, client, db, platform_admin):
        tenant, _ = _create_tenant(db)
        response = client.patch(
            f"/api/platform/tenants/{tenant.id}",
            json={"is_active": False},
            headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"},
        )

        assert response.status_code == 200
        assert response.json()["tenant_is_active"] is False
        db.refresh(tenant)
        assert tenant.lifecycle_status == "suspended"

    def test_legacy_true_cannot_bypass_provisioning_gates(
        self, client, db, platform_admin
    ):
        tenant, _ = _create_tenant(db, lifecycle_status="provisioning")
        response = client.patch(
            f"/api/platform/tenants/{tenant.id}",
            json={"is_active": True},
            headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"},
        )

        assert response.status_code == 200
        assert response.json()["tenant_is_active"] is False
        db.refresh(tenant)
        assert tenant.lifecycle_status == "provisioning"

    @pytest.mark.parametrize("lifecycle_status", ["frozen", "suspended"])
    def test_legacy_true_cannot_bypass_freeze_or_suspension(
        self, client, db, platform_admin, lifecycle_status
    ):
        tenant, _ = _create_tenant(db, lifecycle_status=lifecycle_status)
        response = client.patch(
            f"/api/platform/tenants/{tenant.id}",
            json={"is_active": True},
            headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"},
        )

        assert response.status_code == 409
        db.refresh(tenant)
        assert tenant.lifecycle_status == lifecycle_status

    def test_not_found_404(self, client, platform_admin):
        response = client.patch(
            "/api/platform/tenants/nonexistent-id",
            json={"is_active": False},
            headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"},
        )
        assert response.status_code == 404

    def test_no_auth_401(self, client):
        response = client.patch(
            "/api/platform/tenants/some-id", json={"is_active": False}
        )
        assert response.status_code == 401

    def test_legacy_deactivation_audits_lifecycle_suspend(
        self, client, db, platform_admin
    ):
        tenant, _ = _create_tenant(db)
        initial_count = db.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()
        response = client.patch(
            f"/api/platform/tenants/{tenant.id}",
            json={"is_active": False},
            headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"},
        )

        assert response.status_code == 200
        new_count = db.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()
        assert new_count == initial_count + 1
        action = db.execute(
            text(
                "SELECT action FROM audit_logs WHERE object_id = :tenant_id "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"tenant_id": tenant.id},
        ).scalar_one()
        assert action == "platform.tenant_suspended"
