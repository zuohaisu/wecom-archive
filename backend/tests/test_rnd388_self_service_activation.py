"""Offline acceptance coverage for RND-388 self-service activation.

Covers the persisted activation-check state machine, the five gates, the
RND-394 trial wiring, idempotency (replay short-circuit), recoverability and
the platform override.  No live network: get_wecom_token is monkeypatched.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from app.crypto import encrypt_value
from app.db.base import Base
from app.db.models import (
    AdminSession,
    AdminUser,
    AuditLog,
    BillingPlan,
    PlanEntitlement,
    PlatformAdmin,
    Subscription,
    SubscriptionHistory,
    Tenant,
    TenantActivationCheck,
    TenantWecomConfig,
    ThirdPartyOrganizationBinding,
)
from app.db.session import get_db
from app.services.entitlements import ANNUAL_PLAN_CODE, ARCHIVE_ACCESS
from app.services.trial_subscriptions import TRIAL_SOURCE


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


_RSA_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_RSA_PEM = _RSA_PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
_ARCHIVE_SECRET = "rnd388-archive-secret-never-in-response"
_CALLBACK_TOKEN = "rnd388-callback-token"
_CALLBACK_AES_KEY = base64.b64encode(os.urandom(32)).decode().rstrip("=")


@pytest.fixture()
def activation_client(
    monkeypatch: pytest.MonkeyPatch,
):
    """Provisioning tenant + owner session + billing plan, ready to gate."""
    from app.main import create_app

    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantWecomConfig.__table__,
            TenantActivationCheck.__table__,
            ThirdPartyOrganizationBinding.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            AuditLog.__table__,
            BillingPlan.__table__,
            PlanEntitlement.__table__,
            Subscription.__table__,
            SubscriptionHistory.__table__,
            PlatformAdmin.__table__,
        ],
    )
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    tenant = Tenant(
        id="tenant-rnd388",
        name="测试企业",
        slug="rnd-388",
        is_active=False,
        lifecycle_status="provisioning",
    )
    binding = ThirdPartyOrganizationBinding(
        id="binding-rnd388",
        tenant_id=tenant.id,
        corp_id="ww-rnd388-corp",
        agent_id="1000388",
        permanent_code_encrypted=encrypt_value("rnd388-permanent-code"),
        authorization_mode="admin",
    )
    owner = AdminUser(
        id="user-rnd388",
        tenant_id=tenant.id,
        wecom_user_id="owner-rnd388",
        role="owner",
        status="active",
    )
    session = AdminSession(
        id="session-rnd388",
        admin_user_id=owner.id,
        tenant_id=tenant.id,
        wecom_user_id=owner.wecom_user_id,
        session_scope="provisioning",
        is_revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
    )
    db.add_all([tenant, binding, owner, session])
    db.add(
        BillingPlan(
            id="annual-plan",
            code=ANNUAL_PLAN_CODE,
            display_name="年度基础套餐",
            is_active=True,
            amount_cents=9900,
            currency="CNY",
            billing_period_months=12,
            storage_quota_bytes=5 * 1024**3,
        )
    )
    db.add(
        PlanEntitlement(
            id="archive-access",
            plan_id="annual-plan",
            capability=ARCHIVE_ACCESS,
            is_enabled=True,
        )
    )
    db.commit()

    app = create_app()

    def override_db():
        request_db = session_factory()
        try:
            yield request_db
        finally:
            request_db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def _cookies() -> dict[str, str]:
    return {"session_id": "session-rnd388"}


def _complete_config(db: Session, tenant_id: str) -> None:
    config = TenantWecomConfig(
        id="config-rnd388",
        tenant_id=tenant_id,
        corp_id="ww-rnd388-corp",
        agent_id="1000388",
        callback_domain="archive.example.test",
        is_active=True,
    )
    config.set_credentials(_ARCHIVE_SECRET, _RSA_PEM)
    config.set_callback_credentials(_CALLBACK_TOKEN, _CALLBACK_AES_KEY)
    config.publickey_version = 1
    db.add(config)
    db.commit()


def _enable_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/sdk/lib")


def _ok_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.auth.get_wecom_token", Mock(return_value="rnd388-access-token")
    )


def _fail_token(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    monkeypatch.setattr(
        "app.auth.get_wecom_token", Mock(side_effect=RuntimeError(message))
    )


def _capture_dispatch(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    def fake_dispatch(trigger_source: str, tenant_id: str | None = None):
        calls.append((trigger_source, tenant_id))
        return None

    monkeypatch.setattr(
        "app.routers.provisioning.dispatch_archive_worker", fake_dispatch
    )
    return calls


def _capture_spawn(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    def fake_spawn(db_bind, tenant_id: str, *, actor: str):
        calls.append((tenant_id, actor))

    monkeypatch.setattr(
        "app.routers.provisioning.spawn_activation_worker", fake_spawn
    )
    return calls


# ---------------------------------------------------------------------------
# All gates pass → trial + promotion
# ---------------------------------------------------------------------------


def test_all_gates_pass_activates_promotes_and_grants_trial(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)
    dispatches = _capture_dispatch(monkeypatch)

    response = client.post("/api/provisioning/activate", cookies=_cookies())
    assert response.status_code == 200
    data = response.json()
    assert data["activated"] is True
    assert data["replayed"] is False
    assert data["activation"]["state"] == "ready"
    assert set(data["activation"]["gate_results"]) == {
        "binding",
        "config",
        "connectivity",
        "subscription",
        "runtime",
    }
    assert all(
        result["ok"] for result in data["activation"]["gate_results"].values()
    )
    assert data["activation"]["revision"] == 1
    assert data["worker_dispatch"] is None

    tenant = db.get(Tenant, "tenant-rnd388")
    assert tenant.lifecycle_status == "active"
    assert tenant.is_active is True
    assert tenant.onboarding_completed_at is not None
    assert db.query(AdminSession).one().session_scope == "admin"

    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "platform.tenant_activated")
        .one()
    )
    assert audit.detail["actor"] == "self_service"
    assert audit.detail["promoted_provisioning_sessions"] == 1
    assert set(audit.detail["gate_results"]) == {
        "binding",
        "config",
        "connectivity",
        "subscription",
        "runtime",
    }

    check = db.query(TenantActivationCheck).one()
    assert check.state == "ready"
    assert check.revision == 1

    subscription = db.query(Subscription).one()
    assert subscription.status == "trial"
    assert subscription.source == TRIAL_SOURCE

    # The worker was dispatched exactly once, after the promotion commit.
    assert dispatches == [("activation", "tenant-rnd388")]

    # The promoted session can no longer reach the provisioning surface: the
    # session is admin-scoped now, so the provisioning guard answers 401 and
    # /admin/login bounces the still-valid session to /dashboard.
    assert client.get("/api/provisioning/status", cookies=_cookies()).status_code == 401
    assert (
        client.post("/api/provisioning/activate", cookies=_cookies()).status_code
        == 401
    )
    login = client.get("/admin/login", cookies=_cookies(), follow_redirects=False)
    assert login.status_code == 302
    assert login.headers["location"] == "/dashboard"


def test_replay_is_idempotent_with_no_double_side_effects(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.tenant_activation import activate_tenant

    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)

    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    with factory() as first:
        result = activate_tenant(first, "tenant-rnd388", actor="self_service")
        assert result.activated is True
    with factory() as replay:
        result = activate_tenant(replay, "tenant-rnd388", actor="self_service")
        assert result.activated is False
        assert result.replayed is True
        assert result.snapshot.state == "active"

    with factory() as verify:
        assert (
            verify.query(AuditLog)
            .filter(AuditLog.action == "platform.tenant_activated")
            .count()
            == 1
        )
        assert verify.query(Subscription).count() == 1
        assert verify.query(TenantActivationCheck).one().revision == 1
        assert verify.query(AdminSession).one().session_scope == "admin"


# ---------------------------------------------------------------------------
# Gate failures → blocked + safe code, stays provisioning
# ---------------------------------------------------------------------------


def test_missing_binding_blocks_and_stops_gate_sequence(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)
    db.query(ThirdPartyOrganizationBinding).delete()
    db.commit()

    response = client.post("/api/provisioning/activate", cookies=_cookies())
    assert response.status_code == 200
    activation = response.json()["activation"]
    assert activation["state"] == "blocked"
    assert activation["safe_error_code"] == "missing_binding"
    assert activation["gate_results"]["binding"] == {
        "ok": False,
        "safe_error_code": "missing_binding",
    }
    # Gates after the first failure stay absent (UI renders them skipped).
    assert "config" not in activation["gate_results"]

    tenant = db.get(Tenant, "tenant-rnd388")
    assert tenant.lifecycle_status == "provisioning"
    assert db.query(Subscription).count() == 0
    assert db.query(AdminSession).one().session_scope == "provisioning"
    check = db.query(TenantActivationCheck).one()
    assert check.state == "blocked"
    assert check.safe_error_code == "missing_binding"
    assert check.revision == 1


def test_incomplete_config_blocks_with_config_incomplete(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)

    response = client.post("/api/provisioning/activate", cookies=_cookies())
    activation = response.json()["activation"]
    assert activation["state"] == "blocked"
    assert activation["safe_error_code"] == "config_incomplete"
    assert activation["gate_results"]["config"]["ok"] is False
    assert "connectivity" not in activation["gate_results"]
    assert db.get(Tenant, "tenant-rnd388").lifecycle_status == "provisioning"


def test_unreadable_stored_config_blocks_with_config_not_decryptable(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)
    config = db.query(TenantWecomConfig).one()
    config.app_secret = "not-valid-fernet-ciphertext"
    db.commit()

    response = client.post("/api/provisioning/activate", cookies=_cookies())
    activation = response.json()["activation"]
    assert activation["safe_error_code"] == "config_not_decryptable"


def test_invalid_credentials_from_connectivity_probe_blocks(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    _enable_runtime(monkeypatch)
    _fail_token(monkeypatch, "WeCom gettoken failed: errcode=40001")

    response = client.post("/api/provisioning/activate", cookies=_cookies())
    activation = response.json()["activation"]
    assert activation["state"] == "blocked"
    assert activation["safe_error_code"] == "credentials_invalid"
    assert activation["gate_results"]["connectivity"]["ok"] is False
    assert "subscription" not in activation["gate_results"]
    assert db.query(Subscription).count() == 0
    assert db.get(Tenant, "tenant-rnd388").lifecycle_status == "provisioning"


def test_network_failure_blocks_with_connectivity_failed(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    _enable_runtime(monkeypatch)
    _fail_token(monkeypatch, "Failed to fetch WeCom access_token")

    response = client.post("/api/provisioning/activate", cookies=_cookies())
    activation = response.json()["activation"]
    assert activation["safe_error_code"] == "connectivity_failed"
    assert activation["gate_results"]["connectivity"]["ok"] is False


def test_missing_runtime_blocks_with_runtime_unavailable(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "")
    _ok_token(monkeypatch)

    response = client.post("/api/provisioning/activate", cookies=_cookies())
    activation = response.json()["activation"]
    assert activation["state"] == "blocked"
    assert activation["safe_error_code"] == "runtime_unavailable"
    assert activation["gate_results"]["runtime"]["ok"] is False
    # Every earlier gate passed.
    assert all(
        activation["gate_results"][gate]["ok"]
        for gate in ("binding", "config", "connectivity", "subscription")
    )
    # No trial yet: the RND-394 grant happens only on a fully-passing
    # evaluation, so a tenant blocked at runtime stays subscription-less and
    # the later successful evaluation grants exactly once.
    assert db.query(Subscription).count() == 0


def test_expired_subscription_blocks_without_second_grant(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)
    now = datetime.now(timezone.utc)
    db.add(
        Subscription(
            id="sub-expired",
            tenant_id="tenant-rnd388",
            plan_id="annual-plan",
            status="expired",
            starts_at=now - timedelta(days=60),
            ends_at=now - timedelta(days=30),
            grace_ends_at=now - timedelta(days=23),
            source="annual",
        )
    )
    db.commit()

    response = client.post("/api/provisioning/activate", cookies=_cookies())
    activation = response.json()["activation"]
    assert activation["state"] == "blocked"
    assert activation["safe_error_code"] == "no_entitlement"
    assert db.query(Subscription).count() == 1  # no replacement trial
    assert db.get(Tenant, "tenant-rnd388").lifecycle_status == "provisioning"


def test_used_trial_history_blocks_without_second_grant(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)
    now = datetime.now(timezone.utc)
    db.add(
        SubscriptionHistory(
            id="history-used-trial",
            subscription_id="sub-trial-used",
            tenant_id="tenant-rnd388",
            plan_id="annual-plan",
            status="trial",
            starts_at=now - timedelta(days=60),
            ends_at=now - timedelta(days=45),
            grace_ends_at=now - timedelta(days=38),
            source=TRIAL_SOURCE,
            renewal_count=0,
            revision=1,
            change_kind="assigned",
        )
    )
    db.commit()

    response = client.post("/api/provisioning/activate", cookies=_cookies())
    activation = response.json()["activation"]
    assert activation["state"] == "blocked"
    assert activation["safe_error_code"] == "no_entitlement"
    assert db.query(Subscription).count() == 0  # grant was refused
    assert db.query(TenantActivationCheck).one().state == "blocked"


# ---------------------------------------------------------------------------
# Status endpoint + ready state + recoverability
# ---------------------------------------------------------------------------


def test_status_reports_not_started_before_any_check(
    activation_client,
) -> None:
    client, _db = activation_client
    status = client.get("/api/provisioning/status", cookies=_cookies())
    assert status.status_code == 200
    assert status.json()["activation"] == {
        "state": "not_started",
        "gate_results": {},
        "safe_error_code": None,
        "revision": 0,
    }
    assert "activate" not in status.json()["allowed_actions"]
    assert status.json()["archive_enabled"] is False


def test_ready_state_exposes_activate_action(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.tenant_activation import evaluate_activation

    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)

    snapshot = evaluate_activation(db, "tenant-rnd388")
    assert snapshot.state == "ready"
    db.commit()

    # Evaluation alone never promotes — the tenant stays provisioning.
    tenant = db.get(Tenant, "tenant-rnd388")
    assert tenant.lifecycle_status == "provisioning"
    assert db.query(AdminSession).one().session_scope == "provisioning"
    assert db.query(Subscription).one().status == "trial"

    status = client.get("/api/provisioning/status", cookies=_cookies())
    assert status.status_code == 200
    assert status.json()["activation"]["state"] == "ready"
    assert status.json()["activation"]["revision"] == 1
    assert "activate" in status.json()["allowed_actions"]

    # A second evaluation replays the trial — no new rows.
    evaluate_activation(db, "tenant-rnd388")
    db.commit()
    assert db.query(Subscription).count() == 1


def test_blocked_tenant_recovers_and_activates_after_fix(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)

    blocked = client.post("/api/provisioning/activate", cookies=_cookies())
    assert blocked.json()["activation"]["safe_error_code"] == "config_incomplete"
    assert db.query(TenantActivationCheck).one().revision == 1

    _complete_config(db, "tenant-rnd388")
    recovered = client.post("/api/provisioning/activate", cookies=_cookies())
    assert recovered.json()["activated"] is True
    check = db.query(TenantActivationCheck).one()
    assert check.state == "ready"
    assert check.revision == 2
    assert db.get(Tenant, "tenant-rnd388").lifecycle_status == "active"


# ---------------------------------------------------------------------------
# Client trust + platform override + auto-trigger
# ---------------------------------------------------------------------------


def test_client_cannot_influence_activation_target_or_lifecycle(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    _complete_config(db, "tenant-rnd388")
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)

    response = client.post(
        "/api/provisioning/activate",
        json={"tenant_id": "another-tenant", "lifecycle_status": "active"},
        cookies=_cookies(),
    )
    assert response.status_code == 200
    assert response.json()["activated"] is True
    activated = db.query(Tenant).all()
    assert [tenant.id for tenant in activated] == ["tenant-rnd388"]
    assert activated[0].lifecycle_status == "active"


def test_platform_override_activates_without_gates(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth import hash_password

    client, db = activation_client
    # No config, no runtime env, no working token — gates would all fail.
    platform_email = "platform-rnd388@example.test"
    db.add(
        PlatformAdmin(
            id="rnd388-platform-admin",
            email=platform_email,
            password_hash=hash_password("test-password"),
            role="superadmin",
            status="active",
        )
    )
    db.commit()
    basic = base64.b64encode(f"{platform_email}:test-password".encode()).decode()

    response = client.patch(
        "/api/platform/tenants/tenant-rnd388",
        json={"is_active": True},
        headers={"Authorization": f"Basic {basic}"},
    )
    assert response.status_code == 200
    assert response.json()["tenant_is_active"] is True

    tenant = db.get(Tenant, "tenant-rnd388")
    assert tenant.lifecycle_status == "active"
    assert db.query(AdminSession).one().session_scope == "admin"
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "platform.tenant_activated")
        .one()
    )
    assert audit.detail["actor"] == "platform"
    assert audit.detail["gate_results"] == {}
    assert db.query(TenantActivationCheck).count() == 0


def test_config_save_auto_triggers_activation_worker(
    activation_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = activation_client
    spawns = _capture_spawn(monkeypatch)
    _enable_runtime(monkeypatch)
    _ok_token(monkeypatch)

    response = client.put(
        "/api/provisioning/config",
        json={
            "archive_secret": _ARCHIVE_SECRET,
            "private_key": _RSA_PEM,
            "publickey_version": 1,
            "callback_token": _CALLBACK_TOKEN,
            "callback_encoding_aes_key": _CALLBACK_AES_KEY,
        },
        cookies=_cookies(),
    )
    assert response.status_code == 200
    assert spawns == [("tenant-rnd388", "self_service")]
