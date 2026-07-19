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


@pytest.mark.parametrize(
    "msgtype", ["link", "location", "markdown", "news", "weapp", "card", "docmsg"]
)
def test_classify_media_structured_types_are_not_byte_bearing(msgtype) -> None:
    """RND-197: link/location/markdown/news/weapp (SUPPORTED) and
    card/docmsg (PARTIAL) are structured-field types — they must not run
    the byte-bearing not_downloaded/unknown reasoning built for
    image/video/voice/file. The frontend dispatches these by
    TimelineMessageOut.renderer_strategy, not media_type."""
    from app.media_classification import classify_media

    result = classify_media(msgtype, has_sdkfileid=False)
    assert result == ("structured", None, None)


def test_classify_media_audio_doc_stays_byte_bearing_like_audio_archive() -> None:
    """audio_doc is category MEDIA (unlike the other RND-197 types) and
    must keep the not_downloaded/unknown byte-bearing reasoning, distinct
    from audio_archive (never merged — RND-202 scope)."""
    from app.media_classification import classify_media

    result = classify_media("audio_doc", has_sdkfileid=True)
    assert result.media_type == "audio_doc"
    assert result.media_status == "not_downloaded"


# ---------------------------------------------------------------------------
# /api/conversations/{id}/messages — media fields + no raw identifier leaks
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _run_messages_query(client, app, all_msgs, conversation_id="room1"):
    from app.auth import get_current_user
    from app.db.models import ArchiveMessageRecipient, Contact, MediaFile, MessageRevocation
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

        media_q = MagicMock()
        media_q.filter.return_value = media_q
        media_q.all.return_value = []

        revocation_q = MagicMock()
        revocation_q.filter.return_value = revocation_q
        revocation_q.all.return_value = []

        def _query(model):
            if model is ArchiveMessageRecipient:
                return rcpt_q
            if model is Contact:
                return contact_q
            if model is MediaFile:
                return media_q
            if model is MessageRevocation:
                return revocation_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    try:
        return client.get(f"/api/conversations/{conversation_id}/messages")
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


def test_timeline_direct_voice_empty_text_still_serialized(client, monkeypatch) -> None:
    import app.routers.conversations as conv

    from app.main import app

    msg = _msg(
        1,
        "staff_a",
        roomid=None,
        msgtime=1000,
        content_text=None,
        msgtype="voice",
        sdkfileid="redacted-media-id",
    )
    monkeypatch.setattr(
        conv, "_fetch_conversation_messages", lambda db, cid, tenant_id, **kwargs: [msg]
    )
    resp = _run_messages_query(client, app, [], conversation_id="direct__contact_a___staff_a")
    assert resp.status_code == 200
    data = resp.json()["messages"]
    assert len(data) == 1
    assert data[0]["msgid"] == "m-1"
    assert data[0]["content_text"] is None
    assert data[0]["msgtype"] == "voice"
    assert data[0]["media_type"] == "voice"
    assert data[0]["media_status"] == "not_downloaded"
    assert data[0]["sender"]
    assert data[0]["msgtime"] == 1000
    assert data[0]["roomid"] is None


def test_timeline_group_voice_empty_text_still_serialized(client) -> None:
    from app.main import app

    msg = _msg(
        1,
        "staff_a",
        roomid="room1",
        msgtime=1000,
        content_text=None,
        msgtype="voice",
        sdkfileid="redacted-media-id",
    )
    resp = _run_messages_query(client, app, [msg])
    assert resp.status_code == 200
    data = resp.json()["messages"]
    assert len(data) == 1
    assert data[0]["msgid"] == "m-1"
    assert data[0]["content_text"] is None
    assert data[0]["msgtype"] == "voice"
    assert data[0]["media_type"] == "voice"
    assert data[0]["media_status"] == "not_downloaded"
    assert data[0]["sender"]
    assert data[0]["msgtime"] == 1000
    assert data[0]["roomid"] == "room1"


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


def test_timeline_exposes_registry_metadata_and_structured_content_fields(client) -> None:
    """RND-197: TimelineMessageOut must carry the Message Type Registry
    metadata plus the parsed structured_content.fields — not the raw
    sub-payload (security requirement, see the "raw" exclusion test
    below)."""
    from app.main import app

    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="link", content_text=None)
    msg.structured_content = {
        "fields": {"title": "Example", "url": "https://example.com", "description": None, "image_url": None},
        "raw": {"title": "Example", "link_url": "https://example.com"},
        "parse_warnings": [],
    }
    resp = _run_messages_query(client, app, [msg])
    assert resp.status_code == 200
    out = resp.json()["messages"][0]
    assert out["normalized_type"] == "link"
    assert out["category"] == "structured"
    assert out["support_status"] == "supported"
    assert out["renderer_strategy"] == "structured_card"
    assert out["display_label_key"] == "messageType.link"
    assert out["structured_content"]["fields"]["title"] == "Example"
    assert out["structured_content"]["parse_warnings"] == []
    assert "raw" not in out["structured_content"]


def test_timeline_structured_content_null_for_historical_rows_without_it(client) -> None:
    from app.main import app

    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="link")
    # No structured_content attribute set at all — mirrors a historical
    # row from before this migration existed.
    resp = _run_messages_query(client, app, [msg])
    assert resp.status_code == 200
    out = resp.json()["messages"][0]
    assert out["structured_content"] is None
    assert out["normalized_type"] == "link"


def test_timeline_never_exposes_structured_content_raw_sub_payload(client) -> None:
    """Security requirement: the type-specific raw sub-payload preserved
    server-side must never reach the API response."""
    from app.main import app

    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="location")
    msg.structured_content = {
        "fields": {"name": "Office", "address": None, "latitude": 1.0, "longitude": 2.0, "zoom": None},
        "raw": {"latitude": 1.0, "longitude": 2.0, "title": "Office", "some_internal_field": "secret-raw-value"},
        "parse_warnings": [],
    }
    resp = _run_messages_query(client, app, [msg])
    assert resp.status_code == 200
    assert "secret-raw-value" not in resp.text
    assert "raw" not in resp.json()["messages"][0]["structured_content"]


def test_timeline_docmsg_and_audio_doc_report_partial_structured_card() -> None:
    from app.message_type_registry import describe_message_type

    for msgtype in ("docmsg", "audio_doc", "card"):
        meta = describe_message_type(msgtype)
        assert meta["support_status"] == "partial"
        assert meta["renderer_strategy"] == "structured_card"


def test_timeline_voice_never_exposes_raw_media_identifiers(client) -> None:
    from app.main import app

    all_msgs = [
        _msg(
            1,
            "staff_a",
            roomid="room1",
            msgtime=1000,
            msgtype="voice",
            sdkfileid="redacted-media-id",
        )
    ]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    body_text = resp.text
    assert "redacted-media-id" not in body_text
    msg = resp.json()["messages"][0]
    for forbidden_field in (
        "sdkfileid",
        "local_path",
        "oss_key",
        "raw_payload",
        "raw_encrypted_payload",
        "decrypted_payload",
    ):
        assert forbidden_field not in msg
