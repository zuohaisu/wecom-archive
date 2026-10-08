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
    Contact,
    ExternalContact,
    GroupChatMetadata,
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
                Contact.__table__,
                ExternalContact.__table__,
                GroupChatMetadata.__table__,
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


def test_favorite_batch_uses_canonical_target_lock_order() -> None:
    import app.services.favorites as favorite_service
    from app.schemas.favorites import FavoriteObjectIn

    forward = [
        FavoriteObjectIn(object_type="message", object_id="msg-a2"),
        FavoriteObjectIn(object_type="message", object_id="msg-a1"),
    ]
    reverse = list(reversed(forward))
    forward_targets, _ = favorite_service._unique_targets(forward)
    reverse_targets, _ = favorite_service._unique_targets(reverse)

    assert [target.key for target in forward_targets] == [
        ("message", "msg-a1"),
        ("message", "msg-a2"),
    ]
    assert [target.key for target in reverse_targets] == [
        target.key for target in forward_targets
    ]


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


def test_favorite_conversation_filter_uses_compact_message_projection(
    api_client, db_factory
) -> None:
    client, _identity = api_client
    with db_factory() as db:
        messages = [
            ArchiveMessage(
                id=1000 + index,
                msgid=f"large-room-{index}",
                seq=index,
                publickey_ver=1,
                encrypt_random_key="k" * 2048,
                encrypt_chat_msg="c" * 2048,
                decrypt_status="success",
                content_text=f"synthetic large-room message {index}",
                msgtype="text",
                sender="staff_1",
                roomid="large-room",
                msgtime=10_000 + index,
                tenant_id="tenant-a",
            )
            for index in range(80)
        ]
        db.add_all(messages)
        db.flush()
        db.add(
            ArchiveFavorite(
                id="large-room-favorite",
                tenant_id="tenant-a",
                object_type="message",
                archive_message_id=messages[-1].id,
                favorited_by_admin_user_id="owner-a",
            )
        )
        db.commit()

    loaded_full_messages = 0

    def _count_archive_message_load(_target, _context):
        nonlocal loaded_full_messages
        loaded_full_messages += 1

    event.listen(ArchiveMessage, "load", _count_archive_message_load)
    try:
        response = client.get("/api/favorites?conversation_id=large-room&limit=1")
    finally:
        event.remove(ArchiveMessage, "load", _count_archive_message_load)

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["object_id"] == "large-room-79"
    assert loaded_full_messages == 0


