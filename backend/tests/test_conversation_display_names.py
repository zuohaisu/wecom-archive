"""
Tests for RND-129 — conversation aggregation and display-name improvements.

Production backfill of contacts.name is blocked by WeCom trusted domain/IP
configuration (RND-130), so these tests specifically exercise the case where
contacts.name is missing and the UI must still be readable.

Validates:
  - app.display_names resolvers never return a blank primary label.
  - _build_conversation_list prefers contacts.name for direct conversations
    and falls back to a readable label otherwise.
  - Group conversations never use the bare roomid as the only primary label.
  - /api/conversations/{id}/messages exposes sender/recipient display fields.
  - /api/contacts exposes raw_id alongside display_name.

Run (from backend/):
    pytest tests/test_conversation_display_names.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# app.display_names — pure resolver functions, no DB required
# ---------------------------------------------------------------------------


def test_resolve_person_display_name_prefers_name() -> None:
    from app.display_names import resolve_person_display_name

    assert resolve_person_display_name("contact_zhangsan", "张三") == "张三"


def test_resolve_person_display_name_strips_whitespace_only_name() -> None:
    from app.display_names import resolve_person_display_name

    assert resolve_person_display_name("contact_zhangsan", "   ") == "contact_zhangsan"


def test_resolve_person_display_name_falls_back_to_raw_id() -> None:
    from app.display_names import resolve_person_display_name

    assert resolve_person_display_name("contact_wangwu", None) == "contact_wangwu"


def test_resolve_person_display_name_never_blank() -> None:
    from app.display_names import resolve_person_display_name

    assert resolve_person_display_name("", "").strip() != ""
    assert resolve_person_display_name(None, None).strip() != ""


def test_resolve_room_display_name_falls_back_when_no_room_name() -> None:
    from app.display_names import resolve_room_display_name

    label = resolve_room_display_name("wra_abcdef123456")
    assert label != "wra_abcdef123456"
    assert label.startswith("Group chat")
    assert "abcdef123456"[-6:] in label


def test_resolve_room_display_name_uses_room_name_when_available() -> None:
    from app.display_names import resolve_room_display_name

    assert resolve_room_display_name("wra_abcdef123456", "Sales Team") == "Sales Team"


def test_resolve_room_display_name_never_blank_with_no_roomid() -> None:
    from app.display_names import resolve_room_display_name

    assert resolve_room_display_name(None).strip() != ""


# ---------------------------------------------------------------------------
# _build_conversation_list — pure aggregation function, no DB required
# ---------------------------------------------------------------------------


def _msg(id, sender, roomid=None, msgtime=0, content_text=""):
    return SimpleNamespace(
        id=id, sender=sender, roomid=roomid, msgtime=msgtime, content_text=content_text
    )


def test_direct_conversation_prefers_contact_display_name() -> None:
    from app.routers.conversations import _build_conversation_list

    messages = [_msg(1, "staff_yingzi", msgtime=100)]
    recipients_map = {1: ["contact_zhangsan"]}
    display_names = {"contact_zhangsan": "张三"}

    convs = _build_conversation_list(messages, recipients_map, display_names)
    assert len(convs) == 1
    conv = convs[0]
    assert conv["conversation_type"] == "direct"
    assert conv["display_name"] == "张三"
    assert conv["raw_id"] == "contact_zhangsan"
    assert conv["contact_display_names"] == ["张三"]
    assert conv["contact_raw_ids"] == ["contact_zhangsan"]
    assert conv["monitored_account_raw_ids"] == ["staff_yingzi"]


def test_direct_conversation_fallback_is_readable_when_name_missing() -> None:
    """No contacts.name known — must still show a stable, non-blank label (RND-130 backfill blocked)."""
    from app.routers.conversations import _build_conversation_list

    messages = [_msg(1, "staff_yingzi", msgtime=100)]
    recipients_map = {1: ["contact_wangwu"]}
    display_names: dict = {}

    convs = _build_conversation_list(messages, recipients_map, display_names)
    conv = convs[0]
    assert conv["display_name"] == "contact_wangwu"
    assert conv["display_name"].strip() != ""
    assert conv["raw_id"] == "contact_wangwu"


def test_group_conversation_does_not_use_raw_roomid_as_only_label() -> None:
    from app.routers.conversations import _build_conversation_list

    messages = [_msg(1, "contact_zhangsan", roomid="wra_group_room_007", msgtime=100)]
    recipients_map = {1: ["staff_yingzi"]}
    display_names: dict = {}

    convs = _build_conversation_list(messages, recipients_map, display_names)
    conv = convs[0]
    assert conv["conversation_type"] == "group"
    assert conv["display_name"] != "wra_group_room_007"
    assert conv["display_name"].startswith("Group chat")
    # Full roomid must remain available as secondary/debug data.
    assert conv["roomid"] == "wra_group_room_007"
    assert conv["room_raw_id"] == "wra_group_room_007"


def test_latest_sender_display_name_and_raw_id_reflect_most_recent_message() -> None:
    from app.routers.conversations import _build_conversation_list

    messages = [
        _msg(1, "staff_yingzi", msgtime=100),
        _msg(2, "contact_zhangsan", msgtime=200),
    ]
    recipients_map = {1: ["contact_zhangsan"], 2: ["staff_yingzi"]}
    display_names = {"contact_zhangsan": "张三"}

    convs = _build_conversation_list(messages, recipients_map, display_names)
    conv = convs[0]
    assert conv["latest_sender_id"] == "contact_zhangsan"
    assert conv["latest_sender_raw_id"] == "contact_zhangsan"
    assert conv["latest_sender_display_name"] == "张三"


# ---------------------------------------------------------------------------
# Route-level — /api/conversations/{conversation_id}/messages (group path)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_group_messages_endpoint_includes_sender_and_recipient_display_fields(client) -> None:
    from app.auth import get_current_user
    from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact
    from app.db.session import get_db
    from app.main import app

    msg = MagicMock()
    msg.id = 501
    msg.msgid = "m-501"
    msg.sender = "contact_zhangsan"
    msg.roomid = "wra_room_abc123"
    msg.msgtime = 1700000000000
    msg.msgtype = "text"
    msg.content_text = "hello group"
    msg.decrypt_status = "success"

    contact_row = MagicMock()
    contact_row.wecom_userid = "contact_zhangsan"
    contact_row.name = "张三"

    rcpt_row = MagicMock()
    rcpt_row.message_id = 501
    rcpt_row.receiver_userid = "staff_yingzi"

    def _override_db():
        mock = MagicMock()

        msg_q = MagicMock()
        msg_q.filter.return_value = msg_q
        msg_q.all.return_value = [msg]

        rcpt_q = MagicMock()
        rcpt_q.filter.return_value = rcpt_q
        rcpt_q.all.return_value = [rcpt_row]

        contact_q = MagicMock()
        contact_q.filter.return_value = contact_q
        contact_q.all.return_value = [contact_row]

        def _query(model):
            if model is ArchiveMessageRecipient:
                return rcpt_q
            if model is Contact:
                return contact_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    mock_user = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: (mock_user, "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    try:
        resp = client.get("/api/conversations/wra_room_abc123/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        m = data[0]
        assert m["sender"] == "contact_zhangsan"
        assert m["sender_display_name"] == "张三"
        assert m["sender_raw_id"] == "contact_zhangsan"
        assert m["recipients"] == ["staff_yingzi"]
        # No Contact row for staff_yingzi -> fallback is the raw ID, never blank.
        assert m["recipient_display_names"] == ["staff_yingzi"]
        assert m["recipient_raw_ids"] == ["staff_yingzi"]
    finally:
        app.dependency_overrides.clear()


def test_group_messages_endpoint_still_requires_auth(client) -> None:
    from app.db.session import get_db
    from app.main import app

    def _mock_db_no_session():
        mock = MagicMock()
        mock.query.return_value.filter.return_value.first.return_value = None
        yield mock

    app.dependency_overrides[get_db] = _mock_db_no_session
    try:
        resp = client.get("/api/conversations/wra_room_abc123/messages")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Route-level — /api/contacts exposes raw_id alongside display_name
# ---------------------------------------------------------------------------


def test_contacts_endpoint_includes_raw_id_field(client) -> None:
    from app.auth import get_current_user
    from app.db.models import Contact
    from app.db.session import get_db
    from app.main import app

    contact_row = MagicMock()
    contact_row.wecom_userid = "contact_zhangsan"
    contact_row.name = "张三"

    def _override_db():
        mock = MagicMock()

        sender_q = MagicMock()
        sender_q.filter.return_value = sender_q
        sender_q.distinct.return_value = sender_q
        sender_q.all.return_value = [("contact_zhangsan",)]

        recipient_q = MagicMock()
        recipient_q.filter.return_value = recipient_q
        recipient_q.distinct.return_value = recipient_q
        recipient_q.all.return_value = [("contact_wangwu",)]

        contact_q = MagicMock()
        contact_q.filter.return_value = contact_q
        contact_q.all.return_value = [contact_row]

        def _query(target):
            key = getattr(target, "key", None)
            if key == "sender":
                return sender_q
            if key == "receiver_userid":
                return recipient_q
            if target is Contact:
                return contact_q
            raise AssertionError(f"unexpected query target: {target}")

        mock.query.side_effect = _query
        yield mock

    mock_user = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: (mock_user, "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    try:
        resp = client.get("/api/contacts")
        assert resp.status_code == 200
        by_id = {row["contact_id"]: row for row in resp.json()}
        assert by_id["contact_zhangsan"]["raw_id"] == "contact_zhangsan"
        assert by_id["contact_zhangsan"]["display_name"] == "张三"
        # Unknown contact: raw_id present, display_name falls back but is never blank.
        assert by_id["contact_wangwu"]["raw_id"] == "contact_wangwu"
        assert by_id["contact_wangwu"]["display_name"] == "contact_wangwu"
    finally:
        app.dependency_overrides.clear()
