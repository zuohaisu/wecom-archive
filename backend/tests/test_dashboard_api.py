"""RND-282 dashboard API coverage using a SQLite-compatible hand schema."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import (
    ArchiveMessage,
    ArchiveMessageRecipient,
    AdminUser,
    Contact,
    MediaFile,
    SyncState,
    Tenant,
    TenantWecomConfig,
)

_SCHEMA_SQL = """
CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT, slug TEXT, is_active INTEGER, lifecycle_status TEXT NOT NULL DEFAULT 'active', lifecycle_revision INTEGER NOT NULL DEFAULT 1, frozen_at DATETIME, suspended_at DATETIME, suspension_reason TEXT, suspended_by_platform_admin_id TEXT, suspension_previous_status TEXT, onboarding_completed_at TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE archive_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, msgid TEXT NOT NULL, seq INTEGER NOT NULL, publickey_ver INTEGER NOT NULL, raw_encrypted_payload TEXT, encrypt_random_key TEXT NOT NULL, encrypt_chat_msg TEXT NOT NULL, decrypt_status TEXT NOT NULL, decrypted_payload TEXT, structured_content TEXT, content_text TEXT, msgtype TEXT, sender TEXT, roomid TEXT, msgtime INTEGER, tolist TEXT, sdkfileid TEXT, is_revoked INTEGER NOT NULL DEFAULT 0, revoked_at TEXT, tenant_id TEXT, created_at TEXT);
CREATE TABLE archive_message_recipients (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, receiver_userid TEXT NOT NULL, receiver_type TEXT, tenant_id TEXT, created_at TEXT);
CREATE TABLE media_files (id INTEGER PRIMARY KEY AUTOINCREMENT, sdkfileid TEXT NOT NULL, archive_message_id INTEGER NOT NULL, tenant_id TEXT, file_type TEXT, local_path TEXT, oss_key TEXT, storage_backend TEXT, storage_ref TEXT, file_size INTEGER, download_status TEXT NOT NULL DEFAULT 'pending', download_attempts INTEGER NOT NULL DEFAULT 0, migration_status TEXT, migration_attempted_at TEXT, migration_error TEXT, bucket TEXT, mime_type TEXT, checksum_sha256 TEXT, thumbnail_ref TEXT, image_width INTEGER, image_height INTEGER, thumbnail_status TEXT, thumbnail_attempted_at TEXT, thumbnail_error TEXT, playback_ref TEXT, playback_status TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE sync_states (id INTEGER PRIMARY KEY AUTOINCREMENT, corp_id TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'idle', started_at TEXT, error_message TEXT, seq_version INTEGER NOT NULL DEFAULT 0, tenant_id TEXT, updated_at TEXT);
CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, wecom_userid TEXT NOT NULL, name TEXT, tenant_id TEXT, avatar_storage_backend TEXT, avatar_storage_ref TEXT, avatar_content_type TEXT, avatar_source TEXT, avatar_synced_at TEXT, avatar_status TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE tenant_wecom_configs (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, corp_id TEXT NOT NULL, agent_id TEXT NOT NULL, app_secret TEXT NOT NULL, private_key_encrypted TEXT, callback_token_encrypted TEXT, callback_encoding_aes_key_encrypted TEXT, publickey_version INTEGER, callback_domain TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT, updated_at TEXT);
CREATE TABLE admin_users (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, wecom_user_id TEXT NOT NULL, name TEXT, avatar_url TEXT, last_login_at TEXT, password_hash TEXT, role TEXT NOT NULL DEFAULT 'admin', status TEXT NOT NULL DEFAULT 'active', email TEXT, phone TEXT, department TEXT, last_active_at TEXT, invite_token TEXT, invited_by TEXT, invite_status TEXT, ui_theme TEXT NOT NULL DEFAULT 'light', ui_locale TEXT NOT NULL DEFAULT 'zh-CN', created_at TEXT, updated_at TEXT);
"""
TENANT_A, TENANT_B = "tenant-a", "tenant-b"


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        for statement in _SCHEMA_SQL.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
    return Session(engine)


@pytest.fixture()
def db():
    session = _session()
    yield session
    session.close()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _message(db: Session, tenant_id: str, days_ago: int, msgtype: str, sender: str) -> ArchiveMessage:
    msg = ArchiveMessage(msgid=f"{tenant_id}-{days_ago}-{msgtype}-{sender}", seq=1, publickey_ver=1,
        encrypt_random_key="x", encrypt_chat_msg="x", decrypt_status="success", tenant_id=tenant_id,
        msgtype=msgtype, sender=sender,
        msgtime=int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000))
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _seed(db: Session) -> None:
    recent = _message(db, TENANT_A, 1, "text", "staff-active")
    _message(db, TENANT_A, 20, "image", "staff-silent")
    _message(db, TENANT_A, 60, "text", "staff-old")
    _message(db, TENANT_B, 1, "text", "staff-other")
    db.add_all([
        Tenant(id=TENANT_A, name="Tenant A", slug="tenant-a"),
        TenantWecomConfig(
            id="config-a", tenant_id=TENANT_A, corp_id="corp-a", agent_id="agent-a", app_secret="ciphertext"
        ),
        ArchiveMessageRecipient(message_id=recent.id, receiver_userid="staff-received", tenant_id=TENANT_A),
        MediaFile(sdkfileid="a", archive_message_id=recent.id, tenant_id=TENANT_A, file_size=12, download_status="downloaded"),
        MediaFile(sdkfileid="b", archive_message_id=recent.id, tenant_id=TENANT_B, file_size=99, download_status="downloaded"),
        Contact(wecom_userid="staff-active", tenant_id=TENANT_A),
        Contact(wecom_userid="staff-silent", tenant_id=TENANT_A),
        Contact(wecom_userid="external-contact", tenant_id=TENANT_A),
        Contact(wecom_userid="staff-other", tenant_id=TENANT_B),
        AdminUser(id="admin-active", tenant_id=TENANT_A, wecom_user_id="staff-active"),
        AdminUser(id="admin-silent", tenant_id=TENANT_A, wecom_user_id="staff-silent"),
        SyncState(corp_id="a", tenant_id=TENANT_A, status="idle", last_seq=1),
    ])
    db.commit()


def _authenticated_app(db: Session):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app
    user = MagicMock()
    user.role = "admin"
    app.dependency_overrides[get_current_user] = lambda: (user, TENANT_A)
    def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    return app


def test_dashboard_range_tenant_aggregate_and_series(client, db) -> None:
    _seed(db)
    app = _authenticated_app(db)
    try:
        narrow = client.get("/api/admin/dashboard?range=14")
        wide = client.get("/api/admin/dashboard?range=90")
        assert narrow.status_code == wide.status_code == 200
        data, wide_data = narrow.json(), wide.json()
        assert data["range_days"] == 14 and wide_data["range_days"] == 90
        assert data["total_messages"] == 1 < wide_data["total_messages"] == 3
        assert data["total_archived_messages"] == wide_data["total_archived_messages"] == 3
        assert data["archive_coverage_days"] == 3
        assert data["storage_bytes"] == wide_data["storage_bytes"] == 12
        assert data["staff_count"] == 2
        assert data["sync_status"] == "idle" and data["sync_healthy"] is True
        assert data["archive_status"] == "normal"
        assert data["archive_configured"] is True
        assert data["can_manage_settings"] is True
        assert data["first_archived_at"] and data["last_archived_at"]
        assert len(data["daily_series"]) == 14
        assert sum(p["text_count"] + p["media_count"] for p in data["daily_series"]) == data["total_messages"]
        assert any(p["text_count"] == p["media_count"] == 0 for p in data["daily_series"])
        assert {item["category"] for item in data["type_composition"]} == {
            "text", "image", "voice", "video", "file", "structured", "other"
        }
        assert len(data["hourly_distribution"]) == 24
        assert data["recent_activity"] == []  # audit_logs deliberately absent from this schema
        assert "content_text" not in narrow.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_normalizes_invalid_range(client, db) -> None:
    _seed(db)
    app = _authenticated_app(db)
    try:
        response = client.get("/api/admin/dashboard?range=7")
        assert response.status_code == 200
        assert response.json()["range_days"] == 30
    finally:
        app.dependency_overrides.clear()


def test_dashboard_requires_auth(client) -> None:
    from app.db.session import get_db
    from app.main import app
    def no_session():
        yield MagicMock()
    app.dependency_overrides[get_db] = no_session
    try:
        assert client.get("/api/admin/dashboard").status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_dashboard_marks_configured_tenant_without_data_as_not_started(client, db) -> None:
    db.add_all([
        Tenant(id=TENANT_A, name="Tenant A", slug="tenant-a"),
        TenantWecomConfig(
            id="config-a", tenant_id=TENANT_A, corp_id="corp-a", agent_id="agent-a", app_secret="ciphertext"
        ),
    ])
    db.commit()
    app = _authenticated_app(db)
    try:
        response = client.get("/api/admin/dashboard?range=14")
        assert response.status_code == 200
        data = response.json()
        assert data["total_archived_messages"] == 0
        assert data["archive_coverage_days"] == 0
        assert data["archive_status"] == "not_started"
        assert data["archive_configured"] is True
        assert all(point["text_count"] == point["media_count"] == 0 for point in data["daily_series"])
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("configured", "active", "sync_status", "expected"),
    (
        (False, None, None, "not_configured"),
        (True, False, None, "configuration_error"),
        (True, True, "syncing", "processing"),
        (True, True, "error", "needs_attention"),
    ),
)
def test_dashboard_uses_persisted_configuration_and_sync_state_for_status(
    client, db, configured, active, sync_status, expected
) -> None:
    db.add(Tenant(id=TENANT_A, name="Tenant A", slug="tenant-a"))
    if configured:
        db.add(TenantWecomConfig(
            id="config-a", tenant_id=TENANT_A, corp_id="corp-a", agent_id="agent-a",
            app_secret="ciphertext", is_active=active,
        ))
    if sync_status:
        db.add(SyncState(corp_id="corp-a", tenant_id=TENANT_A, status=sync_status, last_seq=1))
    db.commit()
    app = _authenticated_app(db)
    try:
        response = client.get("/api/admin/dashboard?range=14")
        assert response.status_code == 200
        assert response.json()["archive_status"] == expected
    finally:
        app.dependency_overrides.clear()


def test_dashboard_keeps_summary_when_an_optional_insight_fails(client, db, monkeypatch) -> None:
    """One chart query must not turn the tenant overview into a 500 response."""
    import app.services.dashboard_service as dashboard_service
    from sqlalchemy.exc import SQLAlchemyError

    _seed(db)

    def unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("test-only failed chart query")

    monkeypatch.setattr(dashboard_service, "type_composition", unavailable)
    app = _authenticated_app(db)
    try:
        response = client.get("/api/admin/dashboard?range=14")
        assert response.status_code == 200
        data = response.json()
        assert data["insight_errors"] == {"type_composition": "unavailable"}
        assert data["type_composition"] == []
        assert data["total_archived_messages"] == 3
    finally:
        app.dependency_overrides.clear()
