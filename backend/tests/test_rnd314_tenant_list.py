"""RND-314: GET /tenants list endpoint QA suite."""

from __future__ import annotations

import base64
import json
import os
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import PlatformAdmin, Tenant, TenantWecomConfig


@pytest.fixture(autouse=True)
def setup_field_encryption():
    """Ensure FIELD_ENCRYPTION_KEY is set for crypto operations."""
    if "FIELD_ENCRYPTION_KEY" not in os.environ:
        os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
    yield


# Test data constants
_TEST_SECRET = "test-secret-rnd314"
_TEST_PRIVATE_KEY_PEM = "-----BEGIN PRIVATE KEY-----\ntest-key-rnd314\n-----END PRIVATE KEY-----"

# Mock platform admin credentials (email:password) - matches test_rnd311 pattern
PLATFORM_ADMIN_EMAIL = "platform@example.test"
PLATFORM_ADMIN_PASSWORD = "test-password"
_BASIC_AUTH_TOKEN = base64.b64encode(f"{PLATFORM_ADMIN_EMAIL}:{PLATFORM_ADMIN_PASSWORD}".encode()).decode()


def _get_headers(admin=None):
    """Get request headers with Basic auth or X-Auth session."""
    if admin is None:
        return {}
    return {"Authorization": f"Basic {_BASIC_AUTH_TOKEN}"}


def _create_test_tenant(db_session: Session, corp_id: str, agent_id: str) -> Tenant:
    """Helper to create a tenant with valid WecomConfig."""
    from uuid import uuid4

    tenant = Tenant(id=str(uuid4()), name="Test Tenant", slug=f"test-{uuid4().hex[:8]}")
    config = TenantWecomConfig(
        id=str(uuid4()),
        tenant_id=tenant.id,
        corp_id=corp_id,
        agent_id=agent_id,
        is_active=True,
    )
    config.set_credentials(_TEST_SECRET, _TEST_PRIVATE_KEY_PEM)
    db_session.add(tenant)
    db_session.add(config)
    db_session.commit()
    db_session.refresh(tenant)
    return tenant


def test_tenants_endpoint_returns_list(client: TestClient, platform_admin_user):
    """AC-1: Endpoint returns a list with correct fields."""
    resp = client.get("/api/platform/tenants", headers=_get_headers(platform_admin_user))
    assert resp.status_code == 200
    data = resp.json()
    assert "tenants" in data
    assert isinstance(data["tenants"], list)


def test_tenants_empty_list_when_no_tenants(client: TestClient, platform_admin_user, db_session):
    """AC-4: Empty tenant list when no tenants provisioned."""
    # Ensure no tenants exist
    db_session.query(Tenant).delete()
    db_session.query(TenantWecomConfig).delete()
    db_session.commit()

    resp = client.get("/api/platform/tenants", headers=_get_headers(platform_admin_user))
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenants"] == []


def test_tenants_includes_correct_fields(client: TestClient, platform_admin_user, db_session):
    """AC-1: Response contains all required fields (no extra keys)."""
    tenant = _create_test_tenant(db_session, "ww1234567890abcdef", "1000001")
    # Remove is_active override - it's not needed for this test

    resp = client.get("/api/platform/tenants", headers=_get_headers(platform_admin_user))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tenants"]) == 1
    item = data["tenants"][0]

    # Check all required fields exist
    required_keys = {
        "tenant_id",
        "tenant_name",
        "tenant_slug",
        "corp_id",
        "agent_id",
        "tenant_is_active",
        "config_is_active",
        "created_at",
    }
    assert set(item.keys()) == required_keys

    # Validate field values
    assert item["tenant_id"] == tenant.id
    assert item["tenant_name"] == tenant.name
    assert item["tenant_slug"] == tenant.slug
    assert item["corp_id"] == "ww1234567890abcdef"
    assert item["agent_id"] == "1000001"
    assert item["tenant_is_active"] is True
    assert item["config_is_active"] is True
    assert "created_at" in item


def test_tenants_ac2_no_key_leakage(client: TestClient, platform_admin_user, db_session):
    """AC-2: Zero key leakage - response must NOT contain any secret fields."""
    _create_test_tenant(db_session, "ww9999999999999999", "9999999")

    resp = client.get("/api/platform/tenants", headers=_get_headers(platform_admin_user))
    assert resp.status_code == 200
    json_str = resp.json()

    # JSON serialization check
    forbidden_strings = {
        "app_secret",
        "private_key_encrypted",
        "decrypted_app_secret",
        "decrypted_private_key",
    }
    json_dump = json.dumps(json_str)

    for forbidden in forbidden_strings:
        assert (
            forbidden not in json_dump
        ), f"Security violation: response contains forbidden string '{forbidden}'"


def test_tenants_auth_missing_credentials(client: TestClient):
    """AC-3: No credentials → 401."""
    resp = client.get("/api/platform/tenants")
    assert resp.status_code == 401


def test_tenants_auth_invalid_session(client: TestClient):
    """AC-3: Invalid session → 401."""
    resp = client.get(
        "/api/platform/tenants", headers={"X-Auth": json.dumps({"sub": "invalid"})}
    )
    assert resp.status_code == 401


def test_tenants_different_active_status(client: TestClient, platform_admin_user, db_session):
    """Verify tenant_is_active and config_is_active are tracked independently."""
    tenant = Tenant(id=str(uuid4()), name="Mixed Status", slug="mixed-status", is_active=False)
    config = TenantWecomConfig(
        id=str(uuid4()),
        tenant_id=tenant.id,
        corp_id="ww0000000000000000",
        agent_id="0000000",
        is_active=True,
    )
    config.set_credentials(_TEST_SECRET, _TEST_PRIVATE_KEY_PEM)
    db_session.add(tenant)
    db_session.add(config)
    db_session.commit()

    resp = client.get("/api/platform/tenants", headers=_get_headers(platform_admin_user))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tenants"]) >= 1
    item = next((t for t in data["tenants"] if t["tenant_slug"] == "mixed-status"), None)
    assert item is not None
    assert item["tenant_is_active"] is False
    assert item["config_is_active"] is True
