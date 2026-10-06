"""RND-364: permanent purge, retry, metrics, and auto-cleanup tests."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AdminUser,
    ArchiveMessage,
    ArchiveMessageRecipient,
    AuditLog,
    MediaFile,
    MediaPurgeRetry,
    MessageRevocation,
    ReachabilityFinding,
    RetentionLock,
    Tenant,
)
from app.services.message_deletion import (
    DeletionLockedError,
    purge_expired_messages,
    purge_messages,
    recycle_bin_metrics,
    retry_media_purges,
    soft_delete_messages,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


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
                MediaPurgeRetry.__table__,
                MessageRevocation.__table__,
                RetentionLock.__table__,
                ReachabilityFinding.__table__,
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


def _add_message(db, *, tenant_id="tenant-a", msgid="m1", message_id=1, has_media=False, msgtime=1000):
    message = ArchiveMessage(
        id=message_id,
        msgid=msgid,
        seq=1,
        publickey_ver=1,
        encrypt_random_key="k",
        encrypt_chat_msg="c",
        decrypt_status="success",
        content_text=f"body-{msgid}",
        msgtype="text",
        tenant_id=tenant_id,
        msgtime=msgtime,
    )
    db.add(message)
    db.flush()
    if has_media:
        db.add(
            MediaFile(
                id=message_id,
                sdkfileid=f"sdk-{msgid}",
                archive_message_id=message.id,
                tenant_id=tenant_id,
                file_type="image",
                download_status="downloaded",
                file_size=100,
                storage_backend="local",
                storage_ref=f"tenants/{tenant_id}/media/{msgid}.jpg",
            )
        )
    return message


def _delete(db, tenant_id="tenant-a", msgids=("m1",)):
    soft_delete_messages(db, tenant_id=tenant_id, actor_id="admin-a", msgids=msgids, at=NOW - timedelta(days=31))


def _succeeding_deleter(_backend, _ref):
    return True


def _failing_deleter(_backend, _ref):
    return False


def test_purge_requires_confirmation_and_tenant_scope(db) -> None:
    with db() as session:
        _add_message(session, msgid="m1", message_id=1)
        _add_message(session, tenant_id="tenant-b", msgid="b1", message_id=2)
        _delete(session, msgids=["m1"])
        _delete(session, tenant_id="tenant-b", msgids=["b1"])
        from app.services.message_deletion import MessageDeletionError

        with pytest.raises(MessageDeletionError):
            purge_messages(session, tenant_id="tenant-a", msgids=["m1"], confirm=False)
        result = purge_messages(
            session, tenant_id="tenant-a", msgids=["m1", "missing"], actor_id="admin-a",
            at=NOW, delete_object=_succeeding_deleter,
        )
        session.commit()
        assert result.purged == 1
        assert result.not_found == 1
        assert session.get(ArchiveMessage, 1) is None
        # tenant-b's tombstone is untouched (tenant isolation).
        assert session.get(ArchiveMessage, 2) is not None
        assert session.query(AuditLog).filter_by(action="messages.purged").count() == 1


def test_purge_is_idempotent_and_removes_derived_rows(db) -> None:
    with db() as session:
        message = _add_message(session, msgid="m1", message_id=1)
        session.add(ArchiveMessageRecipient(message_id=message.id, receiver_userid="r1", tenant_id="tenant-a"))
        session.add(MessageRevocation(
            tenant_id="tenant-a", revoke_event_message_id=message.id,
            revoke_event_msgid="rev-1", revoke_event_msgtime=1, status="linked",
            original_message_id=message.id,
        ))
        session.add(RetentionLock(id="lock-1", tenant_id="tenant-a", archive_message_id=message.id))
        session.add(ReachabilityFinding(
            id=1, public_id="f-1", tenant_id="tenant-a", archive_message_id=message.id,
            reason_code="x", status="active", first_seen=NOW, last_seen=NOW,
            first_run_id=1, last_run_id=1, algorithm_version="v1",
        ))
        _delete(session, msgids=["m1"])
        session.commit()
        first = purge_messages(session, tenant_id="tenant-a", msgids=["m1"], at=NOW, delete_object=_succeeding_deleter)
        session.commit()
        second = purge_messages(session, tenant_id="tenant-a", msgids=["m1"], at=NOW, delete_object=_succeeding_deleter)
        session.commit()
        assert first.purged == 1
        assert second.purged == 0 and second.not_found == 1
        assert session.query(ArchiveMessageRecipient).count() == 0
        assert session.query(MessageRevocation).count() == 0
        assert session.query(RetentionLock).count() == 0
        assert session.query(ReachabilityFinding).count() == 0


def test_storage_failure_keeps_retry_state_and_message(db) -> None:
    with db() as session:
        _add_message(session, msgid="m1", message_id=1, has_media=True)
        _delete(session, msgids=["m1"])
        session.commit()
        result = purge_messages(session, tenant_id="tenant-a", msgids=["m1"], at=NOW, delete_object=_failing_deleter)
        session.commit()
        assert result.purged == 0
        assert result.media_retry_pending == 1
        assert session.get(ArchiveMessage, 1) is not None
        assert session.get(MediaFile, 1) is not None
        retry = session.query(MediaPurgeRetry).one()
        assert retry.attempts == 1
        assert retry.storage_ref.endswith("m1.jpg")
        # A later successful retry (after the backoff window) resolves the
        # media and enables the purge.
        resolved = retry_media_purges(
            session, "tenant-a", at=NOW + timedelta(hours=2), delete_object=_succeeding_deleter
        )
        session.commit()
        assert resolved == 1
        assert session.query(MediaPurgeRetry).count() == 0
        assert session.get(MediaFile, 1) is None
        done = purge_messages(session, tenant_id="tenant-a", msgids=["m1"], at=NOW, delete_object=_succeeding_deleter)
        session.commit()
        assert done.purged == 1


def test_shared_media_object_is_not_deleted_for_one_message(db) -> None:
    ref = "tenants/tenant-a/media/shared.bin"
    with db() as session:
        _add_message(session, msgid="m1", message_id=1)
        _add_message(session, msgid="m2", message_id=2)
        for message_id, sdk in ((1, "sdk-1"), (2, "sdk-2")):
            session.add(MediaFile(
                id=message_id, sdkfileid=sdk, archive_message_id=message_id,
                tenant_id="tenant-a", file_type="file", download_status="downloaded",
                file_size=10, storage_backend="local", storage_ref=ref,
            ))
        _delete(session, msgids=["m1"])
        session.commit()
        deleted = []
        def deleter(backend, storage_ref):
            deleted.append(storage_ref)
            return True
        purge_messages(session, tenant_id="tenant-a", msgids=["m1"], at=NOW, delete_object=deleter)
        session.commit()
        # The shared object must not be deleted while m2 still references it;
        # m1's own media row is removed but the object stays for m2.
        assert deleted == []
        assert session.query(MediaFile).count() == 1


def test_expired_only_and_metrics(db) -> None:
    with db() as session:
        _add_message(session, msgid="m1", message_id=1)
        _add_message(session, msgid="m2", message_id=2)
        soft_delete_messages(session, tenant_id="tenant-a", actor_id="admin-a", msgids=["m1"], at=NOW - timedelta(days=40))
        soft_delete_messages(session, tenant_id="tenant-a", actor_id="admin-a", msgids=["m2"], at=NOW - timedelta(days=10))
        session.commit()
        metrics = recycle_bin_metrics(session, "tenant-a", at=NOW)
        assert metrics.pending_purge_count == 1
        assert metrics.oldest_pending_age_days is not None
        result = purge_expired_messages(session, "tenant-a", at=NOW, delete_object=_succeeding_deleter)
        session.commit()
        assert result.purged == 1
        assert session.query(ArchiveMessage).filter_by(msgid="m1").count() == 0
        assert session.query(ArchiveMessage).filter_by(msgid="m2").count() == 1
        # Idempotent rerun.
        again = purge_expired_messages(session, "tenant-a", at=NOW, delete_object=_succeeding_deleter)
        session.commit()
        assert again.purged == 0
        assert recycle_bin_metrics(session, "tenant-a", at=NOW).pending_purge_count == 0


def test_deletion_lock_blocks_purge(db) -> None:
    with db() as session:
        _add_message(session, msgid="m1", message_id=1)
        _delete(session, msgids=["m1"])
        session.get(Tenant, "tenant-a").deletion_locked = True
        session.commit()
        with pytest.raises(DeletionLockedError):
            purge_messages(session, tenant_id="tenant-a", msgids=["m1"], at=NOW, delete_object=_succeeding_deleter)


def test_run_message_purge_once_script_roundtrip(db) -> None:
    import run_message_purge_once

    with db() as session:
        _add_message(session, msgid="m1", message_id=1)
        _add_message(session, msgid="m2", message_id=2)
        soft_delete_messages(session, tenant_id="tenant-a", actor_id="admin-a", msgids=["m1"], at=NOW - timedelta(days=40))
        soft_delete_messages(session, tenant_id="tenant-a", actor_id="admin-a", msgids=["m2"], at=NOW - timedelta(days=5))
        session.commit()
        summary = run_message_purge_once.run_message_purge_once(session, now=NOW)
        assert summary["purged"] == 1
        assert session.query(ArchiveMessage).filter_by(msgid="m1").count() == 0
        assert session.query(ArchiveMessage).filter_by(msgid="m2").count() == 1


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


def test_purge_api_requires_confirmation_and_is_role_gated(client, db) -> None:
    from app.main import app

    with db() as session:
        _add_message(session, msgid="m1", message_id=1)
        _delete(session, msgids=["m1"])
        session.commit()

    _authed(app, db)
    try:
        denied = client.post("/api/admin/messages/purge", json={"message_ids": ["m1"], "confirm": False})
        assert denied.status_code == 422
        ok = client.post("/api/admin/messages/purge", json={"message_ids": ["m1"], "confirm": True})
        assert ok.status_code == 200
        body = ok.json()
        assert body["purged"] == 1
        assert body["purged_message_ids"] == ["m1"]
    finally:
        app.dependency_overrides.clear()

    _authed(app, db, role="compliance")
    try:
        blocked = client.post("/api/admin/messages/purge", json={"message_ids": ["m1"], "confirm": True})
        assert blocked.status_code == 403
    finally:
        app.dependency_overrides.clear()


def _freeze_deletion_clock(monkeypatch, at: datetime) -> None:
    from app.services import message_deletion

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz is not None else at.replace(tzinfo=None)

    monkeypatch.setattr(message_deletion, "datetime", FixedDatetime)


def test_recycle_bin_filters_and_metrics(client, db) -> None:
    from app.main import app

    deleted_at = datetime.now(timezone.utc)
    with db() as session:
        _add_message(session, msgid="m1", message_id=1)
        _add_message(session, msgid="m2", message_id=2)
        soft_delete_messages(session, tenant_id="tenant-a", actor_id="admin-a", msgids=["m1", "m2"], at=deleted_at)
        session.commit()

    _authed(app, db)
    try:
        listed = client.get("/api/admin/messages/recycle-bin")
        assert listed.status_code == 200
        assert listed.json()["total"] == 2
        filtered = client.get("/api/admin/messages/recycle-bin?msgtype=voice")
        assert filtered.json()["total"] == 0
        metrics = client.get("/api/admin/messages/recycle-bin/metrics")
        assert metrics.status_code == 200
        assert metrics.json()["pending_purge_count"] == 0
        assert metrics.json()["storage_retry_pending"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "offset,expected_count,expected_age",
    [
        (timedelta(microseconds=-1), 0, None),
        (timedelta(0), 1, 0.0),
        (timedelta(days=1), 1, 1.0),
    ],
    ids=["before-expiry", "at-expiry", "after-expiry"],
)
def test_recycle_bin_metrics_http_expiry_boundary(
    client, db, monkeypatch, offset, expected_count, expected_age
) -> None:
    from app.main import app

    with db() as session:
        message = _add_message(session)
        # Another tenant's expired message must not affect these metrics.
        _add_message(session, tenant_id="tenant-b", msgid="other", message_id=2)
        soft_delete_messages(session, tenant_id="tenant-a", actor_id="admin-a", msgids=["m1"], at=NOW)
        soft_delete_messages(session, tenant_id="tenant-b", actor_id=None, msgids=["other"], at=NOW - timedelta(days=365))
        session.commit()
        expiry = message.purge_after.replace(tzinfo=timezone.utc)

    _freeze_deletion_clock(monkeypatch, expiry + offset)
    _authed(app, db)
    try:
        response = client.get("/api/admin/messages/recycle-bin/metrics")
        assert response.status_code == 200
        assert response.json()["pending_purge_count"] == expected_count
        assert response.json()["oldest_pending_age_days"] == expected_age
    finally:
        app.dependency_overrides.clear()


def test_recycle_bin_page_renders(client, db) -> None:
    from app.auth import require_html_session
    from app.main import app

    _authed(app, db)
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    try:
        page = client.get("/admin/recycle-bin")
        assert page.status_code == 200
        assert 'id="purge-modal"' in page.text
        assert 'src="/web/static/recycle-bin.js' in page.text
        assert 'id="recycle-select-all"' in page.text
    finally:
        app.dependency_overrides.clear()
