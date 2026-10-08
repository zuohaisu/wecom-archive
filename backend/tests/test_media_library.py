"""RND-334 coverage for media-library conversation locator fields."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient

from app.db.models import MediaFile
from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    _insert_recipient,
    db,  # noqa: F401 - fixture
)


@pytest.fixture()
def client(db):
    from app.db.session import get_db
    from app.main import app

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _authed() -> None:
    from app.auth import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: (
        MagicMock(id="admin-a", role="admin"),
        _TENANT_A,
    )


def _insert_media(db, message_id: int, **kwargs) -> MediaFile:
    media = MediaFile(
        tenant_id=_TENANT_A,
        archive_message_id=message_id,
        sdkfileid=kwargs.pop("sdkfileid", f"sdk-{message_id}"),
        file_type="image",
        download_status="downloaded",
        created_at=kwargs.pop("created_at", datetime.now(timezone.utc)),
        **kwargs,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def test_media_list_includes_group_locator_fields_and_preview_url(client, db, monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    image = tmp_path / "group.jpg"
    image.write_bytes(b"group-image")
    message = _insert_message(
        db, msgid="group-media-msg", msgtype="image", sender="staff-alice", roomid="room-group", tenant_id=_TENANT_A
    )
    _insert_recipient(db, message.id, "contact-group")
    _insert_media(db, message.id, local_path=str(image))
    _authed()

    response = client.get("/api/admin/media")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["msgid"] == message.msgid
    assert item["conversation_id"] == message.roomid
    assert item["session_title"] == "Group chat · -group"
    assert item["message_id"] == message.id
    assert item["room_id"] == message.roomid

    preview = client.get(
        f"/api/conversations/{item['conversation_id']}/messages/{item['msgid']}/media"
    )
    assert preview.status_code == 200
    assert preview.content == b"group-image"


def test_media_list_uses_shared_direct_id_and_display_name_fallback(client, db):
    from app.conversation_membership import _direct_conv_id

    message = _insert_message(
        db, msgid="direct-media-msg", msgtype="image", sender="staff-alice", tenant_id=_TENANT_A
    )
    _insert_recipient(db, message.id, "contact-missing-name")
    _insert_media(db, message.id)
    _authed()

    response = client.get("/api/admin/media")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["msgid"] == message.msgid
    assert item["conversation_id"] == _direct_conv_id("staff-alice", "contact-missing-name")
    # No Contact record exists: resolve_person_display_name falls back to the raw userid.
    assert item["session_title"] == "contact-missing-name"


def test_media_list_malformed_direct_row_has_visible_nonempty_fallback(client, db):
    message = _insert_message(
        db, msgid="missing-recipient-media-msg", msgtype="image", sender="staff-alice", tenant_id=_TENANT_A
    )
    _insert_media(db, message.id)
    _authed()

    response = client.get("/api/admin/media")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["conversation_id"]
    assert item["session_title"] == "unknown"


def test_media_list_favorites_filter_is_server_side_tenant_scoped_and_active(
    client, db
):
    favorite_message = _insert_message(
        db,
        msgid="favorite-media-message",
        msgtype="image",
        sender="staff-alice",
        tenant_id=_TENANT_A,
    )
    favorite_media = _insert_media(db, favorite_message.id)
    ordinary_message = _insert_message(
        db,
        msgid="ordinary-media-message",
        msgtype="file",
        sender="staff-alice",
        tenant_id=_TENANT_A,
    )
    ordinary_media = _insert_media(db, ordinary_message.id)
    deleted_message = _insert_message(
        db,
        msgid="deleted-media-message",
        msgtype="video",
        sender="staff-alice",
        tenant_id=_TENANT_A,
    )
    deleted_media = _insert_media(db, deleted_message.id)
    deleted_message.deleted_at = datetime.now(timezone.utc)
    db.commit()
    db.execute(
        text(
            "CREATE TABLE archive_favorites ("
            "id TEXT PRIMARY KEY, tenant_id TEXT, object_type TEXT, "
            "media_file_id INTEGER, canceled_at DATETIME)"
        )
    )
    db.execute(
        text(
            "INSERT INTO archive_favorites "
            "(id, tenant_id, object_type, media_file_id, canceled_at) "
            "VALUES (:id, :tenant_id, 'media', :media_file_id, :canceled_at)"
        ),
        [
            {
                "id": "active-favorite",
                "tenant_id": _TENANT_A,
                "media_file_id": favorite_media.id,
                "canceled_at": None,
            },
            {
                "id": "canceled-favorite",
                "tenant_id": _TENANT_A,
                "media_file_id": ordinary_media.id,
                "canceled_at": datetime.now(timezone.utc),
            },
            {
                "id": "cross-tenant-favorite",
                "tenant_id": _TENANT_B,
                "media_file_id": ordinary_media.id,
                "canceled_at": None,
            },
            {
                "id": "deleted-favorite",
                "tenant_id": _TENANT_A,
                "media_file_id": deleted_media.id,
                "canceled_at": None,
            },
        ],
    )
    db.commit()
    _authed()

    response = client.get("/api/admin/media?favorited_only=true")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["has_more"] is False
    assert [item["id"] for item in response.json()["items"]] == [favorite_media.id]
