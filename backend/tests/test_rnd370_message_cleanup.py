"""RND-370: bulk cleanup filter, preview, task lifecycle tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.db.base import Base
from app.db.models import (
    AdminUser,
    ArchiveMessage,
    ArchiveMessageRecipient,
    AuditLog,
    MediaFile,
    MessageCleanupPreview,
    MessageCleanupTask,
    Tenant,
)
from app.services.message_cleanup import (
    CleanupConfirmationRequired,
    CleanupPreviewStale,
    CONFIRMATION_PHRASE,
    create_cleanup_task,
    preview_cleanup,
    process_cleanup_task_once,
    store_cleanup_preview,
    validate_filter,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
GIB = 1024**3


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    fts_index = next(
        index
        for index in ArchiveMessage.__table__.indexes
        if index.name == "ix_archive_messages_content_text_fts"
    )
    ArchiveMessage.__table__.indexes.remove(fts_index)
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                Tenant.__table__,
                AdminUser.__table__,
                ArchiveMessage.__table__,
                ArchiveMessageRecipient.__table__,
                MediaFile.__table__,
                MessageCleanupPreview.__table__,
                MessageCleanupTask.__table__,
                AuditLog.__table__,
            ],
        )
    finally:
        ArchiveMessage.__table__.indexes.add(fts_index)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Tenant(id="tenant-a", name="A", slug="a"))
        session.add(Tenant(id="tenant-b", name="B", slug="b"))
        session.add(AdminUser(id="admin-a", tenant_id="tenant-a", wecom_user_id="owner", role="owner"))
        session.commit()
    return factory


def _msg(db, *, mid, msgid, msgtype="text", roomid=None, sender="staff_1", msgtime=1000, tenant="tenant-a"):
    message = ArchiveMessage(
        id=mid, msgid=msgid, seq=mid, publickey_ver=1,
        encrypt_random_key="k", encrypt_chat_msg="c", decrypt_status="success",
        content_text=f"body-{msgid}", msgtype=msgtype, sender=sender,
        roomid=roomid, msgtime=msgtime, tenant_id=tenant,
    )
    db.add(message)
    db.flush()
    return message


def _media(db, *, media_id, message_id, size=100, ref=None, tenant="tenant-a"):
    db.add(MediaFile(
        id=media_id, sdkfileid=f"sdk-{media_id}", archive_message_id=message_id,
        tenant_id=tenant, file_type="image", download_status="downloaded",
        file_size=size, storage_backend="local", storage_ref=ref or f"tenants/{tenant}/m{media_id}.jpg",
    ))


def test_filter_validation_whitelist() -> None:
    f = validate_filter({"older_than_days": 90, "msgtypes": ["text", "image"], "has_media": True})
    assert f.older_than_days == 90
    assert f.msgtypes == ("text", "image")
    with pytest.raises(Exception):
        validate_filter({"older_than_days": 13})
    with pytest.raises(Exception):
        validate_filter({"msgtypes": ["bogus_type"]})
    with pytest.raises(Exception):
        validate_filter({"roomid": {"evil": 1}})


def test_preview_counts_and_capacity_estimates(db) -> None:
    with db() as session:
        _msg(session, mid=1, msgid="m1", msgtype="text", msgtime=1000)
        _msg(session, mid=2, msgid="m2", msgtype="image", roomid="room-1", msgtime=2_000_000)
        _media(session, media_id=1, message_id=2, size=2 * GIB)
        session.commit()
        f = validate_filter({})
        preview = preview_cleanup(session, "tenant-a", f, now=NOW)
        assert preview.matched == 2
        assert preview.conversation_count == 2
        assert preview.msgtype_counts == {"text": 1, "image": 1}
        assert preview.text_bytes_estimate == 512
        assert preview.media_bytes == 2 * GIB
        assert preview.releasable_bytes == 2 * GIB
        assert preview.shared_media_bytes == 0
        assert preview.earliest_msgtime == 1000
        assert preview.latest_msgtime == 2_000_000


def test_older_than_and_media_size_filters(db) -> None:
    now_ms = int(NOW.timestamp() * 1000)
    with db() as session:
        _msg(session, mid=1, msgid="m1", msgtime=now_ms - 40 * 86400000)
        _msg(session, mid=2, msgid="m2", msgtime=now_ms - 5 * 86400000, msgtype="image")
        _media(session, media_id=1, message_id=2, size=500)
        session.commit()
        f = validate_filter({"older_than_days": 30})
        assert preview_cleanup(session, "tenant-a", f, now=NOW).matched == 1
        f = validate_filter({"media_min_bytes": 400})
        assert preview_cleanup(session, "tenant-a", f, now=NOW).matched == 1
        f = validate_filter({"media_max_bytes": 400})
        assert preview_cleanup(session, "tenant-a", f, now=NOW).matched == 0


def test_task_lifecycle_create_process_complete(db) -> None:
    with db() as session:
        for mid in range(1, 6):
            _msg(session, mid=mid, msgid=f"m{mid}", msgtime=mid * 1000)
        session.commit()
        f = validate_filter({})
        preview = preview_cleanup(session, "tenant-a", f, now=NOW)
        assert preview.matched == 5
        store_cleanup_preview(session, tenant_id="tenant-a", preview=preview, f=f, now=NOW)
        task = create_cleanup_task(
            session, tenant_id="tenant-a", actor_id="admin-a",
            raw_filter={}, preview_version=preview.preview_version,
            confirmation=CONFIRMATION_PHRASE, now=NOW,
        )
        session.commit()
        assert task.status == "queued"
        task = session.get(MessageCleanupTask, task.id)
        # Process two pages (page size 2) then complete.
        process_cleanup_task_once(session, task, page_size=2, now=NOW)
        session.commit()
        assert task.status == "running" and task.succeeded == 2
        process_cleanup_task_once(session, task, page_size=2, now=NOW)
        session.commit()
        process_cleanup_task_once(session, task, page_size=2, now=NOW)
        session.commit()
        assert task.status == "completed" and task.succeeded == 5
        deleted = session.query(ArchiveMessage).filter(ArchiveMessage.deleted_at.is_not(None)).count()
        assert deleted == 5
        batch_ids = {
            row.deletion_batch_id
            for row in session.query(ArchiveMessage).filter(ArchiveMessage.deleted_at.is_not(None))
        }
        assert batch_ids == {task.id}
        # Rerunning the worker is a no-op (idempotent).
        process_cleanup_task_once(session, task, now=NOW)
        session.commit()
        assert task.status == "completed"


def test_task_requires_confirmation_and_rejects_stale_preview(db) -> None:
    with db() as session:
        _msg(session, mid=1, msgid="m1")
        session.commit()
        f = validate_filter({})
        preview = preview_cleanup(session, "tenant-a", f, now=NOW)
        store_cleanup_preview(session, tenant_id="tenant-a", preview=preview, f=f, now=NOW)
        with pytest.raises(CleanupConfirmationRequired):
            create_cleanup_task(
                session, tenant_id="tenant-a", actor_id="admin-a", raw_filter={},
                preview_version=preview.preview_version, confirmation="wrong", now=NOW,
            )
        # New messages change the match count beyond tolerance (1 -> 4).
        for mid in (2, 3, 4):
            _msg(session, mid=mid, msgid=f"m{mid}")
        session.commit()
        with pytest.raises(CleanupPreviewStale):
            create_cleanup_task(
                session, tenant_id="tenant-a", actor_id="admin-a", raw_filter={},
                preview_version=preview.preview_version, confirmation=CONFIRMATION_PHRASE, now=NOW,
            )


def test_task_is_tenant_scoped(db) -> None:
    with db() as session:
        _msg(session, mid=1, msgid="m1", tenant="tenant-a")
        _msg(session, mid=2, msgid="b1", tenant="tenant-b")
        session.commit()
        f = validate_filter({})
        preview_a = preview_cleanup(session, "tenant-a", f, now=NOW)
        store_cleanup_preview(session, tenant_id="tenant-a", preview=preview_a, f=f, now=NOW)
        task = create_cleanup_task(
            session, tenant_id="tenant-a", actor_id="admin-a", raw_filter={},
            preview_version=preview_a.preview_version, confirmation=CONFIRMATION_PHRASE, now=NOW,
        )
        session.commit()
        process_cleanup_task_once(session, session.get(MessageCleanupTask, task.id), now=NOW)
        session.commit()
        # Cleanup only soft-deletes: tenant-a's row stays but is tombstoned;
        # tenant-b's row is untouched (tenant isolation).
        assert (
            session.query(ArchiveMessage)
            .filter(ArchiveMessage.tenant_id == "tenant-a", ArchiveMessage.deleted_at.is_not(None))
            .count()
            == 1
        )
        assert (
            session.query(ArchiveMessage)
            .filter(ArchiveMessage.tenant_id == "tenant-b", ArchiveMessage.deleted_at.is_not(None))
            .count()
            == 0
        )


def test_worker_script_advances_queued_tasks(db) -> None:
    import process_message_cleanup_once

    with db() as session:
        for mid in range(1, 7):
            _msg(session, mid=mid, msgid=f"m{mid}", msgtime=mid * 1000)
        session.commit()
        f = validate_filter({})
        preview = preview_cleanup(session, "tenant-a", f, now=NOW)
        store_cleanup_preview(session, tenant_id="tenant-a", preview=preview, f=f, now=NOW)
        create_cleanup_task(
            session, tenant_id="tenant-a", actor_id="admin-a", raw_filter={},
            preview_version=preview.preview_version, confirmation=CONFIRMATION_PHRASE, now=NOW,
        )
        session.commit()
        summary = process_message_cleanup_once.process_message_cleanup_once(session, per_task_pages=2, now=NOW)
        assert summary["pages"] >= 1
        task = session.query(MessageCleanupTask).one()
        assert task.succeeded > 0


# ---------------------------------------------------------------------------
# HTTP API contract
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _authed(app, db_session, tenant_id="tenant-a", role="owner"):
    from app.auth import get_current_user
    from app.db.session import get_db

    def _db_gen():
        with db_session() as session:
            yield session

    user = MagicMock()
    user.id = "admin-a"
    user.role = role
    app.dependency_overrides[get_current_user] = lambda: (user, tenant_id)
    app.dependency_overrides[get_db] = _db_gen


def test_cleanup_api_preview_and_task_roundtrip(client, db) -> None:
    from app.main import app

    with db() as session:
        _msg(session, mid=1, msgid="m1")
        _msg(session, mid=2, msgid="m2")
        session.commit()

    _authed(app, db)
    try:
        preview = client.post(
            "/api/admin/messages/cleanup/preview",
            json={"filter": {}},
        )
        assert preview.status_code == 200
        body = preview.json()
        assert body["matched"] == 2
        assert body["preview_version"]

        denied = client.post(
            "/api/admin/messages/cleanup/tasks",
            json={"filter": {}, "preview_version": body["preview_version"], "confirmation": "wrong"},
        )
        assert denied.status_code == 422

        created = client.post(
            "/api/admin/messages/cleanup/tasks",
            json={"filter": {}, "preview_version": body["preview_version"], "confirmation": "确认清理"},
        )
        assert created.status_code == 201
        task = created.json()
        assert task["status"] == "queued"
        assert task["total_matched"] == 2

        listed = client.get("/api/admin/messages/cleanup/tasks")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        detail = client.get("/api/admin/messages/cleanup/tasks/" + task["id"])
        assert detail.status_code == 200
        assert detail.json()["id"] == task["id"]
    finally:
        app.dependency_overrides.clear()


def test_cleanup_api_is_role_gated(client, db) -> None:
    from app.main import app

    with db() as session:
        _msg(session, mid=1, msgid="m1")
        session.commit()

    _authed(app, db, role="compliance")
    try:
        preview = client.post("/api/admin/messages/cleanup/preview", json={"filter": {}})
        assert preview.status_code == 403
        listed = client.get("/api/admin/messages/cleanup/tasks")
        assert listed.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_cleanup_page_renders(client, db) -> None:
    from app.auth import require_html_session
    from app.main import app

    _authed(app, db)
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    try:
        page = client.get("/admin/cleanup")
        assert page.status_code == 200
        assert 'id="cleanup-preview"' in page.text
        assert 'id="confirm-modal"' in page.text
        assert 'src="/web/static/cleanup.js' in page.text
    finally:
        app.dependency_overrides.clear()
