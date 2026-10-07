"""Acceptance coverage for RND-307 platform cross-tenant usage summaries."""

from __future__ import annotations

import base64
from collections.abc import Generator
from unittest.mock import ANY

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth import hash_password
from app.db.models import ArchiveMessage, Contact, MediaFile, PlatformAdmin, SyncState, Tenant
from app.db.session import get_db

_SCHEMA_SQL = """
CREATE TABLE tenants (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL,
    deletion_locked INTEGER NOT NULL DEFAULT 0, lifecycle_status TEXT NOT NULL DEFAULT 'active',
    lifecycle_revision INTEGER NOT NULL DEFAULT 1, frozen_at DATETIME,
    suspended_at DATETIME, suspension_reason TEXT,
    suspended_by_platform_admin_id TEXT, suspension_previous_status TEXT,
    created_at DATETIME, updated_at DATETIME, onboarding_completed_at DATETIME
);
CREATE TABLE platform_admins (
    id TEXT PRIMARY KEY, name TEXT, email TEXT NOT NULL, password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'superadmin', status TEXT NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, last_active_at DATETIME,
    last_login_at DATETIME
);
CREATE TABLE archive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msgid TEXT NOT NULL, seq INTEGER NOT NULL,
    publickey_ver INTEGER NOT NULL, raw_encrypted_payload TEXT,
    encrypt_random_key TEXT NOT NULL, encrypt_chat_msg TEXT NOT NULL,
    decrypt_status TEXT NOT NULL DEFAULT 'pending', decrypted_payload TEXT,
    structured_content TEXT, content_text TEXT, msgtype TEXT, sender TEXT,
    roomid TEXT, msgtime INTEGER, tolist TEXT, sdkfileid TEXT,
    is_revoked BOOLEAN NOT NULL DEFAULT 0, revoked_at DATETIME, deleted_at DATETIME, deleted_by_admin_user_id TEXT, delete_reason TEXT, purge_after DATETIME, restored_at DATETIME, restored_by_admin_user_id TEXT, deletion_batch_id TEXT, tenant_id TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sdkfileid TEXT NOT NULL,
    archive_message_id INTEGER NOT NULL, tenant_id TEXT, file_type TEXT,
    local_path TEXT, oss_key TEXT, storage_backend TEXT, storage_ref TEXT,
    file_size INTEGER, download_status TEXT NOT NULL DEFAULT 'pending',
    download_attempts INTEGER NOT NULL DEFAULT 0, migration_status TEXT,
    migration_attempted_at DATETIME, migration_error TEXT, bucket TEXT,
    mime_type TEXT, checksum_sha256 TEXT, thumbnail_ref TEXT, image_width INTEGER,
    image_height INTEGER, thumbnail_status TEXT, thumbnail_attempted_at DATETIME,
    thumbnail_error TEXT, playback_ref TEXT, playback_status TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME
);
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, wecom_userid TEXT NOT NULL, name TEXT,
    tenant_id TEXT, avatar_storage_backend TEXT, avatar_storage_ref TEXT,
    avatar_content_type TEXT, avatar_source TEXT, avatar_synced_at DATETIME,
    avatar_status TEXT, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
);
CREATE TABLE sync_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT, corp_id TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'idle',
    started_at DATETIME, error_message TEXT, seq_version INTEGER NOT NULL DEFAULT 0,
    tenant_id TEXT, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _basic(password: str = "test-password") -> dict[str, str]:
    token = base64.b64encode(f"platform@example.test:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def usage_client() -> Generator[tuple[TestClient, Session], None, None]:
    """Serve a fresh app against a SQLite schema compatible with UsageService."""
    from app.main import create_app

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        for statement in _SCHEMA_SQL.strip().split(";"):
            connection.execute(text(statement))

    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    db.add(
        PlatformAdmin(
            id="rnd307-platform-admin",
            email="platform@example.test",
            password_hash=hash_password("test-password"),
            role="superadmin",
            status="active",
        )
    )
    db.commit()

    app = create_app()

    def override_db() -> Generator[Session, None, None]:
        request_db = session_factory()
        try:
            yield request_db
        finally:
            request_db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, db
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def _message(tenant_id: str, msgid: str, content_text: str) -> ArchiveMessage:
    return ArchiveMessage(
        msgid=msgid,
        seq=1,
        publickey_ver=1,
        encrypt_random_key="test-key",
        encrypt_chat_msg="test-message",
        tenant_id=tenant_id,
        content_text=content_text,
    )


def _seed_distinct_tenant_usage(db: Session) -> None:
    tenant_a = Tenant(id="tenant-a", name="Tenant A", slug="tenant-a")
    # Inactive tenants retain historical usage and must be present as well.
    tenant_b = Tenant(
        id="tenant-b",
        name="Tenant B",
        slug="tenant-b",
        lifecycle_status="suspended",
    )
    messages = [
        _message("tenant-a", "a-message-1", "tenant-a-private-message-body"),
        _message("tenant-b", "b-message-1", "tenant-b-private-message-body"),
        _message("tenant-b", "b-message-2", "tenant-b-private-message-body"),
    ]
    db.add_all([tenant_a, tenant_b, *messages])
    db.flush()
    db.add_all(
        [
            MediaFile(
                sdkfileid="a-media",
                archive_message_id=messages[0].id,
                tenant_id="tenant-a",
                file_size=11,
                storage_ref="https://media.example.test/tenant-a-private-media",
            ),
            MediaFile(
                sdkfileid="b-media-1",
                archive_message_id=messages[1].id,
                tenant_id="tenant-b",
                file_size=20,
            ),
            MediaFile(
                sdkfileid="b-media-2",
                archive_message_id=messages[2].id,
                tenant_id="tenant-b",
                file_size=30,
            ),
            Contact(wecom_userid="a-employee-1", tenant_id="tenant-a"),
            Contact(wecom_userid="a-employee-2", tenant_id="tenant-a"),
            Contact(wecom_userid="b-employee-1", tenant_id="tenant-b"),
            SyncState(corp_id="a-corp", tenant_id="tenant-a", status="idle", last_seq=3),
            SyncState(
                corp_id="b-corp",
                tenant_id="tenant-b",
                status="error",
                last_seq=7,
                error_message="tenant-b sync failed",
            ),
        ]
    )
    db.commit()


def test_usage_is_aggregated_separately_for_every_tenant(
    usage_client: tuple[TestClient, Session],
) -> None:
    """AC-1/5: no global total duplication, inactive tenant included, no content leak."""
    client, db = usage_client
    _seed_distinct_tenant_usage(db)

    response = client.get("/api/platform/tenants/usage", headers=_basic())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"tenants"}
    assert len(body["tenants"]) == 2
    summaries = {item["tenant_id"]: item for item in body["tenants"]}
    assert set(summaries) == {"tenant-a", "tenant-b"}
    assert summaries["tenant-a"] == {
        "tenant_id": "tenant-a",
        "tenant_name": "Tenant A",
        "message_count": 1,
        "storage_bytes": 11,
        "employee_count": 2,
        "sync_health": {
            "status": "idle",
            "error_message": None,
            "last_seq": 3,
            "updated_at": ANY,
        },
    }
    assert summaries["tenant-b"] == {
        "tenant_id": "tenant-b",
        "tenant_name": "Tenant B",
        "message_count": 2,
        "storage_bytes": 50,
        "employee_count": 1,
        "sync_health": {
            "status": "error",
            "error_message": "tenant-b sync failed",
            "last_seq": 7,
            "updated_at": ANY,
        },
    }
    assert "tenant-a-private-message-body" not in response.text
    assert "tenant-b-private-message-body" not in response.text
    assert "tenant-a-private-media" not in response.text


def test_usage_returns_empty_list_when_no_tenants(
    usage_client: tuple[TestClient, Session],
) -> None:
    """AC-3: platform admins receive a valid empty response."""
    client, _db = usage_client

    response = client.get("/api/platform/tenants/usage", headers=_basic())

    assert response.status_code == 200
    assert response.json() == {"tenants": []}


@pytest.mark.parametrize("headers", [{}, _basic(password="incorrect")])
def test_usage_requires_valid_platform_admin_credentials(
    usage_client: tuple[TestClient, Session], headers: dict[str, str]
) -> None:
    """AC-4: missing and invalid Basic credentials are denied."""
    client, _db = usage_client

    assert client.get("/api/platform/tenants/usage", headers=headers).status_code == 401