@pytest.mark.parametrize(
    ("deleted_msgid", "active_msgid"),
    [("msg-a4", "msg-a5"), ("msg-a5", "msg-a4")],
    ids=("deleted-group-side", "deleted-direct-side"),
)
def test_favorite_collision_filter_ignores_soft_deleted_side(
    api_client, db_factory, deleted_msgid, active_msgid
) -> None:
    from app.services.message_deletion import soft_delete_messages

    client, _identity = api_client
    for msgid in ("msg-a4", "msg-a5"):
        response = client.post(
            "/api/favorites",
            json={"object_type": "message", "object_id": msgid},
        )
        assert response.status_code == 200

    with db_factory() as db:
        result = soft_delete_messages(
            db,
            tenant_id="tenant-a",
            actor_id="owner-a",
            msgids=[deleted_msgid],
        )
        db.commit()
        assert result.deleted == 1

    response = client.get(
        "/api/favorites?conversation_id=direct__contact_1___staff_1"
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["object_id"] == active_msgid


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


def test_rnd369_favorites_filters_details_and_stable_pagination(api_client, db_factory) -> None:
    client, _identity = api_client
    with db_factory() as db:
        db.query(AdminUser).filter_by(id="owner-a").one().name = "Owner Alice"
        db.query(AdminUser).filter_by(id="owner-b").one().name = "Owner Bob"
        db.add_all(
            [
                Contact(tenant_id="tenant-a", wecom_userid="staff_1", name="Alice"),
                Contact(tenant_id="tenant-a", wecom_userid="contact_1", name="Legacy Contact"),
                ExternalContact(
                    tenant_id="tenant-a",
                    external_userid="contact_1",
                    name="Employee Remark",
                    current_nickname_display="Customer One",
                ),
                GroupChatMetadata(
                    tenant_id="tenant-a", roomid="room-a", display_name="Support Room"
                ),
            ]
        )
        db.commit()

    for object_type, object_id in (
        ("message", "msg-a1"),
        ("message", "msg-a2"),
        ("media", "1"),
        ("message", "msg-a5"),
    ):
        assert client.post(
            "/api/favorites",
            json={"object_type": object_type, "object_id": object_id},
        ).status_code == 200

    page_one = client.get("/api/favorites?limit=2&offset=0")
    page_two = client.get("/api/favorites?limit=2&offset=2")
    assert page_one.status_code == page_two.status_code == 200
    assert page_one.json()["total"] == 4
    assert page_one.json()["has_more"] is True
    assert page_two.json()["has_more"] is False
    assert not {
        item["favorite_id"] for item in page_one.json()["items"]
    } & {item["favorite_id"] for item in page_two.json()["items"]}

    media = client.get("/api/favorites?media_type=image")
    assert media.status_code == 200
    assert media.json()["total"] == 1
    assert media.json()["items"][0]["object_type"] == "media"
    assert media.json()["items"][0]["media_download_status"] == "downloaded"
    assert client.get("/api/favorites?media_type=image&object_type=message").status_code == 422

    by_staff = client.get("/api/favorites?staff_filter=staff_1")
    assert by_staff.status_code == 200
    assert by_staff.json()["total"] == 4
    assert client.get("/api/favorites?staff_filter=unknown-contact").json()["total"] == 0
    by_actor = client.get("/api/favorites?favorited_by_name=Owner%20Alice")
    assert by_actor.status_code == 200
    assert by_actor.json()["total"] == 4
    assert by_actor.json()["items"][0]["favorited_by_name"] == "Owner Alice"
    assert client.get("/api/favorites?favorited_by_name=Owner%20Bob").json()["total"] == 0

    by_conversation = client.get("/api/favorites?conversation_id=room-a")
    assert by_conversation.status_code == 200
    assert by_conversation.json()["total"] == 3
    assert all(item["conversation_name"] == "Support Room" for item in by_conversation.json()["items"])
    assert all(item["staff_name"] == "Alice" for item in by_conversation.json()["items"])

    direct = client.get(
        "/api/favorites?conversation_id=direct__contact_1___staff_1&mode=staff&staff_id=staff_1"
    )
    assert direct.status_code == 200
    assert direct.json()["total"] == 1
    direct_item = direct.json()["items"][0]
    assert direct_item["object_id"] == direct_item["message_id"] == "msg-a5"
    assert direct_item["conversation_name"] == "Alice ↔ Customer One"
    assert direct_item["contact_name"] == "Customer One"
    assert direct_item["focus_entity_type"] == "staff"
    assert direct_item["focus_entity_id"] == "staff_1"
    for contact_filter in ("Customer One", "contact_1"):
        by_contact = client.get(
            "/api/favorites", params={"contact_filter": contact_filter}
        )
        assert by_contact.status_code == 200
        assert [item["object_id"] for item in by_contact.json()["items"]] == ["msg-a5"]

    by_message_time = client.get("/api/favorites?message_since_ms=1500")
    assert by_message_time.status_code == 200
    assert {item["object_id"] for item in by_message_time.json()["items"]} == {
        "msg-a2", "msg-a5"
    }


def test_rnd369_contact_id_filter_matches_archive_only_participants(api_client, db_factory) -> None:
    client, _identity = api_client
    with db_factory() as db:
        db.add_all(
            [
                ArchiveMessage(
                    id=6, msgid="msg-archive-only-sender", seq=6, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="sender-side message",
                    msgtype="text", sender="archive_only_sender", roomid="room-a",
                    msgtime=6000, tenant_id="tenant-a",
                ),
                ArchiveMessage(
                    id=7, msgid="msg-archive-only-recipient", seq=7, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="recipient-side message",
                    msgtype="text", sender="staff_1", roomid=None,
                    msgtime=7000, tenant_id="tenant-a",
                ),
                ArchiveMessageRecipient(
                    message_id=7, tenant_id="tenant-a", receiver_userid="archive_only_recipient"
                ),
            ]
        )
        db.commit()

    for message_id in ("msg-archive-only-sender", "msg-archive-only-recipient"):
        assert client.post(
            "/api/favorites",
            json={"object_type": "message", "object_id": message_id},
        ).status_code == 200

    by_sender_id = client.get(
        "/api/favorites", params={"contact_filter": "archive_only_sender"}
    )
    by_recipient_id = client.get(
        "/api/favorites", params={"contact_filter": "archive_only_recipient"}
    )
    assert [item["object_id"] for item in by_sender_id.json()["items"]] == [
        "msg-archive-only-sender"
    ]
    assert [item["object_id"] for item in by_recipient_id.json()["items"]] == [
        "msg-archive-only-recipient"
    ]


def test_rnd369_nested_media_favorite_projects_safe_item_path(api_client, db_factory) -> None:
    client, _identity = api_client
    with db_factory() as db:
        db.add(
            ArchiveMessage(
                id=6, msgid="msg-nested-media", seq=6, publickey_ver=1,
                encrypt_random_key="key", encrypt_chat_msg="payload",
                decrypt_status="success", content_text="nested attachment",
                msgtype="mixed", sender="staff_1", roomid="room-a",
                msgtime=6000, tenant_id="tenant-a",
                structured_content={
                    "media_refs": [{"path": "0.1", "type": "image", "sdkfileid": "synthetic-nested-id"}]
                },
            )
        )
        db.add(
            MediaFile(
                id=3, sdkfileid="synthetic-nested-id", archive_message_id=6,
                tenant_id="tenant-a", file_type="image", mime_type="image/jpeg",
                file_size=4, download_status="downloaded", storage_backend="local",
                storage_ref="synthetic-private-reference.jpg",
            )
        )
        db.commit()

    assert client.post(
        "/api/favorites", json={"object_type": "media", "object_id": "3"}
    ).status_code == 200
    response = client.get("/api/favorites?object_type=media")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["media_item_path"] == "0.1"
    serialized = response.text
    assert "synthetic-nested-id" not in serialized
    assert "synthetic-private-reference.jpg" not in serialized


def test_rnd369_favorites_page_requires_html_session_and_renders_controls(api_client) -> None:
    from app.auth import require_html_session
    from app.main import app

    client, _identity = api_client
    app.dependency_overrides[require_html_session] = lambda: None
    anonymous = client.get("/admin/favorites", follow_redirects=False)
    assert anonymous.status_code == 302
    assert anonymous.headers["location"] == "/admin/login"

    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    page = client.get("/admin/favorites")
    assert page.status_code == 200
    assert 'id="favorites-filters"' in page.text
    assert 'id="favorites-filter-staff"' in page.text
    assert 'id="favorites-filter-favorite-since"' in page.text
    assert 'id="favorites-unfavorite-selected"' in page.text
    assert 'id="favorites-export-selected"' in page.text
    assert 'data-i18n="favoritesPage.removeOnlyNotice"' in page.text
    assert 'favorites-page.js' in page.text
    assert 'console/media-viewer.js' in page.text
    assert "/admin/favorites" in page.text
