"""RND-366 unified favorite API, tenancy, audit, and deletion lifecycle tests."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AdminUser,
    ArchiveFavorite,
    ArchiveMessage,
    ArchiveMessageRecipient,
    AuditLog,
    MediaFile,
    MediaPurgeRetry,
    MessageRevocation,
    ReachabilityAuditRun,
    ReachabilityFinding,
    RetentionLock,
    Tenant,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    from tests.test_reachability_audit import configure_sqlite_for_savepoints

    configure_sqlite_for_savepoints(engine)
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
                ReachabilityAuditRun.__table__,
                ReachabilityFinding.__table__,
                RetentionLock.__table__,
                ArchiveFavorite.__table__,
                AuditLog.__table__,
            ],
        )
    finally:
        ArchiveMessage.__table__.indexes.add(fts_index)

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                Tenant(id="tenant-a", name="A", slug="tenant-a"),
                Tenant(id="tenant-b", name="B", slug="tenant-b"),
            ]
        )
        db.flush()
        db.add_all(
            [
                AdminUser(
                    id="owner-a", tenant_id="tenant-a", wecom_user_id="staff_1", role="owner"
                ),
                AdminUser(
                    id="reviewer-a", tenant_id="tenant-a", wecom_user_id="staff_2", role="compliance"
                ),
                AdminUser(
                    id="readonly-a", tenant_id="tenant-a", wecom_user_id="staff_3", role="readonlyaudit"
                ),
                AdminUser(
                    id="owner-b", tenant_id="tenant-b", wecom_user_id="staff_b", role="owner"
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                ArchiveMessage(
                    id=1, msgid="msg-a1", seq=1, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="authoritative message body",
                    msgtype="text", sender="staff_1", roomid="room-a", msgtime=1000,
                    tenant_id="tenant-a",
                ),
                ArchiveMessage(
                    id=2, msgid="msg-a2", seq=2, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="another active message",
                    msgtype="image", sender="staff_1", roomid="room-a", msgtime=2000,
                    tenant_id="tenant-a",
                ),
                ArchiveMessage(
                    id=3, msgid="msg-b1", seq=1, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="tenant B private body",
                    msgtype="text", sender="staff_b", roomid="room-b", msgtime=1000,
                    tenant_id="tenant-b",
                ),
                ArchiveMessage(
                    id=4, msgid="msg-a4", seq=4, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="collision group message",
                    msgtype="text", sender="staff_2",
                    roomid="direct__contact_1___staff_1", msgtime=4000,
                    tenant_id="tenant-a",
                ),
                ArchiveMessage(
                    id=5, msgid="msg-a5", seq=5, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="collision direct message",
                    msgtype="text", sender="staff_1", roomid=None, msgtime=5000,
                    tenant_id="tenant-a",
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                ArchiveMessageRecipient(
                    message_id=4, tenant_id="tenant-a", receiver_userid="contact_2"
                ),
                ArchiveMessageRecipient(
                    message_id=5, tenant_id="tenant-a", receiver_userid="contact_1"
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                MediaFile(
                    id=1, sdkfileid="sdk-a1", archive_message_id=1,
                    tenant_id="tenant-a", file_type="image", mime_type="image/jpeg",
                    file_size=12, download_status="downloaded",
                    storage_backend="local", storage_ref="tenant-a/private-media.jpg",
                ),
                MediaFile(
                    id=2, sdkfileid="sdk-b1", archive_message_id=3,
                    tenant_id="tenant-b", file_type="image", mime_type="image/jpeg",
                    file_size=24, download_status="downloaded",
                    storage_backend="local", storage_ref="tenant-b/private-media.jpg",
                ),
            ]
        )
        db.commit()
    yield factory
    engine.dispose()


@pytest.fixture()
def api_client(db_factory):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    identity = {
        "tenant_id": "tenant-a",
        "user_tenant_id": "tenant-a",
        "role": "owner",
        "user_id": "owner-a",
    }

    def _auth():
        return (
            SimpleNamespace(
                tenant_id=identity["user_tenant_id"],
                role=identity["role"],
                id=identity["user_id"],
            ),
            identity["tenant_id"],
        )

    def _db():
        with db_factory() as db:
            yield db

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = _auth
    app.dependency_overrides[get_db] = _db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, identity
    app.dependency_overrides.clear()


def test_favorite_api_is_idempotent_tenant_scoped_and_audited(api_client, db_factory) -> None:
    client, identity = api_client
    first = client.post(
        "/api/favorites",
        json={"object_type": "message", "object_id": "msg-a1", "source_page": "messages"},
    )
    assert first.status_code == 200
    assert first.json()["items"][0]["result"] == "favorited"

    repeated = client.post(
        "/api/favorites", json={"object_type": "message", "object_id": "msg-a1"}
    )
    assert repeated.status_code == 200
    assert repeated.json()["items"][0]["result"] == "already_favorited"
    assert repeated.json()["applied"] == 0

    # The media-library id is also the canonical identity of this same row
    # when the message timeline presents its attachment.
    media = client.post(
        "/api/favorites",
        json={"object_type": "media", "object_id": "1", "source_page": "media"},
    )
    assert media.status_code == 200
    mixed = client.post(
        "/api/favorites/batch",
        json={
            "action": "favorite",
            "items": [
                {"object_type": "message", "object_id": "msg-a2"},
                {"object_type": "message", "object_id": "msg-b1"},
                {"object_type": "media", "object_id": "2"},
                {"object_type": "message", "object_id": "msg-a2"},
            ],
        },
    )
    assert mixed.status_code == 200
    assert (mixed.json()["requested"], mixed.json()["unique"]) == (4, 3)
    assert (mixed.json()["applied"], mixed.json()["not_found"]) == (1, 2)
    assert [item["result"] for item in mixed.json()["items"]] == [
        "favorited", "not_found", "not_found", "favorited"
    ]
    assert mixed.json()["items"][-1]["duplicate"] is True

    page = client.get("/api/favorites?tenant_id=tenant-b&limit=1&offset=0")
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert page.json()["has_more"] is True
    assert all("msg-b1" not in item["object_id"] for item in page.json()["items"])
    assert all("storage_ref" not in item for item in page.json()["items"])

    statuses = client.post(
        "/api/favorites/status",
        json={
            "items": [
                {"object_type": "message", "object_id": "msg-a1"},
                {"object_type": "media", "object_id": "1"},
                {"object_type": "message", "object_id": "msg-b1"},
            ]
        },
    )
    assert statuses.status_code == 200
    assert [item["result"] for item in statuses.json()["items"]] == [
        "found", "found", "not_found"
    ]
    assert [item["is_favorited"] for item in statuses.json()["items"]] == [True, True, None]

    with db_factory() as db:
        favorites = db.query(ArchiveFavorite).filter_by(tenant_id="tenant-a").all()
        assert len(favorites) == 3
        assert {item.object_type for item in favorites} == {"message", "media"}
        logs = db.query(AuditLog).filter(
            AuditLog.tenant_id == "tenant-a",
            AuditLog.action == "favorite.added",
        ).all()
        assert len(logs) == 4
        assert all(row.admin_user_id == identity["user_id"] for row in logs)
        assert all(row.object_id is None for row in logs)
        for row in logs:
            assert "authoritative message body" not in str(row.detail)
            assert "private-media.jpg" not in str(row.detail)
            assert "msg-a1" not in str(row.detail)


def test_favorite_cancel_reactivate_and_delete_restore_lifecycle(api_client, db_factory) -> None:
    from app.services.message_deletion import restore_messages, soft_delete_messages

    client, _identity = api_client
    for item in (
        {"object_type": "message", "object_id": "msg-a1"},
        {"object_type": "media", "object_id": "1"},
    ):
        assert client.post("/api/favorites", json=item).status_code == 200

    removed = client.delete("/api/favorites/message/msg-a1")
    assert removed.status_code == 200
    assert removed.json()["items"][0]["result"] == "unfavorited"
    repeated = client.delete("/api/favorites/message/msg-a1")
    assert repeated.status_code == 200
    assert repeated.json()["items"][0]["result"] == "already_unfavorited"
    reactivated = client.post(
        "/api/favorites", json={"object_type": "message", "object_id": "msg-a1"}
    )
    assert reactivated.status_code == 200
    with db_factory() as db:
        message_favorite = db.query(ArchiveFavorite).filter_by(
            tenant_id="tenant-a", object_type="message", archive_message_id=1
        ).one()
        stable_favorite_id = message_favorite.id
        assert message_favorite.canceled_at is None

        # Favorites do not prevent ordinary soft deletion; reads hide the
        # target's body/media but retain the relation for a later restore.
        soft_delete_messages(
            db, tenant_id="tenant-a", actor_id="owner-a", msgids=["msg-a1"]
        )
        db.commit()
        assert db.query(ArchiveFavorite).filter_by(tenant_id="tenant-a").count() == 2

    hidden = client.get("/api/favorites")
    assert hidden.status_code == 200
    assert hidden.json()["total"] == 0
    hidden_status = client.post(
        "/api/favorites/status",
        json={"items": [{"object_type": "message", "object_id": "msg-a1"}]},
    )
    assert hidden_status.json()["items"][0]["result"] == "not_found"

    with db_factory() as db:
        restore_messages(db, tenant_id="tenant-a", actor_id="owner-a", msgids=["msg-a1"])
        db.commit()
    restored = client.get("/api/favorites")
    assert restored.status_code == 200
    assert restored.json()["total"] == 2
    assert {item["object_type"] for item in restored.json()["items"]} == {"message", "media"}

    # The permanent-purge service deletes the media and message targets;
    # their FK cascades remove favorite references without blocking purge.
    from app.services.message_deletion import purge_messages

    with db_factory() as db:
        soft_delete_messages(
            db, tenant_id="tenant-a", actor_id="owner-a", msgids=["msg-a1"]
        )
        db.commit()
        result = purge_messages(
            db,
            tenant_id="tenant-a",
            msgids=["msg-a1"],
            actor_id="owner-a",
            delete_object=lambda *_args: True,
        )
        db.commit()
        assert result.purged == 1
        assert db.query(ArchiveFavorite).filter_by(tenant_id="tenant-a").count() == 0
        assert db.get(ArchiveMessage, 1) is None
        assert db.get(MediaFile, 1) is None
    assert stable_favorite_id


def test_favorite_permissions_fail_closed_and_audit_failure_rolls_back(
    api_client, db_factory, monkeypatch
) -> None:
    client, identity = api_client
    identity["role"] = "readonlyaudit"
    assert client.get("/api/favorites").status_code == 200
    denied = client.post(
        "/api/favorites", json={"object_type": "message", "object_id": "msg-a1"}
    )
    assert denied.status_code == 403
    identity["role"] = "owner"
    tenant_override = client.post(
        "/api/favorites",
        json={"object_type": "message", "object_id": "msg-a1", "tenant_id": "tenant-b"},
    )
    assert tenant_override.status_code == 422
    identity["role"] = "readonlyaudit"
    with db_factory() as db:
        assert db.query(ArchiveFavorite).count() == 0

    identity.update(role="compliance", user_id="reviewer-a")
    allowed = client.post(
        "/api/favorites", json={"object_type": "message", "object_id": "msg-a1"}
    )
    assert allowed.status_code == 200

    identity.update(tenant_id="tenant-b")
    mismatched_session = client.get("/api/favorites")
    assert mismatched_session.status_code == 403
    identity.update(tenant_id="tenant-a", role="owner", user_id="owner-a")

    import app.services.favorites as favorite_service

    monkeypatch.setattr(favorite_service, "write_audit", lambda *_args, **_kwargs: False)
    failed = client.post(
        "/api/favorites", json={"object_type": "message", "object_id": "msg-a2"}
    )
    assert failed.status_code == 503
    assert failed.json()["detail"] == "favorite_audit_unavailable"
    with db_factory() as db:
        assert db.query(ArchiveFavorite).filter_by(
            object_type="message", archive_message_id=2
        ).count() == 0


def test_concurrent_first_favorite_unique_conflict_is_idempotent(monkeypatch) -> None:
    import app.services.favorites as favorite_service

    target = favorite_service._Target("message", "msg-a1", archive_message_id=1)
    winner = SimpleNamespace(canceled_at=None)

    class _RacingSession:
        def __init__(self):
            self.lookups = 0

        def scalar(self, _statement):
            self.lookups += 1
            return None if self.lookups == 1 else winner

        @contextmanager
        def begin_nested(self):
            yield

        def add(self, _favorite):
            pass

        def flush(self):
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("insert favorite", {}, RuntimeError("unique race"))

    monkeypatch.setattr(
        favorite_service,
        "_resolve_target",
        lambda _db, _tenant_id, _target, **_kwargs: target,
    )
    db = _RacingSession()
    result = favorite_service._apply_one(
        db,
        tenant_id="tenant-a",
        actor_id="owner-a",
        target=target,
        action="favorite",
        source_page="messages",
        now=datetime.now(timezone.utc),
    )
    assert result == "already_favorited"
    assert db.lookups == 2


def test_favorite_list_filters_page_stably_and_only_projects_current_content(
    api_client,
) -> None:
    client, _identity = api_client
    assert client.post(
        "/api/favorites", json={"object_type": "message", "object_id": "msg-a1"}
    ).status_code == 200
    assert client.post(
        "/api/favorites", json={"object_type": "media", "object_id": "1"}
    ).status_code == 200
    assert client.post(
        "/api/favorites", json={"object_type": "message", "object_id": "msg-a2"}
    ).status_code == 200

    page = client.get("/api/favorites?conversation_id=room-a&limit=2&offset=0")
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 2
    assert page.json()["has_more"] is True
    assert all(item["conversation_id"] == "room-a" for item in page.json()["items"])
    assert "authoritative message body" in str(page.json())

    second = client.get("/api/favorites?conversation_id=room-a&limit=2&offset=2")
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert second.json()["has_more"] is False
    messages = client.get("/api/favorites?object_type=message&message_since_ms=1500")
    assert messages.status_code == 200
    assert messages.json()["total"] == 1
    assert messages.json()["items"][0]["object_id"] == "msg-a2"
    filtered_by_actor = client.get("/api/favorites?favorited_by=owner-a")
    assert filtered_by_actor.status_code == 200
    assert filtered_by_actor.json()["total"] == 3
    collision_id = "direct__contact_1___staff_1"
    assert client.post(
        "/api/favorites", json={"object_type": "message", "object_id": "msg-a4"}
    ).status_code == 200
    assert client.post(
        "/api/favorites", json={"object_type": "message", "object_id": "msg-a5"}
    ).status_code == 200
    assert client.get(f"/api/favorites?conversation_id={collision_id}").status_code == 422
    direct = client.get(
        f"/api/favorites?conversation_id={collision_id}&mode=staff&staff_id=staff_1"
    )
    assert direct.status_code == 200
    assert [item["object_id"] for item in direct.json()["items"]] == ["msg-a5"]
    group = client.get(
        f"/api/favorites?conversation_id={collision_id}&mode=staff&staff_id=staff_2"
    )
    assert group.status_code == 200
    assert [item["object_id"] for item in group.json()["items"]] == ["msg-a4"]
    assert client.get(
        f"/api/favorites?conversation_id={collision_id}&mode=staff"
    ).status_code == 422
    assert client.get("/api/favorites?limit=101").status_code == 422
    assert client.get(
        "/api/favorites?favorited_since=2026-01-02T00:00:00Z&favorited_until=2026-01-01T00:00:00Z"
    ).status_code == 422
