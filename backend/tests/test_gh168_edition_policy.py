"""GH-168 runtime edition contract and cloud-worker boundaries."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import resolver
from app.db.base import Base
from app.db.models import (
    AdminSession,
    AdminUser,
    AppConfigStore,
    Tenant,
    TenantWecomConfig,
    ThirdPartyOrganizationBinding,
)
from app.db.session import get_db
from app.services.service_access import CAPABILITIES, tenant_service_denial
from app.settings import APP_EDITION_CLOUD, APP_EDITION_SELFHOST, get_app_edition

BACKEND = Path(__file__).resolve().parents[1]


def _route_paths(app) -> set[str]:
    return {
        route.path
        for route in app.routes
        if hasattr(route, "path") and hasattr(route, "methods")
    }


def _load_script(filename: str, module_name: str):
    path = BACKEND / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unset_edition_defaults_to_selfhost_and_unknown_values_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import create_app

    monkeypatch.delenv("APP_EDITION", raising=False)
    assert get_app_edition() == APP_EDITION_SELFHOST
    assert create_app().state.edition == APP_EDITION_SELFHOST

    monkeypatch.setenv("APP_EDITION", "cloud-ish")
    with pytest.raises(ValueError, match="APP_EDITION"):
        create_app()
    with pytest.raises(ValueError, match="APP_EDITION"):
        create_app(edition="")


def test_selfhost_routes_exclude_cloud_surfaces_but_keep_archive_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import create_app

    monkeypatch.setenv("APP_EDITION", APP_EDITION_CLOUD)
    cloud_paths = _route_paths(create_app())
    selfhost = create_app(edition=APP_EDITION_SELFHOST)
    selfhost_paths = _route_paths(selfhost)

    cloud_only_paths = {
        "/admin/billing",
        "/api/billing/plan",
        "/api/billing/capacity",
        "/api/billing/orders",
        "/api/payments/wechat/notify",
        "/api/payments/alipay/notify",
        "/api/refunds/wechat/notify",
        "/api/auth/wecom/third-party/install",
        "/api/auth/wecom/third-party/callback",
        "/api/auth/wecom/organization-claim/confirm",
        "/admin/organization/confirm",
        "/api/provisioning/activate",
        "/platform",
        "/platform/login",
        "/api/platform/operations/dashboard",
        "/api/platform/operations/product-analytics/overview",
        "/api/platform/ai/handoffs/{handoff_id}/resolve",
        "/public/support",
        "/api/ai/public/support/status",
    }
    assert cloud_only_paths <= cloud_paths
    assert not cloud_only_paths & selfhost_paths
    assert {
        "/admin/provisioning/settings",
        "/api/provisioning/config",
        "/api/provisioning/config/test",
        "/api/auth/password/login",
        "/api/wecom/archive/events",
        "/api/admin/exports/quota",
        "/api/onboarding/status",
    } <= selfhost_paths
    assert selfhost.state.edition == APP_EDITION_SELFHOST


def test_selfhost_does_not_validate_cloud_payment_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    def fail_payment_validation() -> None:
        raise RuntimeError("cloud payment validation ran")

    monkeypatch.setattr(main, "validate_wechat_pay_configuration_if_configured", fail_payment_validation)
    monkeypatch.setattr(main, "validate_alipay_configuration_if_enabled", fail_payment_validation)
    assert main.create_app(edition=APP_EDITION_SELFHOST).state.edition == APP_EDITION_SELFHOST
    with pytest.raises(RuntimeError, match="cloud payment validation"):
        main.create_app(edition=APP_EDITION_CLOUD)


@pytest.mark.parametrize("capability", sorted(CAPABILITIES))
def test_frozen_is_non_blocking_only_for_core_selfhost_capabilities(capability: str) -> None:
    if capability == "owner_billing":
        assert tenant_service_denial("active", capability, edition=APP_EDITION_SELFHOST) == "service_unavailable"
        assert tenant_service_denial("frozen", capability, edition=APP_EDITION_SELFHOST) == "service_unavailable"
        assert tenant_service_denial("frozen", capability, edition=APP_EDITION_CLOUD) is None
    else:
        assert tenant_service_denial("frozen", capability, edition=APP_EDITION_SELFHOST) is None
        assert tenant_service_denial("frozen", capability, edition=APP_EDITION_CLOUD) == "service_frozen"
    assert tenant_service_denial("suspended", capability, edition=APP_EDITION_SELFHOST) == "service_suspended"
    assert tenant_service_denial("future-state", capability, edition=APP_EDITION_SELFHOST) == "service_unavailable"


@pytest.mark.parametrize(
    ("filename", "module_name", "argv"),
    [
        ("process_billing_lifecycle_once.py", "gh168_lifecycle_script", None),
        ("process_billing_notifications_once.py", "gh168_notifications_script", None),
        (
            "process_payment_recovery_once.py",
            "gh168_payment_recovery_script",
            ["process_payment_recovery_once.py", "recovery"],
        ),
    ],
)
def test_cloud_billing_workers_short_circuit_selfhost_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    filename: str,
    module_name: str,
    argv: list[str] | None,
) -> None:
    monkeypatch.setenv("APP_EDITION", APP_EDITION_SELFHOST)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    script = _load_script(filename, module_name)

    result = script.main() if argv is None else script.main(argv)

    assert result == 0
    assert "skipped selfhost edition" in capsys.readouterr().out


def test_selfhost_archive_workers_include_frozen_but_exclude_suspended_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.models import TenantWecomConfig
    from app.services.tenant_credentials import active_tenant_configs, active_tenant_ids

    monkeypatch.setenv("APP_EDITION", APP_EDITION_SELFHOST)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Tenant.__table__, TenantWecomConfig.__table__])
    factory = sessionmaker(bind=engine)
    try:
        with factory() as db:
            db.add_all(
                [
                    Tenant(id="selfhost-active", name="Active", slug="active"),
                    Tenant(
                        id="selfhost-frozen",
                        name="Legacy frozen",
                        slug="frozen",
                        lifecycle_status="frozen",
                    ),
                    Tenant(
                        id="selfhost-suspended",
                        name="Suspended",
                        slug="suspended",
                        lifecycle_status="suspended",
                    ),
                ]
            )
            db.flush()
            db.add_all(
                [
                    TenantWecomConfig(
                        id=f"cfg-{tenant_id}",
                        tenant_id=tenant_id,
                        corp_id=f"corp-{tenant_id}",
                        agent_id=f"agent-{tenant_id}",
                        app_secret="synthetic-secret",
                        is_active=True,
                    )
                    for tenant_id in (
                        "selfhost-active",
                        "selfhost-frozen",
                        "selfhost-suspended",
                    )
                ]
            )
            db.commit()
            assert set(active_tenant_ids(db)) == {"selfhost-active", "selfhost-frozen"}
            assert {config.tenant_id for config in active_tenant_configs(db)} == {
                "selfhost-active",
                "selfhost-frozen",
            }
    finally:
        engine.dispose()


def test_cloud_worker_services_refuse_selfhost_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.billing_lifecycle_batch import run_lifecycle_batch_once
    from app.services.billing_notifications import run_billing_notifications_once
    from app.services.payment_recovery import (
        run_payment_reconciliation_once,
        run_payment_recovery_once,
    )

    monkeypatch.setenv("APP_EDITION", APP_EDITION_SELFHOST)

    def forbidden_database_access():
        pytest.fail("cloud worker accessed the database in selfhost edition")

    with pytest.raises(RuntimeError, match="cloud-only"):
        run_lifecycle_batch_once(object())
    with pytest.raises(RuntimeError, match="cloud-only"):
        run_billing_notifications_once(object())
    with pytest.raises(RuntimeError, match="cloud-only"):
        run_payment_recovery_once(forbidden_database_access, object())
    with pytest.raises(RuntimeError, match="cloud-only"):
        run_payment_reconciliation_once(forbidden_database_access, object())


def test_selfhost_first_run_creates_default_tenant_and_uses_s2_wizard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.routers import auth as auth_router
    from app.services import product_analytics
    from app.services import tenant_config_service

    monkeypatch.setenv("APP_EDITION", APP_EDITION_SELFHOST)
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    for name in ("ADMIN_USERNAME", "ADMIN_PASSWORD_HASH", "WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_OAUTH_SECRET"):
        monkeypatch.delenv(name, raising=False)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Tenant.__table__,
        AdminUser.__table__,
        AdminSession.__table__,
        AppConfigStore.__table__,
        TenantWecomConfig.__table__,
        ThirdPartyOrganizationBinding.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        with factory() as db:
            yield db

    app = create_app()
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(auth_router, "write_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(product_analytics, "record_backend_event_best_effort", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tenant_config_service,
        "write_audit",
        lambda *args, **kwargs: None,
    )
    resolver.invalidate()

    try:
        with TestClient(app) as client:
            assert client.get("/admin/settings/init").status_code == 200
            bootstrap = client.post(
                "/api/admin/settings/bootstrap",
                json={
                    "admin_username": "local-admin",
                    "admin_password": "selfhost-test-password",
                    "wecom_corp_id": "ww-selfhost-synthetic",
                    "wecom_agent_id": "agent-synthetic",
                    "wecom_oauth_secret": "oauth-synthetic-secret",
                },
            )
            assert bootstrap.status_code == 200
            with factory() as db:
                tenants = db.query(Tenant).filter(Tenant.slug == "default").all()
                assert len(tenants) == 1
                tenant_id = tenants[0].id
                assert tenants[0].lifecycle_status == "active"

            login = client.post(
                "/api/auth/password/login",
                json={"username": "local-admin", "password": "selfhost-test-password"},
            )
            assert login.status_code == 200
            setup_page = client.get("/admin/provisioning", follow_redirects=False)
            assert setup_page.status_code == 302
            assert setup_page.headers["location"] == "/admin/provisioning/settings"
            assert client.get("/admin/settings").status_code == 200
            assert 'href="/admin/provisioning/settings"' in client.get("/admin/settings").text

            snapshot = client.get("/api/provisioning/config")
            assert snapshot.status_code == 200
            assert snapshot.json()["org"] == {
                "corp_name": "Default",
                "corp_id": "ww-selfhost-synthetic",
                "agent_id": "agent-synthetic",
                "source": "runtime_config",
            }
            saved = client.put(
                "/api/provisioning/config",
                json={"archive_secret": "archive-synthetic-secret"},
            )
            assert saved.status_code == 200
            assert saved.json()["fields"]["archive_secret"]["status"] == "set"
            assert client.get("/api/provisioning/status").json()["allowed_actions"] == [
                "view_status",
                "view_settings",
            ]
            assert client.post("/api/provisioning/activate", json={}).status_code == 404

            current = client.get("/api/auth/me").json()
            assert current["edition"] == APP_EDITION_SELFHOST
            assert current["tenant_id"] == tenant_id
    finally:
        app.dependency_overrides.clear()
        resolver.invalidate()
        engine.dispose()


def _edition_policy_test_db():
    from sqlalchemy.pool import StaticPool

    from app.db.models import (
        BillingPlan,
        MediaFile,
        PasswordResetToken,
        PlanEntitlement,
        Subscription,
        TenantStorageDaily,
    )

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            AdminUser.__table__,
            PasswordResetToken.__table__,
            BillingPlan.__table__,
            PlanEntitlement.__table__,
            Subscription.__table__,
            MediaFile.__table__,
            TenantStorageDaily.__table__,
        ],
    )
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.parametrize(
    ("edition", "lifecycle", "user_status", "email", "password_hash", "expected"),
    [
        (APP_EDITION_SELFHOST, "frozen", "active", "person@example.test", "synthetic-hash", True),
        (APP_EDITION_SELFHOST, "active", "active", "person@example.test", "synthetic-hash", True),
        (APP_EDITION_CLOUD, "frozen", "active", "person@example.test", "synthetic-hash", False),
        (APP_EDITION_SELFHOST, "suspended", "active", "person@example.test", "synthetic-hash", False),
        (APP_EDITION_SELFHOST, "provisioning", "active", "person@example.test", "synthetic-hash", False),
        (APP_EDITION_SELFHOST, "future-state", "active", "person@example.test", "synthetic-hash", False),
        (APP_EDITION_SELFHOST, "active", "disabled", "person@example.test", "synthetic-hash", False),
        (APP_EDITION_SELFHOST, "active", "active", None, "synthetic-hash", False),
        (APP_EDITION_SELFHOST, "active", "active", "person@example.test", None, False),
    ],
)
def test_password_forgot_uses_edition_aware_service_access(
    monkeypatch: pytest.MonkeyPatch,
    edition: str,
    lifecycle: str,
    user_status: str,
    email: str | None,
    password_hash: str | None,
    expected: bool,
) -> None:
    from sqlalchemy import text

    from app.db.models import PasswordResetToken
    from app.routers import auth as auth_router
    from app.routers.auth import _ForgotBody, password_forgot

    monkeypatch.setenv("APP_EDITION", edition)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(auth_router, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.email.send_password_reset_email",
        lambda address, link: sent.append((address, link)) or True,
    )
    engine, factory = _edition_policy_test_db()
    try:
        with factory() as db:
            if lifecycle == "future-state":
                db.execute(text("PRAGMA ignore_check_constraints = ON"))
            tenant = Tenant(
                id="tenant-reset",
                name="Default",
                slug="default",
                lifecycle_status=lifecycle,
            )
            user = AdminUser(
                id="user-reset",
                tenant_id=tenant.id,
                wecom_user_id="synthetic-user",
                email=email,
                password_hash=password_hash,
                role="admin",
                status=user_status,
            )
            db.add_all([tenant, user])
            db.commit()

            response = password_forgot(
                _ForgotBody(email="person@example.test"), db
            )

            assert response.body == b'{"ok":true}'
            assert bool(sent) is expected
            assert db.query(PasswordResetToken).count() == int(expected)
            if expected:
                assert sent[0][0] == "person@example.test"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("edition", "lifecycle", "expected_active", "expected_can_accept"),
    [
        (APP_EDITION_SELFHOST, "frozen", True, True),
        (APP_EDITION_SELFHOST, "suspended", False, False),
        (APP_EDITION_CLOUD, "frozen", False, False),
    ],
)
def test_ai_diagnostics_match_edition_access_and_storage_policy(
    monkeypatch: pytest.MonkeyPatch,
    edition: str,
    lifecycle: str,
    expected_active: bool,
    expected_can_accept: bool,
) -> None:
    from app.db.models import TenantStorageDaily
    from app.services.ai_tools.handlers import (
        storage_quota_summary_handler,
        tenant_service_status_handler,
    )
    from app.services.ai_tools.registry import ToolContext, ToolScope

    monkeypatch.setenv("APP_EDITION", edition)
    monkeypatch.setenv("SELFHOST_STORAGE_LIMIT_BYTES", "0")
    engine, factory = _edition_policy_test_db()
    try:
        with factory() as db:
            tenant = Tenant(
                id="tenant-diagnostic",
                name="Synthetic tenant",
                slug="diagnostic",
                lifecycle_status=lifecycle,
            )
            db.add(tenant)
            db.commit()
            context = ToolContext(
                tenant_id=tenant.id,
                admin_user_id="synthetic-admin",
                scope=ToolScope.TENANT_ADMIN,
                page_context={},
            )
            before = db.query(TenantStorageDaily).filter_by(tenant_id=tenant.id).count()

            service = tenant_service_status_handler(db, context)
            storage = storage_quota_summary_handler(db, context)

            after = db.query(TenantStorageDaily).filter_by(tenant_id=tenant.id).count()
            assert service["is_active"] is expected_active
            assert storage["can_accept_new_media"] is expected_can_accept
            assert before == after == 0
            if edition == APP_EDITION_SELFHOST and lifecycle == "frozen":
                assert storage["quota_bytes"] == 0
                assert storage["remaining_bytes"] is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("used_bytes", "expected_can_accept", "expected_remaining"),
    [(80, True, 20), (100, False, 0), (101, False, 0)],
)
def test_ai_capacity_diagnostic_uses_finite_selfhost_limit_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    used_bytes: int,
    expected_can_accept: bool,
    expected_remaining: int,
) -> None:
    from app.db.models import MediaFile, TenantStorageDaily
    from app.services.ai_tools.handlers import storage_quota_summary_handler
    from app.services.ai_tools.registry import ToolContext, ToolScope

    monkeypatch.setenv("APP_EDITION", APP_EDITION_SELFHOST)
    monkeypatch.setenv("SELFHOST_STORAGE_LIMIT_BYTES", "100")
    engine, factory = _edition_policy_test_db()
    try:
        with factory() as db:
            tenant = Tenant(
                id="tenant-capacity-diagnostic",
                name="Synthetic tenant",
                slug="capacity-diagnostic",
                lifecycle_status="frozen",
            )
            db.add(tenant)
            if used_bytes:
                db.add(
                    MediaFile(
                        sdkfileid="synthetic-downloaded-media",
                        archive_message_id=1,
                        tenant_id=tenant.id,
                        download_status="downloaded",
                        file_size=used_bytes,
                    )
                )
            db.commit()
            context = ToolContext(
                tenant_id=tenant.id,
                admin_user_id="synthetic-admin",
                scope=ToolScope.TENANT_ADMIN,
                page_context={},
            )
            before = db.query(TenantStorageDaily).filter_by(tenant_id=tenant.id).count()

            storage = storage_quota_summary_handler(db, context)

            after = db.query(TenantStorageDaily).filter_by(tenant_id=tenant.id).count()
            assert storage["quota_bytes"] == 100
            assert storage["used_bytes"] == used_bytes
            assert storage["remaining_bytes"] == expected_remaining
            assert storage["can_accept_new_media"] is expected_can_accept
            assert before == after == 0
    finally:
        engine.dispose()
