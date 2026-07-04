"""
Tests for RND-133 Phase 1 — media classification and safe timeline exposure.

Phase 1 scope only: classify msgtype into a stable media_type/media_status/
unsupported_reason contract, and expose it on the timeline message API.
No media download, no file-serving route, no DB migration — see
app/media_classification.py and the RND-133 module docstring there for the
full rationale.

Validates:
  - classify_media() covers text/image/video/voice/file/other/missing per
    the RND-133 Phase 1 mapping (pure function, no DB required).
  - /api/conversations/{id}/messages exposes media_type/media_status/
    unsupported_reason per message.
  - The timeline API never leaks sdkfileid, local_path, oss_key, or any
    encrypted/decrypted payload field.
  - Existing RND-132 pagination behavior is untouched by these additions.

Run (from backend/):
    pytest tests/test_media_classification.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.test_staff_seats import _msg


# ---------------------------------------------------------------------------
# classify_media — pure function, no DB required
# ---------------------------------------------------------------------------


def test_classify_media_text() -> None:
    from app.media_classification import classify_media

    result = classify_media("text", has_sdkfileid=False)
    assert result.media_type == "text"
    assert result.media_status is None
    assert result.unsupported_reason is None


def test_classify_media_image_with_sdkfileid() -> None:
    from app.media_classification import classify_media

    result = classify_media("image", has_sdkfileid=True)
    assert result.media_type == "image"
    assert result.media_status == "not_downloaded"
    assert result.unsupported_reason == "media_download_not_implemented"


def test_classify_media_image_without_sdkfileid() -> None:
    from app.media_classification import classify_media

    result = classify_media("image", has_sdkfileid=False)
    assert result.media_type == "image"
    assert result.media_status == "unknown"
    assert result.unsupported_reason == "media_download_not_implemented"


def test_classify_media_video() -> None:
    from app.media_classification import classify_media

    result = classify_media("video", has_sdkfileid=True)
    assert result.media_type == "video"
    assert result.media_status in ("not_downloaded", "unsupported")
    assert result.unsupported_reason


def test_classify_media_voice() -> None:
    from app.media_classification import classify_media

    result = classify_media("voice", has_sdkfileid=True)
    assert result.media_type == "voice"
    assert result.media_status in ("not_downloaded", "unsupported")
    assert result.unsupported_reason


def test_classify_media_file() -> None:
    from app.media_classification import classify_media

    result = classify_media("file", has_sdkfileid=True)
    assert result.media_type == "file"
    assert result.media_status in ("not_downloaded", "unsupported")
    assert result.unsupported_reason


def test_classify_media_unsupported_msgtype() -> None:
    from app.media_classification import classify_media

    result = classify_media("emotion", has_sdkfileid=False)
    assert result.media_type == "unsupported"
    assert result.media_status == "unsupported"
    assert result.unsupported_reason == "unsupported_msgtype"


def test_classify_media_missing_msgtype() -> None:
    from app.media_classification import classify_media

    for empty in (None, ""):
        result = classify_media(empty, has_sdkfileid=False)
        assert result.media_type == "unknown"
        assert result.media_status == "unknown"
        assert result.unsupported_reason == "missing_msgtype"


# ---------------------------------------------------------------------------
# /api/conversations/{id}/messages — media fields + no raw identifier leaks
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _run_messages_query(client, app, all_msgs):
    from app.auth import get_current_user
    from app.db.models import ArchiveMessageRecipient, Contact
    from app.db.session import get_db

    def _override_db():
        mock = MagicMock()

        msg_q = MagicMock()
        msg_q.filter.return_value = msg_q
        msg_q.all.return_value = list(all_msgs)

        rcpt_q = MagicMock()
        rcpt_q.filter.return_value = rcpt_q
        rcpt_q.all.return_value = []

        contact_q = MagicMock()
        contact_q.filter.return_value = contact_q
        contact_q.all.return_value = []

        def _query(model):
            if model is ArchiveMessageRecipient:
                return rcpt_q
            if model is Contact:
                return contact_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    try:
        return client.get("/api/conversations/room1/messages")
    finally:
        app.dependency_overrides.clear()


def test_timeline_text_message_media_fields(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, content_text="hello", msgtype="text")]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["content_text"] == "hello"
    assert msg["media_type"] == "text"
    assert msg["media_status"] is None
    assert msg["unsupported_reason"] is None


def test_timeline_image_with_sdkfileid_not_downloaded(client) -> None:
    from app.main import app

    all_msgs = [
        _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-abc123")
    ]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "image"
    assert msg["media_status"] == "not_downloaded"


def test_timeline_image_without_sdkfileid_unknown_status(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid=None)]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "image"
    assert msg["media_status"] == "unknown"


def test_timeline_video_placeholder_status(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="video", sdkfileid="sdk-v1")]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "video"
    assert msg["media_status"] in ("not_downloaded", "unsupported")
    assert msg["unsupported_reason"]


def test_timeline_voice_placeholder_status(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="voice", sdkfileid="sdk-a1")]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "voice"
    assert msg["media_status"] in ("not_downloaded", "unsupported")
    assert msg["unsupported_reason"]


def test_timeline_file_placeholder_status(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="file", sdkfileid="sdk-f1")]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "file"
    assert msg["media_status"] in ("not_downloaded", "unsupported")
    assert msg["unsupported_reason"]


def test_timeline_unsupported_msgtype(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="emotion")]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "unsupported"
    assert msg["unsupported_reason"] == "unsupported_msgtype"


def test_timeline_missing_msgtype(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype=None)]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "unknown"
    assert msg["unsupported_reason"] == "missing_msgtype"


def test_timeline_never_exposes_raw_media_identifiers(client) -> None:
    """
    The timeline response must never leak sdkfileid, local_path, oss_key, or
    any encrypted/decrypted payload field — only the safe classification
    fields (media_type/media_status/unsupported_reason) are exposed.
    """
    from app.main import app

    all_msgs = [
        _msg(
            1,
            "staff_a",
            roomid="room1",
            msgtime=1000,
            msgtype="image",
            sdkfileid="sdk-should-never-appear",
        )
    ]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    body_text = resp.text
    assert "sdk-should-never-appear" not in body_text
    msg = resp.json()["messages"][0]
    for forbidden_field in (
        "sdkfileid",
        "local_path",
        "oss_key",
        "raw_encrypted_payload",
        "decrypted_payload",
    ):
        assert forbidden_field not in msg
