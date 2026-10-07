"""RND-368 server-side favorite filtering for chat timelines."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

from app.db.models import ArchiveFavorite, ArchiveMessage, ArchiveMessageRecipient, Contact
from tests.test_rnd366_favorites import api_client, db_factory  # noqa: F401


def test_favorited_only_is_conversation_scoped_paginated_and_respects_restore(
    api_client, db_factory, monkeypatch
):
    from app.routers import conversations

    monkeypatch.setattr(
        conversations, "attach_group_chat_display_name", lambda _db, _tenant, _conv, _type, page: page
    )
    Contact.__table__.create(db_factory.kw["bind"])
    client, _identity = api_client
    with db_factory() as db:
        db.add_all(
            [
                ArchiveMessage(
                    id=6, msgid="msg-a6", seq=6, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="favorite newer group message",
                    msgtype="text", sender="staff_1", roomid="room-a", msgtime=3000,
                    tenant_id="tenant-a",
                ),
                ArchiveMessage(
                    id=7, msgid="msg-a7", seq=7, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="ordinary direct message",
                    msgtype="text", sender="staff_1", roomid=None, msgtime=6000,
                    tenant_id="tenant-a",
                ),
                ArchiveMessage(
                    id=8, msgid="msg-a8", seq=8, publickey_ver=1,
                    encrypt_random_key="key", encrypt_chat_msg="payload",
                    decrypt_status="success", content_text="ordinary newest group message",
                    msgtype="text", sender="staff_1", roomid="room-a", msgtime=4000,
                    tenant_id="tenant-a",
                ),
            ]
        )
        db.add(
            ArchiveMessageRecipient(
                message_id=7, tenant_id="tenant-a", receiver_userid="contact_1"
            )
        )
        db.flush()
        db.add_all(
            [
                ArchiveFavorite(
                    id="favorite-a1", tenant_id="tenant-a", object_type="message",
                    archive_message_id=1, favorited_by_admin_user_id="owner-a",
                    source_page="messages", favorited_at=datetime.now(timezone.utc),
                ),
                ArchiveFavorite(
                    id="favorite-a6", tenant_id="tenant-a", object_type="message",
                    archive_message_id=6, favorited_by_admin_user_id="owner-a",
                    source_page="messages", favorited_at=datetime.now(timezone.utc),
                ),
                ArchiveFavorite(
                    id="favorite-a5", tenant_id="tenant-a", object_type="message",
                    archive_message_id=5, favorited_by_admin_user_id="owner-a",
                    source_page="messages", favorited_at=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

    base = "/api/conversations/room-a/messages?mode=staff&staff_id=staff_1&conversation_type=group&limit=1"
    # The legacy rollback snapshot predates favorites. Filtered reads must
    # keep using the maintained resolver even while the rollback flag is set.
    monkeypatch.setenv("WEARCHIVE_LEGACY_TIMELINE", "1")
    first = client.get(base + "&favorited_only=true")
    assert first.status_code == 200, first.text
    assert [item["msgid"] for item in first.json()["messages"]] == ["msg-a6"]
    assert first.json()["pagination"]["has_older"] is True
    cursor = first.json()["pagination"]["next_before"]
    older = client.get(base + "&favorited_only=true&" + urlencode({"before": cursor}))
    assert older.status_code == 200
    assert [item["msgid"] for item in older.json()["messages"]] == ["msg-a1"]
    assert older.json()["pagination"]["has_older"] is False

    unfiltered = client.get(base)
    assert unfiltered.status_code == 200
    assert [item["msgid"] for item in unfiltered.json()["messages"]] == ["msg-a8"]

    # A direct/group-collision ID uses the authoritative fallback resolver;
    # the favorite filter must still be applied within the selected direct side.
    collision = (
        "/api/conversations/direct__contact_1___staff_1/messages"
        "?mode=staff&staff_id=staff_1&conversation_type=direct&favorited_only=true"
    )
    direct = client.get(collision)
    assert direct.status_code == 200
    assert [item["msgid"] for item in direct.json()["messages"]] == ["msg-a5"]

    monkeypatch.delenv("WEARCHIVE_LEGACY_TIMELINE", raising=False)
    # Soft deletion hides the message from the filtered timeline without
    # deleting its shared favorite relation; restore makes it queryable again.
    with db_factory() as db:
        message = db.get(ArchiveMessage, 1)
        message.deleted_at = datetime.now(timezone.utc)
        db.commit()
    after_delete = client.get(base + "&favorited_only=true&limit=10")
    assert after_delete.status_code == 200
    assert [item["msgid"] for item in after_delete.json()["messages"]] == ["msg-a6"]
    ordinary_after_delete = client.get(base + "&limit=10")
    assert [item["msgid"] for item in ordinary_after_delete.json()["messages"]] == ["msg-a2", "msg-a6", "msg-a8"]
    with db_factory() as db:
        message = db.get(ArchiveMessage, 1)
        message.deleted_at = None
        db.commit()
    after_restore = client.get(base + "&favorited_only=true&limit=10")
    assert after_restore.status_code == 200
    assert [item["msgid"] for item in after_restore.json()["messages"]] == ["msg-a1", "msg-a6"]
