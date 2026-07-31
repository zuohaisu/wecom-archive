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
    from app.db.models import Base, PlatformAdmin as PAModel, Tenant as TModel, TenantWecomConfig as TWModel
    
    Base.metadata.create_all(bind=engine, tables=[PAModel.__table__, TModel.__table__, TWModel.__table__])
    
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


def _create_tenant(db: Session, is_active: bool = True):
    from app.db.models import Tenant, TenantWecomConfig
    
    tenant = Tenant(id=str(uuid4()), name=f"Test Corp {uuid4().hex[:8]}", slug=f"test-corp-{uuid4().hex[:8]}", is_active=is_active)
    config = TenantWecomConfig(id=str(uuid4()), tenant_id=tenant.id, corp_id=f"ww{uuid4().hex[:8]}", agent_id=str(uuid4().int % 100000000), is_active=True)
    config.set_credentials(_TEST_SECRET, _TEST_PRIVATE_KEY_PEM)
    
    db.add(tenant)
    db.add(config)
    db.commit()
    db.refresh(tenant)
    return tenant, config


class TestTenantActivationEndpoint:
    def test_deactivate_tenant(self, client, db, platform_admin):
        """AC-1: Deactivating tenant sets is_active=false."""
        tenant, _ = _create_tenant(db)
        resp = client.patch(f"/api/platform/tenants/{tenant.id}", json={"is_active": False}, headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tenant_is_active"] is False

    def test_activate_tenant(self, client, db, platform_admin):
        """AC-1: Activating tenant sets is_active=true."""
        tenant, _ = _create_tenant(db, is_active=False)
        resp = client.patch(f"/api/platform/tenants/{tenant.id}", json={"is_active": True}, headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"})
        assert resp.status_code == 200
        assert resp.json()["tenant_is_active"] is True

    def test_not_found_404(self, client, platform_admin):
        """AC-3: Non-existent tenant returns 404."""
        resp = client.patch("/api/platform/tenants/nonexistent-id", json={"is_active": False}, headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"})
        assert resp.status_code == 404

    def test_no_auth_401(self, client):
        """AC-4: Missing auth returns 401."""
        resp = client.patch("/api/platform/tenants/some-id", json={"is_active": False})
        assert resp.status_code == 401

    def test_audit_written_on_change(self, client, db, platform_admin):
        """AC-2: Audit log is written when tenant status changes."""
        tenant, _ = _create_tenant(db)
        initial_count = db.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()
        resp = client.patch(f"/api/platform/tenants/{tenant.id}", json={"is_active": False}, headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"})
        assert resp.status_code == 200
        new_count = db.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()
        assert new_count == initial_count + 1

    def test_audit_has_correct_action(self, client, db, platform_admin):
        """AC-2: Audit contains correct action type."""
        tenant, _ = _create_tenant(db)
        resp = client.patch(f"/api/platform/tenants/{tenant.id}", json={"is_active": False}, headers={"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"})
        assert resp.status_code == 200
        result = db.execute(text("SELECT action FROM audit_logs WHERE object_id = :tid ORDER BY created_at DESC LIMIT 1"), {"tid": tenant.id}).fetchone()
        assert result is not None
        assert result[0] == "platform.tenant_deactivated"
