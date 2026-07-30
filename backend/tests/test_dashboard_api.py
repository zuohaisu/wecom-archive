"""RND-282 dashboard API coverage using a SQLite-compatible hand schema."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact, MediaFile, SyncState

_SCHEMA_SQL = """
CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT, slug TEXT, is_active INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE archive_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, msgid TEXT NOT NULL, seq INTEGER NOT NULL, publickey_ver INTEGER NOT NULL, raw_encrypted_payload TEXT, encrypt_random_key TEXT NOT NULL, encrypt_chat_msg TEXT NOT NULL, decrypt_status TEXT NOT NULL, decrypted_payload TEXT, structured_content TEXT, content_text TEXT, msgtype TEXT, sender TEXT, roomid TEXT, msgtime INTEGER, tolist TEXT, sdkfileid TEXT, is_revoked INTEGER NOT NULL DEFAULT 0, revoked_at TEXT, tenant_id TEXT, created_at TEXT);
CREATE TABLE archive_message_recipients (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, receiver_userid TEXT NOT NULL, receiver_type TEXT, tenant_id TEXT, created_at TEXT);
CREATE TABLE media_files (id INTEGER PRIMARY KEY AUTOINCREMENT, sdkfileid TEXT NOT NULL, archive_message_id INTEGER NOT NULL, tenant_id TEXT, file_type TEXT, local_path TEXT, oss_key TEXT, storage_backend TEXT, storage_ref TEXT, file_size INTEGER, download_status TEXT NOT NULL DEFAULT 'pending', download_attempts INTEGER NOT NULL DEFAULT 0, migration_status TEXT, migration_attempted_at TEXT, migration_error TEXT, bucket TEXT, mime_type TEXT, checksum_sha256 TEXT, thumbnail_ref TEXT, image_width INTEGER, image_height INTEGER, thumbnail_status TEXT, thumbnail_attempted_at TEXT, thumbnail_error TEXT, playback_ref TEXT, playback_status TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE sync_states (id INTEGER PRIMARY KEY AUTOINCREMENT, corp_id TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'idle', started_at TEXT, error_message TEXT, seq_version INTEGER NOT NULL DEFAULT 0, tenant_id TEXT, updated_at TEXT);
CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, wecom_userid TEXT NOT NULL, name TEXT, tenant_id TEXT, created_at TEXT, updated_at TEXT);
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
        ArchiveMessageRecipient(message_id=recent.id, receiver_userid="staff-received", tenant_id=TENANT_A),
        MediaFile(sdkfileid="a", archive_message_id=recent.id, tenant_id=TENANT_A, file_size=12, download_status="downloaded"),
        MediaFile(sdkfileid="b", archive_message_id=recent.id, tenant_id=TENANT_B, file_size=99, download_status="downloaded"),
        Contact(wecom_userid="staff-active", tenant_id=TENANT_A),
        Contact(wecom_userid="staff-silent", tenant_id=TENANT_A),
        Contact(wecom_userid="staff-other", tenant_id=TENANT_B),
        SyncState(corp_id="a", tenant_id=TENANT_A, status="idle", last_seq=1),
    ])
    db.commit()


def _authenticated_app(db: Session):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app
    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), TENANT_A)
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
        assert data["storage_bytes"] == wide_data["storage_bytes"] == 12
        assert data["staff_count"] == 2
        assert data["sync_status"] == "idle" and data["sync_healthy"] is True
        assert len(data["daily_series"]) == 14
        assert sum(p["text_count"] + p["media_count"] for p in data["daily_series"]) == data["total_messages"]
        assert any(p["text_count"] == p["media_count"] == 0 for p in data["daily_series"])
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
