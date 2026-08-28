"""RND-402: tenant service access gates, worker gates, and lifecycle batch.

Covers the ADR-0005 §2.6 access matrix end to end:

* service access policy matrix (pure unit tests, including fail-closed
  unknown/None lifecycle status);
* admin API / HTML / billing session gates for active / grace-projected
  active / frozen / suspended / provisioning, with two-tenant isolation
  and renewal recovery;
* sync / media / export worker gates at tenant resolution (archive
  worker chain, media dispatch + CLI gate, export claim / poll / notify);
* idempotent lifecycle batch: commercial tenants only, replay-safe,
  per-tenant failure isolation, manual suspensions never reprojected.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit import AuditAction
from app.db.base import Base
from app.db.models import (
    AdminSession,
    AdminUser,
    AuditLog,
    BillingPlan,
    ExportJob,
    PlanEntitlement,
    PlatformAdmin,
    Subscription,
    SubscriptionActivation,
    SubscriptionHistory,
    Tenant,
    TenantWecomConfig,
)
from app.services.billing_lifecycle import (
    reconcile_tenant_billing_lifecycle,
    restore_tenant_after_paid_subscription,
    suspend_tenant_service,
)
from app.services.billing_lifecycle_batch import (
    batch_summary_line,
    run_lifecycle_batch_once,
)
from app.services.entitlements import (
    ANNUAL_PLAN_CODE,
    ARCHIVE_ACCESS,
    UNLIMITED_SEATS,
    assign_subscription,
)
from app.services.service_access import (
    DENY_FROZEN,
    DENY_PROVISIONING,
    DENY_SUSPENDED,
    INTERACTIVE,
    OWNER_BILLING,
    WORKER_EXPORT,
    WORKER_MEDIA,
    WORKER_SYNC,
    tenant_service_allows,
    tenant_service_denial,
)

NOW = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
PLAN_ID = "rnd402-plan"

_ALL_CAPABILITIES = (INTERACTIVE, OWNER_BILLING, WORKER_SYNC, WORKER_MEDIA, WORKER_EXPORT)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


# ---------------------------------------------------------------------------
# 1. Policy matrix (pure)
# ---------------------------------------------------------------------------


def test_policy_matrix_matches_adr_access_table() -> None:
    # active (grace is folded into active by the lifecycle service)
    for capability in _ALL_CAPABILITIES:
        assert tenant_service_allows("active", capability)
    # provisioning: billing only
    assert tenant_service_allows("provisioning", OWNER_BILLING)
    for capability in (INTERACTIVE, WORKER_SYNC, WORKER_MEDIA, WORKER_EXPORT):
        assert not tenant_service_allows("provisioning", capability)
    # frozen: billing only (recovery path)
    assert tenant_service_allows("frozen", OWNER_BILLING)
    for capability in (INTERACTIVE, WORKER_SYNC, WORKER_MEDIA, WORKER_EXPORT):
        assert not tenant_service_allows("frozen", capability)
    # suspended: everything denied, including billing — payment can never
    # clear a manual suspension
    for capability in _ALL_CAPABILITIES:
        assert not tenant_service_allows("suspended", capability)


def test_policy_denial_codes_are_stable() -> None:
    assert tenant_service_denial("frozen", INTERACTIVE) == DENY_FROZEN
    assert tenant_service_denial("frozen", WORKER_EXPORT) == DENY_FROZEN
    assert tenant_service_denial("suspended", OWNER_BILLING) == DENY_SUSPENDED
    assert tenant_service_denial("suspended", WORKER_SYNC) == DENY_SUSPENDED
    assert tenant_service_denial("provisioning", INTERACTIVE) == DENY_PROVISIONING


def test_policy_fails_closed_for_unknown_and_missing_status() -> None:
    assert tenant_service_denial(None, INTERACTIVE) == DENY_SUSPENDED
    assert tenant_service_denial("", WORKER_MEDIA) == DENY_SUSPENDED
    assert tenant_service_denial("weird", INTERACTIVE) == "service_unavailable"


def test_policy_rejects_unknown_capability() -> None:
    with pytest.raises(ValueError):
        tenant_service_denial("active", "not_a_capability")


# ---------------------------------------------------------------------------
# Shared DB fixtures
# ---------------------------------------------------------------------------


def _tables(extra):
    base = [
        Tenant.__table__,
        PlatformAdmin.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        SubscriptionHistory.__table__,
        SubscriptionActivation.__table__,
        AdminUser.__table__,
        AdminSession.__table__,
        AuditLog.__table__,
        TenantWecomConfig.__table__,
    ]
    return base + extra


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_tables([ExportJob.__table__]))
    result = sessionmaker(bind=engine, expire_on_commit=False)
    with result() as db:
        db.add_all(
            [
                Tenant(id="tenant-a", name="A", slug="tenant-a"),
                Tenant(id="tenant-b", name="B", slug="tenant-b"),
                Tenant(id="tenant-frozen", name="Frozen", slug="tenant-frozen"),
                Tenant(id="tenant-suspended", name="Susp", slug="tenant-suspended"),
                Tenant(id="tenant-legacy", name="Legacy", slug="tenant-legacy"),
                PlatformAdmin(
                    id="platform-admin",
                    email="platform@example.test",
                    password_hash="unused-in-domain-test",
                    status="active",
                ),
                BillingPlan(
                    id=PLAN_ID,
                    code=ANNUAL_PLAN_CODE,
                    display_name="年度基础套餐",
                    is_active=True,
                    amount_cents=9900,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=5 * 1024**3,
                ),
                PlanEntitlement(
                    id="rnd402-archive",
                    plan_id=PLAN_ID,
                    capability=ARCHIVE_ACCESS,
                    is_enabled=True,
                ),
                PlanEntitlement(
                    id="rnd402-seats",
                    plan_id=PLAN_ID,
                    capability=UNLIMITED_SEATS,
                    is_enabled=True,
                ),
                AdminUser(
                    id="owner-a",
                    tenant_id="tenant-a",
                    wecom_user_id="owner-a",
                    name="Owner A",
                    role="owner",
                    status="active",
                    email="owner-a@example.test",
                ),
                AdminUser(
                    id="owner-b",
                    tenant_id="tenant-b",
                    wecom_user_id="owner-b",
                    name="Owner B",
                    role="owner",
                    status="active",
                ),
                AdminUser(
                    id="owner-frozen",
                    tenant_id="tenant-frozen",
                    wecom_user_id="owner-frozen",
                    name="Owner Frozen",
                    role="owner",
                    status="active",
                ),
                AdminUser(
                    id="owner-suspended",
                    tenant_id="tenant-suspended",
                    wecom_user_id="owner-suspended",
                    name="Owner Susp",
                    role="owner",
                    status="active",
                ),
            ]
        )
        db.commit()
    return result


def _assign(
    db: Session,
    *,
    tenant_id: str = "tenant-a",
    status: str = "active",
    starts_at: datetime = NOW - timedelta(days=365),
    ends_at: datetime = NOW + timedelta(days=30),
) -> Subscription:
    return assign_subscription(
        db,
        tenant_id=tenant_id,
        plan_code=ANNUAL_PLAN_CODE,
        status=status,
        starts_at=starts_at,
        ends_at=ends_at,
        source="rnd402_test",
    )


def _set_lifecycle(db: Session, tenant_id: str, status: str) -> None:
    tenant = db.get(Tenant, tenant_id)
    assert tenant is not None
    tenant.lifecycle_status = status
    tenant.is_active = status == "active"
    db.commit()


# ---------------------------------------------------------------------------
# 2. Lifecycle batch
# ---------------------------------------------------------------------------


def test_batch_scans_only_commercial_tenants_and_ignores_legacy(factory) -> None:
    with factory() as db:
        # tenant-a is commercial (has a subscription); tenant-b has none.
        _assign(db, tenant_id="tenant-a")
        _set_lifecycle(db, "tenant-b", "provisioning")
        summary = run_lifecycle_batch_once(db, now=NOW, limit=50)
        assert summary.scanned == 1
        assert summary.changed == 0 and summary.unchanged == 1
        # The legacy/self-host tenant was never touched by the batch.
        assert db.get(Tenant, "tenant-b").lifecycle_status == "provisioning"
        assert db.get(Tenant, "tenant-legacy").lifecycle_status == "active"


def test_batch_transitions_grace_to_frozen_and_is_idempotent(factory) -> None:
    with factory() as db:
        _assign(
            db,
            tenant_id="tenant-a",
            ends_at=NOW - timedelta(days=2),
        )
        reconcile_tenant_billing_lifecycle(db, "tenant-a", at=NOW)
        db.commit()
        first = run_lifecycle_batch_once(db, now=NOW + timedelta(days=8))
        assert first.changed == 1
        assert db.get(Tenant, "tenant-a").lifecycle_status == "frozen"
        audits = (
            db.query(AuditLog)
            .filter(AuditLog.tenant_id == "tenant-a")
            .filter(AuditLog.action == AuditAction.TENANT_BILLING_FROZEN)
            .count()
        )
        assert audits == 1
        # Replay applies nothing new and writes no duplicate audit rows.
        second = run_lifecycle_batch_once(db, now=NOW + timedelta(days=9))
        assert second.changed == 0
        assert (
            db.query(AuditLog)
            .filter(AuditLog.tenant_id == "tenant-a")
            .filter(AuditLog.action == AuditAction.TENANT_BILLING_FROZEN)
            .count()
            == 1
        )


def test_batch_renews_frozen_tenant_back_to_active(factory) -> None:
    with factory() as db:
        _assign(db, tenant_id="tenant-a", ends_at=NOW - timedelta(days=1))
        # At NOW the tenant is still in grace; freeze only after grace ends.
        reconcile_tenant_billing_lifecycle(db, "tenant-a", at=NOW + timedelta(days=8))
        db.commit()
        assert db.get(Tenant, "tenant-a").lifecycle_status == "frozen"
        # Renewal: new trusted service period starting now.
        assign_subscription(
            db,
            tenant_id="tenant-a",
            plan_code=ANNUAL_PLAN_CODE,
            status="active",
            starts_at=NOW,
            ends_at=NOW + timedelta(days=365),
            source="rnd402_test",
        )
        db.commit()
        summary = run_lifecycle_batch_once(db, now=NOW + timedelta(minutes=1))
        assert summary.tenant_changed == 1
        assert db.get(Tenant, "tenant-a").lifecycle_status == "active"
        assert db.get(Tenant, "tenant-a").is_active is True


def test_batch_never_reprojects_a_manual_suspension(factory) -> None:
    with factory() as db:
        _assign(db, tenant_id="tenant-a", ends_at=NOW - timedelta(days=2))
        suspend_tenant_service(
            db,
            "tenant-a",
            platform_admin_id="platform-admin",
            reason_code="abuse",
        )
        # Subscription grace -> expired must still advance while suspended.
        summary = run_lifecycle_batch_once(
            db, now=NOW + timedelta(days=8), limit=50
        )
        assert summary.skipped_suspended == 1
        assert db.get(Tenant, "tenant-a").lifecycle_status == "suspended"
        assert (
            db.get(Tenant, "tenant-a").suspension_reason == "abuse"
        )


def test_batch_isolates_per_tenant_failure(monkeypatch, factory) -> None:
    import app.services.billing_lifecycle_batch as batch_module

    with factory() as db:
        _assign(db, tenant_id="tenant-a")
        _assign(db, tenant_id="tenant-frozen", ends_at=NOW - timedelta(days=2))
        calls = {"n": 0}

        def flaky_reconcile(db_, tenant_id, *, at):
            calls["n"] += 1
            if tenant_id == "tenant-frozen":
                raise RuntimeError("simulated transient failure")
            return reconcile_tenant_billing_lifecycle(db_, tenant_id, at=at)

        monkeypatch.setattr(batch_module, "reconcile_tenant_billing_lifecycle", flaky_reconcile)
        summary = run_lifecycle_batch_once(db, now=NOW, limit=50, retries=2)
        # tenant-frozen exhausted its bounded retries; tenant-a still applied.
        assert summary.failed == 1
        assert summary.unchanged == 1
        assert calls["n"] == 2 + 1  # 2 retries for frozen + 1 for tenant-a


def test_batch_summary_contains_no_identifiers(factory) -> None:
    from app.services.billing_lifecycle_batch import LifecycleBatchSummary

    summary = LifecycleBatchSummary(
        scanned=2,
        changed=1,
        unchanged=1,
        failed=0,
        skipped_suspended=0,
        subscription_changed=1,
        tenant_changed=1,
        tenant_transitions={"active->frozen": 1},
    )
    line = batch_summary_line(summary)
    for forbidden in ("tenant-a", "tenant-frozen", "tenant-b", "tenant-suspended"):
        assert forbidden not in line


# ---------------------------------------------------------------------------
# 3. Export worker gates
# ---------------------------------------------------------------------------


def _export_job(db: Session, *, tenant_id: str, job_id: str, status: str = "queued"):
    db.add(
        ExportJob(
            id=job_id,
            tenant_id=tenant_id,
            requested_by="owner-a",
            kind="media_zip",
            format="zip",
            status=status,
            notification_status="pending",
            attempt_count=0,
            notification_attempts=0,
            requested_at=NOW,
            expires_at=NOW + timedelta(days=2) if status == "ready" else None,
        )
    )
    db.commit()


def test_export_claim_skips_frozen_tenant_and_claims_active(factory) -> None:
    from app.services.export_jobs import _claim_next_job

    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
        _export_job(db, tenant_id="tenant-frozen", job_id="job-frozen")
        _export_job(db, tenant_id="tenant-a", job_id="job-active")
        claimed = _claim_next_job(db, NOW)
        assert claimed is not None and claimed.id == "job-active"
        assert db.get(ExportJob, "job-frozen").status == "queued"


def test_export_maintenance_counts_blocked_jobs(factory) -> None:
    from app.services.export_jobs import run_export_maintenance_once

    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
        _set_lifecycle(db, "tenant-suspended", "suspended")
        _export_job(db, tenant_id="tenant-frozen", job_id="job-frozen")
        _export_job(db, tenant_id="tenant-suspended", job_id="job-susp")
        summary = run_export_maintenance_once(db, now=NOW, generation_limit=5)
        assert summary.blocked == 2
        assert summary.claimed == 0
        assert db.get(ExportJob, "job-frozen").status == "queued"
        assert db.get(ExportJob, "job-susp").status == "queued"


def test_export_notifications_skip_frozen_tenant(monkeypatch, factory) -> None:
    from app.services import export_jobs

    with factory() as db:
        monkeypatch.setattr(export_jobs, "send_export_ready_email", lambda *a, **k: True)
        monkeypatch.setattr(
            export_jobs,
            "_notification_link",
            lambda job_id: f"https://example.test/admin/exports?job={job_id}",
        )
        _set_lifecycle(db, "tenant-frozen", "frozen")
        _export_job(
            db,
            tenant_id="tenant-frozen",
            job_id="job-frozen-ready",
            status="ready",
        )
        _export_job(db, tenant_id="tenant-a", job_id="job-active-ready", status="ready")
        sent, failed = export_jobs.send_pending_export_notifications(db, now=NOW)
        assert sent == 1 and failed == 0
        assert db.get(ExportJob, "job-frozen-ready").notification_attempts == 0
        assert db.get(ExportJob, "job-active-ready").notification_status == "sent"


# ---------------------------------------------------------------------------
# 4. Archive / media worker gates
# ---------------------------------------------------------------------------


def test_archive_worker_gate_skips_frozen_and_audits(factory) -> None:
    from scripts.run_archive_worker_once import ArchiveWorkerExit, _gate_single_tenant

    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
        with pytest.raises(ArchiveWorkerExit) as exc:
            _gate_single_tenant(db, "tenant-frozen", "digest")
        assert exc.value.code == 0 and exc.value.result == "skipped"
        audit = (
            db.query(AuditLog)
            .filter(AuditLog.tenant_id == "tenant-frozen")
            .filter(AuditLog.action == AuditAction.SERVICE_ACCESS_DENIED)
            .one()
        )
        assert audit.detail["error_code"] == DENY_FROZEN
        assert audit.detail["capability"] == WORKER_SYNC
        # Active tenants pass the gate without side effects.
        _gate_single_tenant(db, "tenant-a", "digest")
        assert (
            db.query(AuditLog)
            .filter(AuditLog.tenant_id == "tenant-a")
            .filter(AuditLog.action == AuditAction.SERVICE_ACCESS_DENIED)
            .count()
            == 0
        )


def test_media_worker_cli_gate_skips_frozen_and_audits(factory) -> None:
    from scripts.download_wecom_media_once import _gate_tenant_service

    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
        with pytest.raises(SystemExit) as exc:
            _gate_tenant_service(db, "tenant-frozen")
        assert exc.value.code == 0
        audit = (
            db.query(AuditLog)
            .filter(AuditLog.tenant_id == "tenant-frozen")
            .filter(AuditLog.action == AuditAction.SERVICE_ACCESS_DENIED)
            .one()
        )
        assert audit.detail["error_code"] == DENY_FROZEN
        assert audit.detail["capability"] == WORKER_MEDIA
        _gate_tenant_service(db, "tenant-a")  # no-op for active tenants


def test_media_dispatch_skips_frozen_tenant(monkeypatch, factory) -> None:
    from app.media_event_dispatch import MediaWorkerDispatch, dispatch_media_worker

    with factory() as db:
        monkeypatch.setattr(
            "app.media_event_dispatch.get_engine",
            lambda: db.get_bind(),
        )
        _set_lifecycle(db, "tenant-frozen", "frozen")
        outcome = dispatch_media_worker(
            trigger_source="archive-complete", tenant_id="tenant-frozen"
        )
        assert outcome is MediaWorkerDispatch.SKIPPED
        # The gate passes for active tenants (the preflight result itself
        # depends on media tables and is covered by the CLI gate test).
        _set_lifecycle(db, "tenant-a", "active")
        outcome = dispatch_media_worker(
            trigger_source="archive-complete", tenant_id="tenant-a"
        )
        assert outcome is not MediaWorkerDispatch.SKIPPED


def test_contact_refresh_worker_skips_frozen_tenant(factory) -> None:
    from app.services.tenant_credentials import active_tenant_configs

    with factory() as db:
        db.add(
            TenantWecomConfig(
                id="cfg-frozen",
                tenant_id="tenant-frozen",
                corp_id="corp-frozen",
                is_active=True,
                app_secret="unused",
                agent_id="1",
            )
        )
        db.add(
            TenantWecomConfig(
                id="cfg-active",
                tenant_id="tenant-a",
                corp_id="corp-active",
                is_active=True,
                app_secret="unused",
                agent_id="1",
            )
        )
        db.commit()
        _set_lifecycle(db, "tenant-frozen", "frozen")
        assert [config.tenant_id for config in active_tenant_configs(db)] == ["tenant-a"]


# ---------------------------------------------------------------------------
# 5. Admin API / HTML / billing session gates (TestClient)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client(factory, monkeypatch):
    from fastapi import Depends
    from fastapi.testclient import TestClient

    from app.auth import get_current_user, require_role
    from app.db.session import get_db
    from app.main import create_app

    application = create_app()

    def override_db():
        with factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_db

    # Lightweight gate probes: they exercise exactly the auth dependencies
    # under test without pulling in unrelated route tables.
    @application.get("/__rnd402_probe")
    def _probe(auth: tuple = Depends(get_current_user)):
        return {"ok": True}

    @application.get("/__rnd402_probe_role")
    def _probe_role(auth: tuple = Depends(require_role("owner"))):
        return {"ok": True}

    with TestClient(application) as client:
        yield client, factory


def _session_cookie(factory, *, user_id: str, tenant_id: str) -> str:
    with factory() as db:
        session = AdminSession(
            id=str(uuid.uuid4()),
            admin_user_id=user_id,
            tenant_id=tenant_id,
            wecom_user_id=user_id,
            # Session expiry is validated against the wall clock, not the
            # test's fixed NOW (which may already be in the past).
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            is_revoked=False,
        )
        db.add(session)
        db.commit()
        return session.id


def test_business_api_access_matrix(app_client) -> None:
    client, factory = app_client
    sessions = {
        "active": _session_cookie(factory, user_id="owner-a", tenant_id="tenant-a"),
        "frozen": _session_cookie(
            factory, user_id="owner-frozen", tenant_id="tenant-frozen"
        ),
        "suspended": _session_cookie(
            factory, user_id="owner-suspended", tenant_id="tenant-suspended"
        ),
    }
    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
        _set_lifecycle(db, "tenant-suspended", "suspended")
        _set_lifecycle(db, "tenant-b", "provisioning")

    response = client.get("/__rnd402_probe", cookies={"session_id": sessions["active"]})
    assert response.status_code == 200

    response = client.get("/__rnd402_probe", cookies={"session_id": sessions["frozen"]})
    assert response.status_code == 403
    assert response.json()["detail"] == DENY_FROZEN

    response = client.get("/__rnd402_probe", cookies={"session_id": sessions["suspended"]})
    assert response.status_code == 403
    assert response.json()["detail"] == DENY_SUSPENDED


def test_provisioning_tenant_denies_interactive_business_api(app_client) -> None:
    client, factory = app_client
    session_id = _session_cookie(factory, user_id="owner-a", tenant_id="tenant-b")
    with factory() as db:
        _set_lifecycle(db, "tenant-b", "provisioning")
    response = client.get("/__rnd402_probe", cookies={"session_id": session_id})
    assert response.status_code == 403
    assert response.json()["detail"] == DENY_PROVISIONING


def test_two_tenants_do_not_contaminate_each_other(app_client) -> None:
    client, factory = app_client
    frozen_session = _session_cookie(
        factory, user_id="owner-frozen", tenant_id="tenant-frozen"
    )
    active_session = _session_cookie(factory, user_id="owner-a", tenant_id="tenant-a")
    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
    assert (
        client.get("/__rnd402_probe", cookies={"session_id": frozen_session}).status_code
        == 403
    )
    assert (
        client.get("/__rnd402_probe", cookies={"session_id": active_session}).status_code
        == 200
    )


def test_billing_surface_allowed_for_frozen_and_suspended_owner(app_client) -> None:
    client, factory = app_client
    frozen_session = _session_cookie(
        factory, user_id="owner-frozen", tenant_id="tenant-frozen"
    )
    suspended_session = _session_cookie(
        factory, user_id="owner-suspended", tenant_id="tenant-suspended"
    )
    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
        _set_lifecycle(db, "tenant-suspended", "suspended")
    # Frozen Owner keeps the billing/renewal recovery surface.
    response = client.get("/admin/billing", cookies={"session_id": frozen_session})
    assert response.status_code == 200
    # RND-404: a manual suspension still renders the read-only billing page
    # (an accurate status instead of an opaque 403) — payment-write actions
    # remain gated separately via get_billing_manager/get_billing_owner.
    response = client.get("/admin/billing", cookies={"session_id": suspended_session})
    assert response.status_code == 200


def test_dashboard_html_redirects_non_active_tenants(app_client) -> None:
    client, factory = app_client
    frozen_session = _session_cookie(
        factory, user_id="owner-frozen", tenant_id="tenant-frozen"
    )
    active_session = _session_cookie(factory, user_id="owner-a", tenant_id="tenant-a")
    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
    response = client.get("/dashboard", cookies={"session_id": frozen_session}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"
    assert (
        client.get("/dashboard", cookies={"session_id": active_session}).status_code
        == 200
    )


def test_login_page_routes_frozen_session_to_billing_without_loop(app_client) -> None:
    client, factory = app_client
    frozen_session = _session_cookie(
        factory, user_id="owner-frozen", tenant_id="tenant-frozen"
    )
    active_session = _session_cookie(factory, user_id="owner-a", tenant_id="tenant-a")
    suspended_session = _session_cookie(
        factory, user_id="owner-suspended", tenant_id="tenant-suspended"
    )
    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
        _set_lifecycle(db, "tenant-suspended", "suspended")
    # Frozen: the login page must not bounce to /dashboard (which would
    # redirect back to login forever); it goes straight to billing.
    response = client.get("/admin/login", cookies={"session_id": frozen_session}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/billing"
    # Active: unchanged behavior.
    response = client.get("/admin/login", cookies={"session_id": active_session}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"
    # Suspended: stays on the login page (no loop, no console).
    response = client.get("/admin/login", cookies={"session_id": suspended_session}, follow_redirects=False)
    assert response.status_code == 200


def test_auth_me_projects_lifecycle_status(app_client) -> None:
    client, factory = app_client
    session_id = _session_cookie(
        factory, user_id="owner-frozen", tenant_id="tenant-frozen"
    )
    with factory() as db:
        _set_lifecycle(db, "tenant-frozen", "frozen")
    data = client.get("/api/auth/me", cookies={"session_id": session_id}).json()
    assert data["authenticated"] is True
    assert data["lifecycle_status"] == "frozen"


def test_password_login_denied_for_suspended_default_tenant(monkeypatch, app_client) -> None:
    import app.routers.auth as auth_router

    client, factory = app_client
    monkeypatch.setattr(auth_router, "get_auth_mode", lambda: "password")
    with factory() as db:
        default = db.query(Tenant).filter(Tenant.slug == "default").first()
        if default is None:
            default = Tenant(id="default", name="Default", slug="default")
            db.add(default)
        default.lifecycle_status = "suspended"
        default.is_active = False
        db.commit()
    response = client.post(
        "/api/auth/password/login",
        json={"username": "owner-suspended", "password": "whatever"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == DENY_SUSPENDED


def test_renewal_recovery_reopens_interactive_gate(app_client) -> None:
    client, factory = app_client
    session_id = _session_cookie(
        factory, user_id="owner-frozen", tenant_id="tenant-frozen"
    )
    with factory() as db:
        subscription = _assign(
            db, tenant_id="tenant-frozen", ends_at=NOW - timedelta(days=1)
        )
        db.commit()
        tenant = db.get(Tenant, "tenant-frozen")
        assert tenant is not None
        assert tenant.lifecycle_status == "active"  # default fixture state
        tenant.lifecycle_status = "frozen"
        tenant.is_active = False
        db.commit()
        assert (
            client.get(
                "/api/admin/users", cookies={"session_id": session_id}
            ).status_code
            == 403
        )
        # Trusted renewal: billing freeze is lifted, suspension never.
        restore_tenant_after_paid_subscription(db, tenant, subscription, at=NOW)
        db.commit()
    assert (
        client.get("/__rnd402_probe", cookies={"session_id": session_id}).status_code
        == 200
    )
