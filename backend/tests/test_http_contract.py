"""
HTTP contract & behaviour test baseline (RND-214).

Covers: real response-model-name route snapshot, auth gates,
authenticated non‑empty JSON shape tests, headers including
Cache‑Control success/error paths, frontend HTML structure order,
and media entity‑context collision characterisation (with real
media + timeline assertions).

No live database required (SQLite :memory: or mock DB).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Generator, Union, get_args, get_origin
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, text
from sqlalchemy.orm import Session

# ── SQLite schema (hand‑written — JSONB columns → TEXT) ────────────

_SCHEMA_SQL = """
CREATE TABLE tenants (
    id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    slug TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1,
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    lifecycle_revision INTEGER NOT NULL DEFAULT 1,
    frozen_at DATETIME,
    suspended_at DATETIME,
    suspension_reason TEXT,
    suspended_by_platform_admin_id TEXT,
    suspension_previous_status TEXT,
    onboarding_completed_at TEXT,
    deletion_locked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE archive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msgid TEXT NOT NULL, seq INTEGER NOT NULL,
    publickey_ver INTEGER NOT NULL, raw_encrypted_payload TEXT,
    encrypt_random_key TEXT NOT NULL DEFAULT '', encrypt_chat_msg TEXT NOT NULL DEFAULT '',
    decrypt_status TEXT NOT NULL DEFAULT 'pending', decrypted_payload TEXT,
    structured_content TEXT, content_text TEXT, msgtype TEXT, sender TEXT,
    roomid TEXT, msgtime INTEGER, tolist TEXT, sdkfileid TEXT,
    is_revoked INTEGER NOT NULL DEFAULT 0, revoked_at TEXT,
    deleted_at DATETIME, deleted_by_admin_user_id TEXT, delete_reason TEXT,
    purge_after DATETIME, restored_at DATETIME, restored_by_admin_user_id TEXT,
    deletion_batch_id TEXT,
    tenant_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE archive_message_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
    receiver_userid TEXT NOT NULL, receiver_type TEXT, tenant_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, wecom_userid TEXT NOT NULL,
    name TEXT, tenant_id TEXT, avatar_storage_backend TEXT, avatar_storage_ref TEXT,
    avatar_content_type TEXT, avatar_source TEXT, avatar_synced_at TEXT, avatar_status TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sdkfileid TEXT NOT NULL,
    archive_message_id INTEGER, tenant_id TEXT, file_type TEXT,
    local_path TEXT, oss_key TEXT, storage_backend TEXT, storage_ref TEXT,
    file_size INTEGER, download_status TEXT NOT NULL DEFAULT 'pending',
    download_attempts INTEGER NOT NULL DEFAULT 0,
    migration_status TEXT, migration_attempted_at TEXT, migration_error TEXT,
    bucket TEXT, mime_type TEXT, checksum_sha256 TEXT, thumbnail_ref TEXT,
    image_width INTEGER, image_height INTEGER, thumbnail_status TEXT,
    thumbnail_attempted_at TEXT, thumbnail_error TEXT,
    playback_ref TEXT, playback_status TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE message_revocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT,
    revoke_event_message_id INTEGER NOT NULL, revoke_event_msgid TEXT NOT NULL DEFAULT '',
    revoke_event_msgtime INTEGER, target_msgid TEXT, original_message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(status IN ('pending','linked','malformed')),
    CHECK((status='linked') = (original_message_id IS NOT NULL))
);
CREATE TABLE admin_users (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, wecom_user_id TEXT NOT NULL,
    name TEXT, avatar_url TEXT, last_login_at TEXT,
    password_hash TEXT, role TEXT NOT NULL DEFAULT 'admin',
    status TEXT NOT NULL DEFAULT 'active', email TEXT, phone TEXT,
    department TEXT, last_active_at TEXT, invite_token TEXT,
    invited_by TEXT, invite_status TEXT,
    ui_theme TEXT NOT NULL DEFAULT 'light', ui_locale TEXT NOT NULL DEFAULT 'zh-CN',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE tenant_wecom_configs (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, corp_id TEXT NOT NULL,
    agent_id TEXT, app_secret TEXT, callback_domain TEXT,
    private_key_encrypted TEXT,
    callback_token_encrypted TEXT,
    callback_encoding_aes_key_encrypted TEXT,
    publickey_version INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE admin_sessions (
    id TEXT PRIMARY KEY, admin_user_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
    wecom_user_id TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL, is_revoked INTEGER NOT NULL DEFAULT 0,
    session_scope TEXT NOT NULL DEFAULT 'admin'
);
CREATE TABLE reachability_audit_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL,
    algorithm_version TEXT NOT NULL, scope_from DATETIME NOT NULL, scope_to DATETIME NOT NULL,
    scope_max_message_id INTEGER NOT NULL, matching_count INTEGER NOT NULL DEFAULT 0,
    checked_count INTEGER NOT NULL DEFAULT 0, reachable_count INTEGER NOT NULL DEFAULT 0,
    unreachable_count INTEGER NOT NULL DEFAULT 0, reason_counts JSON NOT NULL DEFAULT '{}',
    safe_error_code TEXT, started_at DATETIME, completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_reachability_audit_runs_tenant_checking
ON reachability_audit_runs(tenant_id) WHERE status = 'checking';
CREATE TABLE group_chat_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    roomid TEXT NOT NULL,
    display_name TEXT,
    source TEXT NOT NULL DEFAULT 'wecom_external_groupchat',
    sync_status TEXT NOT NULL DEFAULT 'unresolved',
    last_checked_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(tenant_id, roomid)
);
"""


def _make_session() -> Session:
    from tests.test_reachability_audit import configure_sqlite_for_savepoints

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    configure_sqlite_for_savepoints(engine)
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    return Session(engine)


# ── Route snapshot helpers ──────────────────────────────────────────


def _snapshot_response_model(route: Any) -> str:
    rm = getattr(route, "response_model", None)
    if rm is None:
        return "None"
    origin = get_origin(rm)
    args = get_args(rm)
    if origin is Union and len(args) == 2 and type(None) in args:
        value_type = next(arg for arg in args if arg is not type(None))
        value_name = getattr(value_type, "__name__", str(value_type))
        return f"Optional[{value_name}]"
    name = getattr(rm, "__name__", "")
    if name in ("list", "List"):
        if args:
            return f"list[{args[0].__name__}]"
        return "list"
    return name


def _snapshot_response_class(route: Any) -> str:
    rc = getattr(route, "response_class", None)
    if rc is None:
        return "None"
    name = getattr(rc, "__name__", "")
    if name and name != "DefaultPlaceholder":
        return name
    return "None"


# ── Mock DB ─────────────────────────────────────────────────────────


def _mock_db_no_session() -> Generator:
    mock = MagicMock()
    mock.query.return_value.filter.return_value.first.return_value = None
    yield mock


def _mock_db_with_session() -> Generator:
    from app.db.models import AdminSession, AdminUser

    mock_user = MagicMock(spec=AdminUser)
    mock_user.id = "user-001"
    mock_user.tenant_id = "tenant-a"
    # RND-321 QA-001 round 2: get_current_user/require_html_session now
    # re-check user.status; MagicMock(spec=...) only restricts which
    # attributes exist, it doesn't give them real values, so without this
    # mock_user.status was itself a MagicMock — never equal to "active".
    mock_user.status = "active"
    mock_session = MagicMock(spec=AdminSession)
    mock_session.id = "session-001"
    mock_session.admin_user_id = "user-001"
    mock_session.tenant_id = "tenant-a"
    mock_session.expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
    mock_session.is_revoked = False
    results: dict[type, Any] = {AdminSession: mock_session, AdminUser: mock_user}
    mock = MagicMock()

    def _query(cls: Any) -> MagicMock:
        q = MagicMock()
        q.filter.return_value = q
        q.filter_by.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = []
        q.first.return_value = results.get(cls)
        return q

    mock.query.side_effect = _query
    # RND-402: require_html_session re-checks the tenant's authoritative
    # service projection, so the fixture must present an active tenant.
    mock_tenant = MagicMock()
    mock_tenant.lifecycle_status = "active"
    mock.get.return_value = mock_tenant
    yield mock


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def override_db_unauth():
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_no_session
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def db_session():
    s = _make_session()
    yield s
    s.close()


@pytest.fixture()
def authed_client(db_session: Session):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    mock_user = MagicMock()
    mock_user.id = "user-001"

    def _override_db():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: (mock_user, "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app, raise_server_exceptions=False) as c:
        c.cookies.set("session_id", "session-001")
        yield c
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides[get_db] = _mock_db_no_session


@pytest.fixture()
def authed_html_client():
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_with_session
    with TestClient(app, raise_server_exceptions=False) as c:
        c.cookies.set("session_id", "session-001")
        yield c
    app.dependency_overrides[get_db] = _mock_db_no_session


# ── Seed helpers ────────────────────────────────────────────────────


def _seed_tenant_a(db: Session) -> None:
    db.execute(
        text("INSERT INTO tenants (id,name,slug,is_active) VALUES (:i,:n,:s,1)"),
        {"i": "tenant-a", "n": "Tenant A", "s": "tenant-a"},
    )


def _seed_contact(db: Session, uid: str, name: str) -> None:
    db.execute(
        text(
            "INSERT INTO contacts (wecom_userid,name,tenant_id) VALUES (:u,:n,'tenant-a')"
        ),
        {"u": uid, "n": name},
    )


def _seed_message(
    db: Session,
    *,
    msgid: str,
    sender: str = "",
    roomid: str = "",
    msgtype: str = "text",
    content_text: str = "",
    sdkfileid: str = "",
    msgtime: int = 1000,
) -> int:
    db.execute(
        text(
            "INSERT INTO archive_messages (msgid,seq,publickey_ver,encrypt_random_key,"
            "encrypt_chat_msg,msgtype,sender,roomid,msgtime,content_text,sdkfileid,"
            "decrypt_status,tenant_id) VALUES (:m,1,1,'','',:t,:s,:r,:mt,:c,:sdk,"
            "'decrypted','tenant-a')"
        ),
        {
            "m": msgid,
            "t": msgtype,
            "s": sender,
            "r": roomid,
            "mt": msgtime,
            "c": content_text,
            "sdk": sdkfileid,
        },
    )
    row = db.execute(
        text("SELECT id FROM archive_messages WHERE msgid=:m AND tenant_id='tenant-a'"),
        {"m": msgid},
    ).fetchone()
    return row[0]


def _seed_recipient(db: Session, mid: int, recv: str) -> None:
    db.execute(
        text(
            "INSERT INTO archive_message_recipients (message_id,receiver_userid,tenant_id) "
            "VALUES (:mid,:r,'tenant-a')"
        ),
        {"mid": mid, "r": recv},
    )


# =====================================================================
# 1. App bootstrap & route registration
# =====================================================================


def test_app_imports_and_has_routes() -> None:
    from app.main import app

    assert len(app.routes) > 10


def test_router_count() -> None:
    from app.main import app

    route_count = len([r for r in app.routes if hasattr(r, "methods")])
    # GH-111 adds one PlatformAdmin-only payment-finding resolution route.
    # RND-405 adds one controlled platform payment-recovery route.
    # RND-386 adds three provisioning config-wizard routes.
    # RND-388 adds one self-service activation route.
    # RND-357 adds the 8 AI support page/API routes.
    # RND-161 adds the proactive user-feedback route.
    # RND-359 adds the internal-staff handoff-resolution route.
    # RND-408 adds the 7 public AI support page/API routes.
    # RND-371 adds the controlled avatar and user-detail routes.
    # RND-162 adds one tenant-scoped collection route and three
    # PlatformAdmin-only aggregate Product Analytics routes.
    # RND-404 adds two Owner billing routes: cancel-at-period-end intent
    # and read-only latest-refund status.
    # RND-413 adds three platform-auth routes: login page GET/POST and
    # logout POST.
    # RND-416 adds one authenticated Alipay cashier redirect and one signed
    # Alipay payment notification route.
    # RND-414 adds the 9-item platform-console side nav: /platform (new
    # canonical dashboard URL — /platform/operations already counted
    # towards the RND-413 baseline and is now just a second route on the
    # same handler, not a new one) plus 8 new HTML shells — tenants
    # list/detail/new, usage, ledger, infra, analytics, audit.
    # RND-415 adds three platform account HTML shells and four guarded/public
    # account APIs: list, invite, invitation acceptance, and self password
    # rotation. GH-122 removes the customer cancel-intent API.
    # RND-366 adds five tenant-scoped favorites API operations.
    assert route_count == 217


def test_routers_are_registered(client: TestClient) -> None:
    from app.main import app

    unique = sorted(
        {r.path for r in app.routes if hasattr(r, "path") and hasattr(r, "methods")}
    )
    expected = sorted(
        [
            "/",
            "/dashboard",
            "/admin/analytics",
            "/admin/audit-logs",
            "/admin/billing",
            "/admin/conversations",
            "/admin/contacts",
            "/admin/diagnostics/reachability",
            "/admin/exports",
            "/admin/forgot-password",
            "/admin/login",
            "/admin/organization/confirm",
            "/admin/provisioning",
            "/admin/provisioning/settings",
            "/admin/media",
            "/admin/messages",
            "/admin/cleanup",
            "/admin/recycle-bin",
            "/admin/messages/{msgid}",
            "/api/admin/messages/delete",
            "/api/admin/messages/deletion-status",
            "/api/admin/messages/cleanup/preview",
            "/api/admin/messages/cleanup/tasks",
            "/api/admin/messages/cleanup/tasks/{task_id}",
            "/api/admin/messages/cleanup/tasks/{task_id}/cancel",
            "/api/admin/messages/recycle-bin",
            "/api/admin/messages/recycle-bin/metrics",
            "/api/admin/messages/purge",
            "/api/admin/messages/restore",
            "/admin/reset-password",
            "/admin/search",
            "/admin/settings",
            "/admin/settings/init",
            "/admin/users",
            "/api/admin/access-requests",
            "/api/admin/access-requests/{request_id}/link",
            "/api/admin/access-requests/{request_id}/create-account",
            "/api/admin/audit-logs",
            "/api/admin/avatars/{identity_type}/{avatar_id}",
            "/api/admin/dashboard",
            "/api/admin/export/approve",
            "/api/admin/export/execute",
            "/api/admin/export/record",
            "/api/admin/exports/jobs",
            "/api/admin/exports/jobs/{job_id}",
            "/api/admin/exports/jobs/{job_id}/download",
            "/api/admin/exports/media",
            "/api/admin/exports/quota",
            "/api/admin/exports/text",
            "/api/admin/external-contacts",
            "/api/admin/external-contacts/{external_userid}",
            "/api/admin/media",
            "/api/admin/media/{media_id}/download",
            "/api/admin/reachability-audit",
            "/api/admin/reachability-checks",
            "/api/admin/reachability-checks/latest",
            "/api/admin/reachability-findings",
            "/api/admin/retention-config",
            "/api/admin/usage",
            "/api/admin/settings",
            "/api/admin/settings/bootstrap",
            "/api/admin/settings/bootstrap-status",
            "/api/admin/settings/export",
            "/api/admin/settings/password",
            "/api/admin/settings/test-connection",
            "/api/admin/sync-now",
            "/api/admin/sync-status",
            "/api/admin/users",
            "/api/admin/users/accept",
            "/api/admin/users/invite",
            "/api/admin/users/invite-batch",
            "/api/admin/users/{user_id}",
            "/api/admin/users/{user_id}/role",
            "/api/admin/users/{user_id}/reset-password",
            "/api/auth/logout",
            "/api/auth/me",
            "/api/auth/me/preferences",
            "/api/auth/password/forgot",
            "/api/auth/password/login",
            "/api/auth/password/reset",
            "/api/auth/wecom/callback",
            "/api/auth/wecom/login",
            "/api/auth/wecom/qr/callback",
            "/api/auth/wecom/qr/login",
            "/api/auth/wecom/third-party/callback",
            "/api/auth/wecom/third-party/install",
            "/api/auth/wecom/organization-claim/cancel",
            "/api/auth/wecom/organization-claim/confirm",
            "/api/billing/orders",
            "/api/billing/capacity",
            "/api/billing/capacity/trend",
            "/api/billing/subscription",
            "/api/billing/orders/latest",
            "/api/billing/orders/{order_id}",
            "/api/billing/orders/{order_id}/close",
            "/api/billing/orders/{order_id}/checkout",
            "/api/billing/orders/{order_id}/qr",
            "/api/billing/orders/{order_id}/refresh",
            "/api/billing/plan",
            "/api/billing/refunds/latest",
            "/api/branding",
            "/api/branding/logo",
            "/api/branding/favicon",
            "/api/branding/manifest.webmanifest",
            "/api/branding/domain",
            "/api/branding/domain/verify",
            "/api/branding/domain/enable",
            "/api/branding/domain/disable",
            "/api/contacts",
            "/api/conversations",
            "/api/conversations/{conversation_id}/detail",
            "/api/conversations/{conversation_id}/messages",
            "/api/conversations/{conversation_id}/messages/{msgid}/media",
            "/api/conversations/{conversation_id}/messages/{msgid}/media/access",
            "/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}",
            "/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}/access",
            "/api/favorites",
            "/api/favorites/batch",
            "/api/favorites/status",
            "/api/favorites/{object_type}/{object_id}",
            "/api/messages",
            "/api/messages/{msgid}",
            "/api/monitored-accounts",
            "/api/onboarding/complete",
            "/api/onboarding/status",
            "/api/platform/content-access-requests",
            "/api/platform/account/password",
            "/api/platform/operators",
            "/api/platform/operators/invitations",
            "/api/platform/operators/accept-invite",
            "/api/platform/branding/domains",
            "/api/platform/branding/domain-metrics",
            "/api/platform/operations/dashboard",
            "/api/platform/operations/payment-findings/{finding_id}/resolve",
            "/api/platform/operations/product-analytics/overview",
            "/api/platform/operations/product-analytics/tenants",
            "/api/platform/operations/product-analytics/tenants/{tenant_id}",
            "/api/platform/operations/tenants",
            "/api/platform/operations/tenants/{tenant_id}",
            "/api/platform/operations/tenants/{tenant_id}/financial-transactions",
            "/api/platform/operations/tenants/{tenant_id}/payments/{order_id}/query",
            "/api/platform/operations/tenants/{tenant_id}/refunds",
            "/api/platform/operations/tenants/{tenant_id}/refunds/{refund_id}/query",
            "/api/platform/operations/tenants/{tenant_id}/service",
            "/api/platform/operations/tenants/{tenant_id}/subscription",
            "/api/platform/tenants",
            "/api/platform/tenants/usage",
            "/api/platform/tenants/{tenant_id}",
            "/api/platform/tenants/{tenant_id}/connectivity-check",
            "/api/platform/tenants/{tenant_id}/branding/certificate-status",
            "/api/platform/wecom/third-party/suite-ticket-status",
            "/admin/support",
            "/api/ai/support/status",
            "/api/ai/support/sessions",
            "/api/ai/support/sessions/{chat_session_id}",
            "/api/ai/support/sessions/{chat_session_id}/messages",
            "/api/ai/support/messages/{message_id}/feedback",
            "/api/ai/support/sessions/{chat_session_id}/handoff/preview",
            "/api/ai/support/sessions/{chat_session_id}/handoff",
            "/api/ai/support/feedback",
            "/api/platform/ai/handoffs/{handoff_id}/resolve",
            "/public/support",
            "/api/ai/public/support/status",
            "/api/ai/public/support/sessions",
            "/api/ai/public/support/sessions/{chat_session_id}",
            "/api/ai/public/support/sessions/{chat_session_id}/messages",
            "/api/ai/public/support/sessions/{chat_session_id}/handoff/preview",
            "/api/ai/public/support/sessions/{chat_session_id}/handoff",
            "/api/payments/alipay/notify",
            "/api/payments/wechat/notify",
            "/api/product-analytics/events",
            "/api/provisioning/activate",
            "/api/provisioning/config",
            "/api/provisioning/config/test",
            "/api/provisioning/status",
            "/api/refunds/wechat/notify",
            "/api/search/contacts",
            "/api/search/messages",
            "/api/wecom/archive/events",
            "/api/wecom/third-party/instructions",
            "/docs",
            "/docs/oauth2-redirect",
            "/health",
            "/health/live",
            "/health/ready",
            "/openapi.json",
            "/platform",
            "/platform/accept-invite",
            "/platform/login",
            "/platform/logout",
            "/platform/operations",
            "/platform/settings",
            "/platform/settings/operators/new",
            "/platform/tenants",
            "/platform/tenants/new",
            "/platform/tenants/{tenant_id}",
            "/platform/usage",
            "/platform/ledger",
            "/platform/infra",
            "/platform/analytics",
            "/platform/audit",
            "/redoc",
        ]
    )
    missing = set(expected) - set(unique)
    extra = set(unique) - set(expected)
    assert not missing, f"missing: {sorted(missing)}"
    assert not extra, f"extra: {sorted(extra)}"


def test_route_snapshot_with_real_model_names() -> None:
    from app.main import app

    docs = {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
    actual = []
    for r in app.routes:
        if not hasattr(r, "methods") or not hasattr(r, "path"):
            continue
        if r.path in docs:
            continue
        actual.append(
            (
                r.path,
                frozenset(r.methods) if r.methods else frozenset(),
                _snapshot_response_model(r),
                _snapshot_response_class(r),
            )
        )
    expected = [
        ("/", frozenset({"GET"}), "None", "RedirectResponse"),
        ("/dashboard", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/analytics", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/audit-logs", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/billing", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/conversations", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/contacts", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/diagnostics/reachability", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/exports", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/forgot-password", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/login", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/organization/confirm", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/provisioning", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/provisioning/settings", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/media", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/recycle-bin", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/cleanup", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/messages", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/messages/{msgid}", frozenset({"GET"}), "None", "HTMLResponse"),
        (
            "/api/admin/messages/delete",
            frozenset({"POST"}),
            "MessageDeleteOut",
            "None",
        ),
        (
            "/api/admin/messages/deletion-status",
            frozenset({"GET"}),
            "DeletionStatusOut",
            "None",
        ),
        (
            "/api/admin/messages/recycle-bin",
            frozenset({"GET"}),
            "RecycleBinOut",
            "None",
        ),
        (
            "/api/admin/messages/recycle-bin/metrics",
            frozenset({"GET"}),
            "PurgeMetricsOut",
            "None",
        ),
        (
            "/api/admin/messages/cleanup/preview",
            frozenset({"POST"}),
            "CleanupPreviewOut",
            "None",
        ),
        (
            "/api/admin/messages/cleanup/tasks",
            frozenset({"POST"}),
            "CleanupTaskOut",
            "None",
        ),
        (
            "/api/admin/messages/cleanup/tasks",
            frozenset({"GET"}),
            "CleanupTaskListOut",
            "None",
        ),
        (
            "/api/admin/messages/cleanup/tasks/{task_id}",
            frozenset({"GET"}),
            "CleanupTaskOut",
            "None",
        ),
        (
            "/api/admin/messages/cleanup/tasks/{task_id}/cancel",
            frozenset({"POST"}),
            "CleanupTaskOut",
            "None",
        ),
        (
            "/api/admin/messages/purge",
            frozenset({"POST"}),
            "PurgeOut",
            "None",
        ),
        (
            "/api/admin/messages/restore",
            frozenset({"POST"}),
            "MessageDeleteOut",
            "None",
        ),
        ("/admin/reset-password", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/search", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/settings", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/settings/init", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/admin/users", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/accept-invite", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/login", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/login", frozenset({"POST"}), "None", "HTMLResponse"),
        ("/platform/logout", frozenset({"POST"}), "None", "None"),
        ("/platform/operations", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/tenants", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/tenants/new", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/tenants/{tenant_id}", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/usage", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/ledger", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/infra", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/analytics", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/audit", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/platform/settings", frozenset({"GET"}), "None", "HTMLResponse"),
        (
            "/platform/settings/operators/new",
            frozenset({"GET"}),
            "None",
            "HTMLResponse",
        ),
        ("/api/admin/audit-logs", frozenset({"GET"}), "AuditLogListOut", "None"),
        (
            "/api/admin/avatars/{identity_type}/{avatar_id}",
            frozenset({"GET"}),
            "None",
            "None",
        ),
        ("/api/admin/dashboard", frozenset({"GET"}), "DashboardOut", "None"),
        ("/api/admin/export/approve", frozenset({"POST"}), "None", "None"),
        ("/api/admin/export/execute", frozenset({"POST"}), "None", "None"),
        ("/api/admin/export/record", frozenset({"POST"}), "None", "None"),
        ("/api/admin/exports/jobs", frozenset({"GET"}), "None", "None"),
        (
            "/api/admin/exports/jobs/{job_id}",
            frozenset({"GET"}),
            "None",
            "None",
        ),
        (
            "/api/admin/exports/jobs/{job_id}/download",
            frozenset({"GET"}),
            "None",
            "None",
        ),
        ("/api/admin/exports/media", frozenset({"POST"}), "None", "None"),
        ("/api/admin/exports/quota", frozenset({"GET"}), "None", "None"),
        ("/api/admin/exports/text", frozenset({"POST"}), "None", "None"),
        (
            "/api/admin/external-contacts",
            frozenset({"GET"}),
            "ExternalContactListPage",
            "None",
        ),
        (
            "/api/admin/external-contacts/{external_userid}",
            frozenset({"GET"}),
            "ExternalContactDetail",
            "None",
        ),
        ("/api/admin/media", frozenset({"GET"}), "MediaLibraryPage", "None"),
        (
            "/api/admin/media/{media_id}/download",
            frozenset({"GET"}),
            "None",
            "None",
        ),
        (
            "/api/admin/reachability-audit",
            frozenset({"GET"}),
            "ReachabilityAuditOut",
            "None",
        ),
        (
            "/api/admin/reachability-checks",
            frozenset({"POST"}),
            "ReachabilityCheckSnapshotOut",
            "None",
        ),
        (
            "/api/admin/reachability-checks/latest",
            frozenset({"GET"}),
            "ReachabilityCheckSnapshotOut",
            "None",
        ),
        (
            "/api/admin/reachability-findings",
            frozenset({"GET"}),
            "ReachabilityFindingListOut",
            "None",
        ),
        ("/api/admin/retention-config", frozenset({"GET"}), "RetentionConfigOut", "None"),
        ("/api/admin/retention-config", frozenset({"PUT"}), "RetentionConfigOut", "None"),
        ("/api/admin/usage", frozenset({"GET"}), "UsageAnalyticsOut", "None"),
        ("/api/admin/settings", frozenset({"GET"}), "SettingsGetOut", "None"),
        ("/api/admin/settings", frozenset({"PUT"}), "SettingsUpdateOut", "None"),
        ("/api/admin/settings/bootstrap", frozenset({"POST"}), "dict", "None"),
        ("/api/admin/settings/bootstrap-status", frozenset({"GET"}), "dict", "None"),
        ("/api/admin/settings/export", frozenset({"GET"}), "None", "PlainTextResponse"),
        (
            "/api/admin/settings/password",
            frozenset({"POST"}),
            "None",
            "None",
        ),
        (
            "/api/admin/settings/test-connection",
            frozenset({"POST"}),
            "dict",
            "None",
        ),
        ("/api/admin/sync-now", frozenset({"POST"}), "SyncNowResponse", "None"),
        ("/api/admin/sync-status", frozenset({"GET"}), "SyncStatusResponse", "None"),
        ("/api/admin/access-requests", frozenset({"GET"}), "None", "None"),
        (
            "/api/admin/access-requests/{request_id}/link",
            frozenset({"POST"}),
            "None",
            "None",
        ),
        (
            "/api/admin/access-requests/{request_id}/create-account",
            frozenset({"POST"}),
            "None",
            "None",
        ),
        ("/api/admin/users", frozenset({"GET"}), "AdminUserListOut", "None"),
        ("/api/admin/users/accept", frozenset({"POST"}), "None", "None"),
        ("/api/admin/users/invite", frozenset({"POST"}), "None", "None"),
        ("/api/admin/users/invite-batch", frozenset({"POST"}), "None", "None"),
        ("/api/admin/users/{user_id}", frozenset({"GET"}), "AdminUserDetail", "None"),
        ("/api/admin/users/{user_id}", frozenset({"PATCH"}), "None", "None"),
        (
            "/api/admin/users/{user_id}/role",
            frozenset({"PATCH"}),
            "None",
            "None",
        ),
        (
            "/api/admin/users/{user_id}/reset-password",
            frozenset({"POST"}),
            "None",
            "None",
        ),
        ("/api/auth/logout", frozenset({"POST"}), "None", "None"),
        ("/api/auth/me", frozenset({"GET"}), "None", "None"),
        (
            "/api/auth/me/preferences",
            frozenset({"PUT"}),
            "PreferencesOut",
            "None",
        ),
        ("/api/auth/password/forgot", frozenset({"POST"}), "None", "None"),
        ("/api/auth/password/login", frozenset({"POST"}), "None", "None"),
        ("/api/auth/password/reset", frozenset({"POST"}), "None", "None"),
        ("/api/auth/wecom/callback", frozenset({"GET"}), "None", "None"),
        ("/api/auth/wecom/login", frozenset({"GET"}), "None", "None"),
        ("/api/auth/wecom/qr/callback", frozenset({"GET"}), "None", "None"),
        ("/api/auth/wecom/qr/login", frozenset({"GET"}), "None", "None"),
        ("/api/auth/wecom/third-party/callback", frozenset({"GET"}), "None", "RedirectResponse"),
        ("/api/auth/wecom/third-party/install", frozenset({"GET"}), "None", "RedirectResponse"),
        ("/api/auth/wecom/organization-claim/cancel", frozenset({"POST"}), "None", "RedirectResponse"),
        ("/api/auth/wecom/organization-claim/confirm", frozenset({"POST"}), "None", "RedirectResponse"),
        ("/api/billing/plan", frozenset({"GET"}), "BillingPlanOut", "None"),
        (
            "/api/billing/capacity",
            frozenset({"GET"}),
            "StorageCapacityOut",
            "None",
        ),
        (
            "/api/billing/capacity/trend",
            frozenset({"GET"}),
            "StorageTrendOut",
            "None",
        ),
        (
            "/api/billing/subscription",
            frozenset({"GET"}),
            "SubscriptionOverviewOut",
            "None",
        ),
        (
            "/api/billing/refunds/latest",
            frozenset({"GET"}),
            "Optional[RefundOut]",
            "None",
        ),
        (
            "/api/billing/orders/latest",
            frozenset({"GET"}),
            "Optional[PaymentOrderOut]",
            "None",
        ),
        (
            "/api/billing/orders/{order_id}",
            frozenset({"GET"}),
            "PaymentOrderOut",
            "None",
        ),
        ("/api/billing/orders", frozenset({"POST"}), "PaymentOrderOut", "None"),
        ("/api/billing/orders/{order_id}/qr", frozenset({"GET"}), "None", "None"),
        ("/api/billing/orders/{order_id}/checkout", frozenset({"GET"}), "None", "None"),
        (
            "/api/billing/orders/{order_id}/refresh",
            frozenset({"POST"}),
            "PaymentOrderOut",
            "None",
        ),
        (
            "/api/billing/orders/{order_id}/close",
            frozenset({"POST"}),
            "PaymentOrderOut",
            "None",
        ),
        ("/api/branding", frozenset({"GET"}), "BrandingStatusOut", "None"),
        ("/api/branding/logo", frozenset({"GET"}), "None", "None"),
        ("/api/branding/favicon", frozenset({"GET"}), "None", "None"),
        ("/api/branding/manifest.webmanifest", frozenset({"GET"}), "None", "None"),
        ("/api/branding/logo", frozenset({"PUT"}), "BrandingStatusOut", "None"),
        ("/api/branding/logo", frozenset({"DELETE"}), "BrandingStatusOut", "None"),
        ("/api/branding/favicon", frozenset({"PUT"}), "BrandingStatusOut", "None"),
        ("/api/branding/favicon", frozenset({"DELETE"}), "BrandingStatusOut", "None"),
        ("/api/branding/domain", frozenset({"POST"}), "None", "None"),
        ("/api/branding/domain/verify", frozenset({"POST"}), "BrandingStatusOut", "None"),
        ("/api/branding/domain/enable", frozenset({"POST"}), "BrandingStatusOut", "None"),
        ("/api/branding/domain/disable", frozenset({"POST"}), "BrandingStatusOut", "None"),
        ("/api/branding/domain", frozenset({"DELETE"}), "BrandingStatusOut", "None"),
        ("/api/contacts", frozenset({"GET"}), "list[ContactOut]", "None"),
        ("/api/conversations", frozenset({"GET"}), "list[ConversationOut]", "None"),
        ("/api/favorites", frozenset({"POST"}), "FavoriteMutationOut", "None"),
        ("/api/favorites", frozenset({"GET"}), "FavoritePageOut", "None"),
        ("/api/favorites/batch", frozenset({"POST"}), "FavoriteMutationOut", "None"),
        ("/api/favorites/status", frozenset({"POST"}), "FavoriteStatusOut", "None"),
        (
            "/api/favorites/{object_type}/{object_id}",
            frozenset({"DELETE"}),
            "FavoriteMutationOut",
            "None",
        ),
        (
            "/api/conversations/{conversation_id}/detail",
            frozenset({"GET"}),
            "ConversationDetailOut",
            "None",
        ),
        (
            "/api/conversations/{conversation_id}/messages",
            frozenset({"GET"}),
            "ConversationMessagesOut",
            "None",
        ),
        (
            "/api/conversations/{conversation_id}/messages/{msgid}/media",
            frozenset({"GET"}),
            "None",
            "None",
        ),
        (
            "/api/conversations/{conversation_id}/messages/{msgid}/media/access",
            frozenset({"GET"}),
            "MediaAccessOut",
            "None",
        ),
        (
            "/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}",
            frozenset({"GET"}),
            "None",
            "None",
        ),
        (
            "/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}/access",
            frozenset({"GET"}),
            "NestedMediaAccessOut",
            "None",
        ),
        ("/api/messages", frozenset({"GET"}), "list[MessageOut]", "None"),
        ("/api/messages/{msgid}", frozenset({"GET"}), "MessageDetailOut", "None"),
        (
            "/api/monitored-accounts",
            frozenset({"GET"}),
            "list[MonitoredAccountOut]",
            "None",
        ),
        ("/api/onboarding/complete", frozenset({"POST"}), "OnboardingCompleteOut", "None"),
        ("/api/onboarding/status", frozenset({"GET"}), "OnboardingStatusOut", "None"),
        (
            "/api/platform/content-access-requests",
            frozenset({"POST"}),
            "ContentAccessRequestOut",
            "None",
        ),
        (
            "/api/platform/account/password",
            frozenset({"POST"}),
            "PlatformPasswordChangeOut",
            "None",
        ),
        (
            "/api/platform/operators",
            frozenset({"GET"}),
            "PlatformOperatorListOut",
            "None",
        ),
        (
            "/api/platform/operators/invitations",
            frozenset({"POST"}),
            "PlatformOperatorInviteOut",
            "None",
        ),
        (
            "/api/platform/operators/accept-invite",
            frozenset({"POST"}),
            "None",
            "None",
        ),
        (
            "/api/platform/branding/domains",
            frozenset({"GET"}),
            "list[ManagedBrandingDomainOut]",
            "None",
        ),
        (
            "/api/platform/branding/domain-metrics",
            frozenset({"GET"}),
            "BrandingDomainMetricsOut",
            "None",
        ),
        (
            "/api/platform/operations/dashboard",
            frozenset({"GET"}),
            "PlatformOperationsDashboardOut",
            "None",
        ),
        (
            "/api/platform/operations/payment-findings/{finding_id}/resolve",
            frozenset({"POST"}),
            "PaymentRecoveryFindingResolutionOut",
            "None",
        ),
        (
            "/api/platform/operations/tenants",
            frozenset({"GET"}),
            "TenantOperationsListOut",
            "None",
        ),
        (
            "/api/platform/operations/tenants/{tenant_id}",
            frozenset({"GET"}),
            "TenantOperationsDetailOut",
            "None",
        ),
        (
            "/api/platform/operations/tenants/{tenant_id}/service",
            frozenset({"PATCH"}),
            "TenantServiceStatusOut",
            "None",
        ),
        (
            "/api/platform/operations/tenants/{tenant_id}/subscription",
            frozenset({"PUT"}),
            "ManualSubscriptionOut",
            "None",
        ),
        (
            "/api/platform/operations/tenants/{tenant_id}/financial-transactions",
            frozenset({"POST"}),
            "ManualFinancialTransactionOut",
            "None",
        ),
        (
            "/api/platform/operations/tenants/{tenant_id}/payments/{order_id}/query",
            frozenset({"POST"}),
            "PaymentOrderOut",
            "None",
        ),
        (
            "/api/platform/operations/tenants/{tenant_id}/refunds",
            frozenset({"POST"}),
            "RefundOut",
            "None",
        ),
        (
            "/api/platform/operations/tenants/{tenant_id}/refunds/{refund_id}/query",
            frozenset({"POST"}),
            "RefundOut",
            "None",
        ),
        (
            "/api/product-analytics/events",
            frozenset({"POST"}),
            "ProductAnalyticsEventAcceptedOut",
            "None",
        ),
        (
            "/api/platform/operations/product-analytics/overview",
            frozenset({"GET"}),
            "ProductAnalyticsOverviewOut",
            "None",
        ),
        (
            "/api/platform/operations/product-analytics/tenants",
            frozenset({"GET"}),
            "ProductAnalyticsTenantListOut",
            "None",
        ),
        (
            "/api/platform/operations/product-analytics/tenants/{tenant_id}",
            frozenset({"GET"}),
            "ProductAnalyticsTenantDetailOut",
            "None",
        ),
        ("/api/platform/tenants", frozenset({"POST"}), "TenantProvisionOut", "None"),
        (
            "/api/platform/tenants",
            frozenset({"GET"}),
            "TenantListOut",
            "None",
        ),
        (
            "/api/platform/tenants/usage",
            frozenset({"GET"}),
            "TenantUsageListOut",
            "None",
        ),
        (
            "/api/platform/tenants/{tenant_id}",
            frozenset({"PATCH"}),
            "TenantStatusUpdateOut",
            "None",
        ),
        (
            "/api/platform/tenants/{tenant_id}/connectivity-check",
            frozenset({"POST"}),
            "TenantConnectivityCheckOut",
            "None",
        ),
        (
            "/api/platform/tenants/{tenant_id}/branding/certificate-status",
            frozenset({"POST"}),
            "BrandingStatusOut",
            "None",
        ),
        (
            "/api/platform/wecom/third-party/suite-ticket-status",
            frozenset({"GET"}),
            "None",
            "None",
        ),
        ("/api/payments/alipay/notify", frozenset({"POST"}), "None", "None"),
        ("/api/payments/wechat/notify", frozenset({"POST"}), "None", "None"),
        ("/api/provisioning/activate", frozenset({"POST"}), "dict", "None"),
        ("/api/provisioning/config", frozenset({"GET"}), "dict", "None"),
        ("/api/provisioning/config", frozenset({"PUT"}), "dict", "None"),
        ("/api/provisioning/config/test", frozenset({"POST"}), "dict", "None"),
        ("/api/provisioning/status", frozenset({"GET"}), "dict", "None"),
        ("/admin/support", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/api/ai/support/status", frozenset({"GET"}), "AiSupportStatusOut", "None"),
        ("/api/ai/support/sessions", frozenset({"POST"}), "AiChatSessionOut", "None"),
        ("/api/ai/support/sessions/{chat_session_id}", frozenset({"DELETE"}), "None", "None"),
        ("/api/ai/support/sessions/{chat_session_id}/messages", frozenset({"POST"}), "None", "None"),
        ("/api/ai/support/messages/{message_id}/feedback", frozenset({"POST"}), "dict", "None"),
        (
            "/api/ai/support/sessions/{chat_session_id}/handoff/preview",
            frozenset({"POST"}),
            "HandoffPreviewOut",
            "None",
        ),
        ("/api/ai/support/sessions/{chat_session_id}/handoff", frozenset({"POST"}), "HandoffOut", "None"),
        (
            "/api/platform/ai/handoffs/{handoff_id}/resolve",
            frozenset({"POST"}),
            "HandoffResolveOut",
            "None",
        ),
        ("/api/ai/support/feedback", frozenset({"POST"}), "UserFeedbackOut", "None"),
        ("/public/support", frozenset({"GET"}), "None", "HTMLResponse"),
        ("/api/ai/public/support/status", frozenset({"GET"}), "_AiPublicSupportStatusOut", "None"),
        ("/api/ai/public/support/sessions", frozenset({"POST"}), "_AiPublicChatSessionOut", "None"),
        ("/api/ai/public/support/sessions/{chat_session_id}", frozenset({"DELETE"}), "None", "None"),
        ("/api/ai/public/support/sessions/{chat_session_id}/messages", frozenset({"POST"}), "None", "None"),
        (
            "/api/ai/public/support/sessions/{chat_session_id}/handoff/preview",
            frozenset({"POST"}),
            "_HandoffPreviewOut",
            "None",
        ),
        ("/api/ai/public/support/sessions/{chat_session_id}/handoff", frozenset({"POST"}), "_HandoffOut", "None"),
        ("/api/refunds/wechat/notify", frozenset({"POST"}), "None", "None"),
        (
            "/api/search/contacts",
            frozenset({"GET"}),
            "list[ContactSearchResult]",
            "None",
        ),
        (
            "/api/search/messages",
            frozenset({"GET"}),
            "MessageSearchResponse",
            "None",
        ),
        ("/api/wecom/archive/events", frozenset({"GET"}), "None", "None"),
        ("/api/wecom/archive/events", frozenset({"POST"}), "None", "None"),
        ("/api/wecom/third-party/instructions", frozenset({"GET"}), "None", "None"),
        ("/api/wecom/third-party/instructions", frozenset({"POST"}), "None", "None"),
        ("/health", frozenset({"GET"}), "None", "None"),
        ("/health/live", frozenset({"GET"}), "None", "None"),
        ("/health/ready", frozenset({"GET"}), "None", "None"),
    ]
    assert sorted(actual) == sorted(expected), (
        "Route snapshot mismatch — update expected list if intentional."
    )


# =====================================================================
# 2. Public routes
# =====================================================================


class TestPublicRoutes:
    def test_health_live_always_ok(self, client: TestClient) -> None:
        resp = client.get("/health/live")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        assert resp.json() == {"status": "ok"}

    def test_health(self, client: TestClient) -> None:
        # RND-227: /health is now a real readiness check (DB connectivity
        # + schema revision), not a static "ok" — this test file's fixture
        # never sets DATABASE_URL / runs Alembic, so the honest result
        # here is 503, not a masked 200. See
        # tests/test_readiness_health_endpoint.py for the 200-on-a-real-
        # migrated-database and 503-on-drift live-Postgres coverage.
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.headers["content-type"] == "application/json"
        assert resp.json() == {"status": "unavailable"}

    def test_root_is_a_temporary_relative_redirect_to_the_canonical_login(
        self, client: TestClient
    ) -> None:
        first = client.get("/", follow_redirects=False)
        assert first.status_code == 302
        assert first.headers["location"] == "/admin/login"
        assert "session_id" not in first.headers.get("set-cookie", "")

        final = client.get("/", follow_redirects=True)
        assert [hop.status_code for hop in final.history] == [302]
        assert [hop.headers["location"] for hop in final.history] == ["/admin/login"]
        assert final.status_code == 200
        assert final.url.path == "/admin/login"
        assert "text/html" in final.headers["content-type"]

    def test_login_page(self, client: TestClient) -> None:
        resp = client.get("/admin/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_login_with_error_params(self, client: TestClient) -> None:
        for code in ("invalid_state", "auth_failed", "user_inactive", "config_error"):
            resp = client.get(f"/admin/login?error={code}")
            assert resp.status_code == 200
            assert "text/html" in resp.headers["content-type"]

    def test_wecom_login_redirects(self, client: TestClient) -> None:
        resp = client.get("/api/auth/wecom/login", follow_redirects=False)
        assert resp.status_code == 302
        assert "config_error" in resp.headers.get("location", "")

    def test_wecom_callback_requires_params(self, client: TestClient) -> None:
        assert (
            client.get("/api/auth/wecom/callback", follow_redirects=False).status_code
            == 422
        )

    def test_password_login_disabled(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/password/login", json={"username": "x", "password": "x"}
        )
        assert resp.status_code == 404

    def test_wecom_events_not_session_gated(self, client: TestClient) -> None:
        for m in ("GET", "POST"):
            assert client.request(m, "/api/wecom/archive/events").status_code != 401


class TestProductEntryBoundaries:
    def test_authenticated_root_uses_existing_login_session_redirect(
        self, authed_html_client: TestClient
    ) -> None:
        final = authed_html_client.get("/", follow_redirects=True)

        assert [hop.status_code for hop in final.history] == [302]
        assert [hop.headers["location"] for hop in final.history] == ["/dashboard"]
        assert final.status_code == 200
        assert final.url.path == "/dashboard"
        assert "text/html" in final.headers["content-type"]

    def test_unknown_path_remains_a_404_not_a_login_redirect(
        self, client: TestClient
    ) -> None:
        response = client.get("/rnd-261-unknown-path", follow_redirects=False)

        assert response.status_code == 404
        assert "location" not in response.headers
        assert response.json() == {"detail": "Not Found"}

    def test_root_does_not_capture_existing_health_api_static_or_wecom_routes(
        self, client: TestClient
    ) -> None:
        health = client.get("/health/live", follow_redirects=False)
        api = client.get("/api/monitored-accounts", follow_redirects=False)
        static = client.get("/web/static/base.css", follow_redirects=False)
        oauth_callback = client.get("/api/auth/wecom/callback", follow_redirects=False)
        event_callback = client.get("/api/wecom/archive/events", follow_redirects=False)

        assert health.status_code == 200
        assert api.status_code == 401
        assert static.status_code == 200
        assert static.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert oauth_callback.status_code == 422
        assert event_callback.status_code == 422


# =====================================================================
# 3. Auth gates
# =====================================================================


class TestAuthGates:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/monitored-accounts",
            "/api/contacts",
            "/api/conversations",
            "/api/conversations/any/messages",
            "/api/messages",
            "/api/messages/any",
            "/api/messages/any-id",
            "/api/admin/reachability-audit",
            "/api/admin/reachability-checks/latest",
            "/api/admin/reachability-findings",
            "/api/admin/sync-status",
            "/api/admin/users",
        ],
    )
    def test_api_401(self, client: TestClient, path: str) -> None:
        resp = client.get(path)
        assert resp.status_code == 401, f"{path}: {resp.status_code}"
        assert resp.headers["content-type"] == "application/json"

    @pytest.mark.parametrize(
        "path",
        [
            "/admin/conversations",
            "/dashboard",
            "/admin/diagnostics/reachability",
            "/admin/messages",
            "/admin/messages/any",
        ],
    )
    def test_html_302(self, client: TestClient, path: str) -> None:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin/login"

    def test_auth_me_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is False
        extra = set(body.keys()) - {"authenticated", "user", "message", "wecom_userid"}
        assert not extra, f"unexpected keys: {extra}"


# =====================================================================
# 4. Authenticated shapes — non‑empty seed data
# =====================================================================


class _ShapeSeed:
    @staticmethod
    def seed(db: Session) -> None:
        _seed_tenant_a(db)
        db.execute(
            text(
                "INSERT INTO admin_users (id,tenant_id,wecom_user_id,name) "
                "VALUES ('user-001','tenant-a','staff_alice','Alice')"
            )
        )
        db.execute(
            text(
                "INSERT INTO admin_sessions (id,admin_user_id,tenant_id,wecom_user_id,expires_at) "
                "VALUES ('session-001','user-001','tenant-a','staff_alice','2099-01-01T00:00:00+00:00')"
            )
        )
        _seed_contact(db, "staff_alice", "Alice")
        _seed_contact(db, "contact_bob", "Bob")
        mid = _seed_message(
            db,
            msgid="msg-1",
            sender="staff_alice",
            msgtype="text",
            content_text="hello",
        )
        _seed_recipient(db, mid, "contact_bob")
        db.commit()


class TestAuthenticatedShapes:
    def test_auth_me(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        resp = authed_client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is True

    def test_auth_logout(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        resp = authed_client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["logged_out"] is True

    def test_monitored_accounts(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        resp = authed_client.get("/api/monitored-accounts")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list) and len(body) > 0
        assert "staff_id" in body[0]

    def test_contacts(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        resp = authed_client.get("/api/contacts")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list) and len(body) > 0
        for f in ("contact_id", "display_name", "raw_id"):
            assert f in body[0], f"ContactOut missing '{f}'"

    def test_conversations(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        resp = authed_client.get("/api/conversations?mode=staff&staff_id=staff_alice")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list) and len(body) > 0
        for f in ("conversation_id", "conversation_type", "display_name"):
            assert f in body[0], f"ConversationOut missing '{f}'"

    def test_conversations_missing_mode(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        assert authed_client.get("/api/conversations").status_code == 422

    def test_conversations_invalid_mode(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        assert authed_client.get("/api/conversations?mode=invalid").status_code == 400

    def test_messages(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        resp = authed_client.get("/api/messages")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list) and len(body) > 0
        for f in ("msgid", "msgtype", "sender", "msgtime"):
            assert f in body[0], f"MessageOut missing '{f}'"

    def test_messages_invalid_limit(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        assert authed_client.get("/api/messages?limit=0").status_code == 422

    def test_messages_query_params(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        resp = authed_client.get(
            "/api/messages?sender=staff_alice&q=hello&msgtype=text&limit=10"
        )
        assert resp.status_code == 200

    def test_reachability_audit(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _ShapeSeed.seed(db_session)
        resp = authed_client.get(
            "/api/admin/reachability-audit?conversation_id=any&message_type=2&limit=10&include_samples=true"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "scanned_count" in body, (
            f"ReachabilityAuditOut missing fields: {list(body.keys())}"
        )
        assert "matching_total" in body
        assert "has_more" in body


class TestTimelineShape:
    """Timeline & message-page response shape contracts."""

    def test_timeline_returns_messages_and_pagination(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _seed_tenant_a(db_session)
        _seed_contact(db_session, "staff_alice", "Alice")
        _seed_contact(db_session, "contact_bob", "Bob")
        mid = _seed_message(
            db_session,
            msgid="msg-1",
            sender="staff_alice",
            msgtype="text",
            content_text="hi",
        )
        _seed_recipient(db_session, mid, "contact_bob")
        db_session.commit()
        resp = authed_client.get(
            "/api/conversations/direct__contact_bob___staff_alice/messages"
            "?mode=staff&staff_id=staff_alice&limit=10"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "messages" in body, f"missing 'messages': {list(body.keys())}"
        assert isinstance(body["messages"], list) and len(body["messages"]) > 0
        assert "pagination" in body, f"missing pagination: {sorted(body.keys())}"


# 5. Headers — Cache‑Control & content‑type
# =====================================================================


class TestHeaderContracts:
    def test_media_access_no_store_unauth(self, client: TestClient) -> None:
        resp = client.get(
            "/api/conversations/any/messages/any/media/access"
            "?mode=staff&staff_id=x&conversation_type=direct"
        )
        assert resp.headers.get("cache-control", "").lower() == "no-store"

    def test_nested_media_access_no_store_unauth(self, client: TestClient) -> None:
        resp = client.get(
            "/api/conversations/any/messages/any/nested-media/0/access?variant=thumb"
        )
        assert resp.headers.get("cache-control", "").lower() == "no-store"

    def test_media_access_no_store_handler_error(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        _seed_tenant_a(db_session)
        db_session.commit()
        resp = authed_client.get(
            "/api/conversations/nonexistent/messages/nonexistent/media/access"
            "?mode=staff&staff_id=x&conversation_type=direct"
        )
        assert resp.status_code == 404
        assert resp.headers.get("cache-control", "").lower() == "no-store"

    def test_media_access_no_store_success(
        self,
        authed_client: TestClient,
        db_session: Session,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """200 media access descriptor (local proxy) → Cache‑Control: no-store + MediaAccessOut body."""
        _seed_tenant_a(db_session)

        media_root = tmp_path / "media"
        media_root.mkdir()
        img_path = media_root / "photo.jpg"
        img_path.write_bytes(b"fake-jpeg")
        monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

        from app.db.models import ArchiveMessage, MediaFile

        msg = ArchiveMessage(
            msgid="media-msg",
            seq=1,
            publickey_ver=1,
            encrypt_random_key="x",
            encrypt_chat_msg="y",
            decrypt_status="success",
            msgtype="image",
            sender="staff_a",
            roomid="roomM",
            sdkfileid="sdk-123",
            tenant_id="tenant-a",
            msgtime=100,
        )
        db_session.add(msg)
        db_session.commit()
        db_session.refresh(msg)

        mf = MediaFile(
            tenant_id="tenant-a",
            sdkfileid="sdk-123",
            archive_message_id=msg.id,
            download_status="downloaded",
            local_path=str(img_path),
            file_type="image", file_size=len(img_path.read_bytes()),
        )
        db_session.add(mf)
        db_session.commit()

        resp = authed_client.get(
            "/api/conversations/roomM/messages/media-msg/media/access"
            "?mode=staff&staff_id=staff_alice"
        )
        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        assert resp.headers.get("cache-control", "").lower() == "no-store"
        body = resp.json()
        assert body.get("access_type") in ("proxy", "signed_url")
        assert "url" in body
        assert body.get("storage_backend") == "local"
        assert body.get("content_type") == "image/jpeg"

    def test_api_errors_are_json(self, client: TestClient) -> None:
        for path, exp in [("/api/monitored-accounts", 401), ("/api/nonexistent", 404)]:
            resp = client.get(path)
            assert resp.status_code == exp
            assert "application/json" in resp.headers.get("content-type", "")

    def test_html_pages_return_html(self, client: TestClient) -> None:
        for path in (
            "/admin/login",
            "/admin/conversations",
            "/admin/messages",
            "/admin/diagnostics/reachability",
        ):
            resp = client.get(path, follow_redirects=False)
            if resp.status_code == 302:
                continue
            assert "text/html" in resp.headers.get("content-type", ""), path


# =====================================================================
# 6. Frontend HTML structure baseline
# =====================================================================


class TestFrontendBaseline:
    def test_page_is_html(self, authed_html_client: TestClient) -> None:
        resp = authed_html_client.get("/admin/conversations", follow_redirects=False)
        assert resp.status_code == 200
        assert resp.text.strip().lower().startswith("<!doctype html>")

    def test_periodic_browser_refresh_is_absent(self, authed_html_client: TestClient) -> None:
        resp = authed_html_client.get("/admin/conversations", follow_redirects=False)
        html = resp.text
        # RND-216/RND-217: console code is loaded from ordered static modules,
        # so inspect the real module sources rather than only the HTML shell.
        matches = re.findall(r'<script src="(/web/static/console/[^"]*\.js\?v=[^"]*)"></script>', html)
        assert matches, "review console module <script src> tags not found in page"
        js = "".join(authed_html_client.get(src).text for src in matches)
        assert "REFRESH_INTERVAL_SEC" not in js
        assert "function startAutoRefresh()" not in js
        assert "function refreshNow(reason)" not in js

    def test_unchanged_load_same_html(self, authed_html_client: TestClient) -> None:
        h1 = authed_html_client.get("/admin/conversations", follow_redirects=False).text
        h2 = authed_html_client.get("/admin/conversations", follow_redirects=False).text
        assert h1 == h2

    def test_top_bar_before_main_layout(self, authed_html_client: TestClient) -> None:
        resp = authed_html_client.get("/admin/conversations", follow_redirects=False)
        html = resp.text
        tb = html.find("top-bar")
        ml = max(
            html.find("conv-list") if "conv-list" in html else -1,
            html.find("timeline") if "timeline" in html else -1,
        )
        assert tb >= 0 and ml >= 0 and tb < ml


# =====================================================================
# 7. Media entity‑context — collision with real media
# =====================================================================


class TestMediaEntityContext:
    DIRECT_CID = "direct__contact_bob___staff_alice"

    def _seed_collision(self, db: Session, tmp_path: Any) -> None:
        _seed_tenant_a(db)
        _seed_contact(db, "staff_alice", "Alice")
        _seed_contact(db, "contact_bob", "Bob")

        # Create real local media files so media_status="downloaded" and
        # media_access_url is non-null in timeline responses.
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        for fname in ("sdk-dm-1.jpg", "sdk-gm-1.jpg"):
            (media_dir / fname).write_bytes(b"fake-jpeg")
        os.environ["STORAGE_LOCAL_PATH"] = str(media_dir)

        # Direct message with media sdkfileid
        dm_id = _seed_message(
            db,
            msgid="dm-1",
            sender="staff_alice",
            msgtype="image",
            sdkfileid="sdk-dm-1",
            content_text="",
            msgtime=1000,
        )
        _seed_recipient(db, dm_id, "contact_bob")
        # Group message — roomid matches direct CID → collision
        gm_id = _seed_message(
            db,
            msgid="gm-1",
            sender="staff_alice",
            roomid=self.DIRECT_CID,
            msgtype="image",
            sdkfileid="sdk-gm-1",
            content_text="",
            msgtime=2000,
        )
        _seed_recipient(db, gm_id, "contact_bob")

        from app.db.models import MediaFile

        for mid, sdk, fname in [
            (dm_id, "sdk-dm-1", "sdk-dm-1.jpg"),
            (gm_id, "sdk-gm-1", "sdk-gm-1.jpg"),
        ]:
            mf = MediaFile(
                tenant_id="tenant-a",
                sdkfileid=sdk,
                archive_message_id=mid,
                download_status="downloaded",
                file_type="image",
                local_path=str(media_dir / fname),
                storage_backend="local",
                storage_ref=str(media_dir / fname),
                mime_type="image/jpeg", file_size=1,
            )
            db.add(mf)
        db.commit()

    def test_collision_group_wins(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        self._seed_collision(db_session, tmp_path)
        resp = authed_client.get("/api/conversations?mode=staff&staff_id=staff_alice")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) > 0
        assert body[0]["conversation_id"] == self.DIRECT_CID
        assert body[0]["conversation_type"] == "group"

    def test_collision_timeline_returns_messages(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        self._seed_collision(db_session, tmp_path)
        resp = authed_client.get(
            f"/api/conversations/{self.DIRECT_CID}/messages"
            "?mode=staff&staff_id=staff_alice&conversation_type=group&limit=10"
        )
        assert resp.status_code == 200
        msgs = resp.json().get("messages", [])
        assert len(msgs) > 0
        senders = {m.get("sender") for m in msgs}
        assert "staff_alice" in senders
        # With seeded media rows, all timeline messages must carry the
        # media_access_url field (entity‑context propagation baseline).
        assert msgs[0].get("media_access_url"), "null media_access_url"
        assert "mode=" in msgs[0].get("media_access_url", ""), (
            f"missing in: {list(msgs[0].keys())}"
        )

    def test_top_level_media_conversation_type_accepted(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        self._seed_collision(db_session, tmp_path)
        resp = authed_client.get(
            f"/api/conversations/{self.DIRECT_CID}/messages/dm-1/media/access"
            "?mode=staff&staff_id=staff_alice"
        )
        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )

    def test_nested_media_extra_params_accepted(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        self._seed_collision(db_session, tmp_path)
        resp = authed_client.get(
            f"/api/conversations/{self.DIRECT_CID}/messages/dm-1/nested-media/0/access"
            "?mode=staff&staff_id=staff_alice"
        )
        assert resp.status_code != 422

    def test_media_access_response_is_json(
        self, authed_client: TestClient, db_session: Session, tmp_path: Any
    ) -> None:
        self._seed_collision(db_session, tmp_path)
        resp = authed_client.get(
            f"/api/conversations/{self.DIRECT_CID}/messages/dm-1/media/access"
            "?mode=staff&staff_id=staff_alice"
        )
        assert "application/json" in resp.headers.get("content-type", "")


# =====================================================================
# 8. Method contracts
# =====================================================================


class TestMethodContracts:
    def test_wecom_events_get(self, client: TestClient) -> None:
        assert client.get("/api/wecom/archive/events").status_code != 405

    def test_wecom_events_post(self, client: TestClient) -> None:
        assert client.post("/api/wecom/archive/events").status_code != 405

    def test_auth_logout_post_only(self, client: TestClient) -> None:
        assert client.get("/api/auth/logout").status_code == 405

    def test_password_login_post_only(self, client: TestClient) -> None:
        assert client.get("/api/auth/password/login").status_code == 405
