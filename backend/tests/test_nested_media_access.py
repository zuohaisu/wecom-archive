"""
Tests for the RND-200 QA fix — the public nested-media contract for
mixed/chatrecord messages.

Prior state (FAIL): a mixed/chatrecord structured_content node with a
media reference only ever exposed {"has_reference": true} — enough to
know a nested item HAS downloadable media, but with no way for an API
consumer to determine its status, type, or fetch it. This file covers
the fix: app.routers.conversations._build_nested_media_descriptor /
_enrich_nested_media_fields / _validate_nested_media_path /
_find_nested_media_ref / _resolve_authorized_nested_media, and the two
new routes GET .../nested-media/{item_path} and
GET .../nested-media/{item_path}/access.

Sections (matching the QA fix ticket's own test matrix):
  A. API serialization — every node shape (mixed/chatrecord, every
     nested media type, sibling media, multi-level nesting) exposes the
     right path/media_type/status/access_url and leaks no internal value.
  B. Nested media access endpoint — every status, every rejection class
     (malformed path, nonexistent path, non-media node, wrong
     conversation/message, unauthenticated, cross-tenant), sibling/deep
     path resolution.
  C. Storage backend parity — local proxy and Qiniu signed_url, reusing
     the exact RND-199 primitives (no second implementation).
  D. Regression — non-nested (image/voice/video/file/emotion) messages
     and the existing /media, /media/access routes are unaffected.
  E. API compatibility contract — the intentional media_type="structured"
     classification and the intentional per-node "media" shape change
     are both explicitly pinned down by a test, not left implicit.

Run (from backend/):
    pytest tests/test_nested_media_access.py -v
"""

from __future__ import annotations

import json

import pytest

from app.media_storage import MediaStorageUnavailable
from tests.test_media_access_descriptor import (
    _FAKE_SIGNED_URL,
    _authed,
    _patch_cloud_provider,
    client,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)
from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)
from tests.test_tenant_media_access import _insert_media_file

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _node(path, item_type, media=None, children=None, **extra) -> dict:
    base = {
        "path": path,
        "type": item_type,
        "supported": True,
        "text": None,
        "fields": None,
        "media": media,
        "sender": None,
        "sender_name": None,
        "timestamp": None,
        "children": children,
    }
    base.update(extra)
    return base


def _structured_content(items: list, media_refs: list, title=None) -> dict:
    fields = {"items": items, "item_count": len(items)}
    if title is not None:
        fields = {"title": title, **fields}
    return {"fields": fields, "raw": {}, "parse_warnings": [], "media_refs": media_refs}


def _nested_access_url(conversation_id: str, msgid: str, item_path: str) -> str:
    return f"/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}/access"


def _nested_bytes_url(conversation_id: str, msgid: str, item_path: str) -> str:
    return f"/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}"


_FORBIDDEN_SUBSTRINGS_TEMPLATE = (
    "local_path",
    "storage_ref",
    "oss_key",
    "checksum_sha256",
)


def _assert_no_internal_leak(payload_text: str, *, sdkfileid: str, local_path: str = None) -> None:
    assert sdkfileid not in payload_text
    if local_path:
        assert local_path not in payload_text
    for marker in _FORBIDDEN_SUBSTRINGS_TEMPLATE:
        assert marker not in payload_text


# ---------------------------------------------------------------------------
# Unit tests — _validate_nested_media_path (path grammar / traversal safety)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["0", "0.1", "12.3", "0.0.0.0.0.0.0.0", "199"],
)
def test_validate_nested_media_path_accepts_well_formed_paths(raw) -> None:
    from app.routers.conversations import _validate_nested_media_path

    assert _validate_nested_media_path(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "0.abc",
        "-1",
        "0.-1",
        "0/1",
        "../../etc/passwd",
        "0/../1",
        "0..1",
        ".0",
        "0.",
        "0 1",
        "0\n1",
        "0\x001",
        "0" * 200,
        "0." * 20 + "0",  # too many segments
        None,
        123,
        ["0", "1"],
    ],
)
def test_validate_nested_media_path_rejects_malformed_paths(raw) -> None:
    from app.routers.conversations import _validate_nested_media_path

    assert _validate_nested_media_path(raw) is None


def test_validate_nested_media_path_rejects_too_many_segments() -> None:
    from app.routers.conversations import (
        _NESTED_MEDIA_MAX_PATH_SEGMENTS,
        _validate_nested_media_path,
    )

    ok = ".".join(str(i) for i in range(_NESTED_MEDIA_MAX_PATH_SEGMENTS))
    too_many = ok + ".0"
    assert _validate_nested_media_path(ok) == ok
    assert _validate_nested_media_path(too_many) is None


def test_nested_media_path_segment_cap_matches_parser_max_depth() -> None:
    """Regression guard for the documented coupling between this cap and
    app.structured_message_parser._MIXED_MAX_DEPTH (no import between the
    two modules is introduced to avoid a new dependency — this test is
    the safety net instead)."""
    from app.routers.conversations import _NESTED_MEDIA_MAX_PATH_SEGMENTS
    from app.structured_message_parser import _MIXED_MAX_DEPTH

    assert _NESTED_MEDIA_MAX_PATH_SEGMENTS == _MIXED_MAX_DEPTH + 1


def test_validate_nested_media_path_never_raises_on_garbage_input() -> None:
    from app.routers.conversations import _validate_nested_media_path

    for garbage in (object(), {}, b"0.1", 3.14, True, False):
        assert _validate_nested_media_path(garbage) is None


# ---------------------------------------------------------------------------
# Unit tests — _find_nested_media_ref
# ---------------------------------------------------------------------------


def test_find_nested_media_ref_resolves_exact_path() -> None:
    from app.routers.conversations import _find_nested_media_ref

    structured_content = _structured_content(
        [_node("0", "image", media={"has_reference": True})],
        [{"path": "0", "type": "image", "sdkfileid": "sdk-1"}],
    )
    ref = _find_nested_media_ref(structured_content, "0")
    assert ref == {"path": "0", "type": "image", "sdkfileid": "sdk-1"}


def test_find_nested_media_ref_returns_none_for_missing_path() -> None:
    from app.routers.conversations import _find_nested_media_ref

    structured_content = _structured_content(
        [_node("0", "image", media={"has_reference": True})],
        [{"path": "0", "type": "image", "sdkfileid": "sdk-1"}],
    )
    assert _find_nested_media_ref(structured_content, "5") is None


def test_find_nested_media_ref_none_for_missing_or_malformed_structured_content() -> None:
    from app.routers.conversations import _find_nested_media_ref

    assert _find_nested_media_ref(None, "0") is None
    assert _find_nested_media_ref({}, "0") is None
    assert _find_nested_media_ref("garbage", "0") is None


def test_find_nested_media_ref_resolution_is_structurally_unique() -> None:
    """Every media_refs path is unique by construction (see
    app.structured_message_parser._parse_nested_item) -- confirmed here
    rather than merely assumed: a hand-built duplicate-path list (which
    the real parser could never produce) must not silently return the
    wrong entry; the first exact match is deterministic."""
    from app.routers.conversations import _find_nested_media_ref

    structured_content = {
        "media_refs": [
            {"path": "0", "type": "image", "sdkfileid": "sdk-first"},
            {"path": "0", "type": "file", "sdkfileid": "sdk-second"},
        ]
    }
    ref = _find_nested_media_ref(structured_content, "0")
    assert ref["sdkfileid"] == "sdk-first"


# ---------------------------------------------------------------------------
# Unit tests — _build_nested_media_descriptor (status/mime/size/access_url)
# ---------------------------------------------------------------------------


def test_descriptor_downloaded_and_servable_is_available(tmp_path, monkeypatch) -> None:
    from app.db.models import MediaFile
    from app.routers.conversations import _build_nested_media_descriptor

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"jpeg")
    media_file = MediaFile(
        sdkfileid="sdk-1", archive_message_id=1, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(img),
        local_path=str(img), file_size=8,
    )
    descriptor = _build_nested_media_descriptor("image", media_file, "conv-1", "msg-1", "0")
    assert descriptor["status"] == "available"
    assert descriptor["media_type"] == "image"
    assert descriptor["mime_type"] == "image/jpeg"
    assert descriptor["size_bytes"] == 8
    assert descriptor["access_url"] == "/api/conversations/conv-1/messages/msg-1/nested-media/0/access"


def test_descriptor_no_media_file_row_is_not_downloaded() -> None:
    from app.routers.conversations import _build_nested_media_descriptor

    descriptor = _build_nested_media_descriptor("image", None, "conv-1", "msg-1", "0")
    assert descriptor == {
        "status": "not_downloaded",
        "media_type": "image",
        "mime_type": None,
        "size_bytes": None,
        "access_url": None,
        # RND-207: thumbnail fields are always present in the shape, null
        # when there is no media_file / no generated thumbnail.
        "thumbnail_access_url": None,
        "image_width": None,
        "image_height": None,
    }


def test_descriptor_pending_media_file_is_not_downloaded() -> None:
    from app.db.models import MediaFile
    from app.routers.conversations import _build_nested_media_descriptor

    media_file = MediaFile(
        sdkfileid="sdk-1", archive_message_id=1, tenant_id=_TENANT_A, download_status="pending"
    )
    descriptor = _build_nested_media_descriptor("image", media_file, "conv-1", "msg-1", "0")
    assert descriptor["status"] == "not_downloaded"
    assert descriptor["access_url"] is None


def test_descriptor_failed_download_status_is_failed() -> None:
    from app.db.models import MediaFile
    from app.routers.conversations import _build_nested_media_descriptor

    media_file = MediaFile(
        sdkfileid="sdk-1", archive_message_id=1, tenant_id=_TENANT_A, download_status="failed"
    )
    descriptor = _build_nested_media_descriptor("voice", media_file, "conv-1", "msg-1", "0")
    assert descriptor["status"] == "failed"
    assert descriptor["access_url"] is None


def test_descriptor_downloaded_but_file_missing_on_disk_is_failed(tmp_path, monkeypatch) -> None:
    from app.db.models import MediaFile
    from app.routers.conversations import _build_nested_media_descriptor

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    missing_path = tmp_path / "gone.jpg"
    media_file = MediaFile(
        sdkfileid="sdk-1", archive_message_id=1, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(missing_path),
        local_path=str(missing_path), file_size=8,
    )
    descriptor = _build_nested_media_descriptor("image", media_file, "conv-1", "msg-1", "0")
    assert descriptor["status"] == "failed"
    assert descriptor["access_url"] is None


def test_descriptor_provider_outage_is_unavailable(monkeypatch) -> None:
    from app.db.models import MediaFile
    import app.services.timeline_service as timeline_service
    from app.routers.conversations import _build_nested_media_descriptor

    def _boom(_media_file):
        raise MediaStorageUnavailable("simulated outage")

    # RND-220: _build_nested_media_descriptor's implementation (and its own
    # call to resolve_downloadable_media_state) now lives in
    # app.services.timeline_service, not app.routers.conversations (which
    # only re-exports the function object) -- patch the name where it is
    # actually looked up at call time.
    monkeypatch.setattr(timeline_service, "resolve_downloadable_media_state", _boom)
    media_file = MediaFile(
        sdkfileid="sdk-1", archive_message_id=1, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="qiniu_kodo",
        storage_ref="tenants/tenant-a/images/1.jpg",
    )
    descriptor = _build_nested_media_descriptor("image", media_file, "conv-1", "msg-1", "0")
    assert descriptor["status"] == "unavailable"
    assert descriptor["access_url"] is None


def test_descriptor_never_includes_internal_identifiers() -> None:
    """Every key in a descriptor must be one of the five documented,
    safe fields -- no sdkfileid, no media_file.id, no storage path."""
    from app.db.models import MediaFile
    from app.routers.conversations import _build_nested_media_descriptor

    media_file = MediaFile(
        id=42, sdkfileid="sdk-super-secret", archive_message_id=1, tenant_id=_TENANT_A,
        download_status="pending",
    )
    descriptor = _build_nested_media_descriptor("image", media_file, "conv-1", "msg-1", "0")
    assert set(descriptor.keys()) == {
        "status", "media_type", "mime_type", "size_bytes", "access_url",
        "thumbnail_access_url", "image_width", "image_height",
    }
    assert "sdk-super-secret" not in json.dumps(descriptor)
    assert "42" not in json.dumps(descriptor)


# ---------------------------------------------------------------------------
# Unit tests — _enrich_nested_media_fields (deep-copy safety, hierarchy)
# ---------------------------------------------------------------------------


def test_enrich_nested_media_fields_never_mutates_input() -> None:
    from app.routers.conversations import _enrich_nested_media_fields

    original_media = {"has_reference": True}
    node = _node("0", "image", media=original_media)
    fields = {"items": [node], "item_count": 1}
    media_refs = [{"path": "0", "type": "image", "sdkfileid": "sdk-1"}]

    result = _enrich_nested_media_fields(fields, media_refs, {}, "conv-1", "msg-1")

    assert fields["items"][0]["media"] is original_media  # untouched
    assert original_media == {"has_reference": True}
    assert result["items"][0]["media"] != original_media
    assert result["items"][0]["media"]["status"] == "not_downloaded"


def test_enrich_nested_media_fields_preserves_order_and_hierarchy() -> None:
    from app.routers.conversations import _enrich_nested_media_fields

    child = _node("0.0", "image", media={"has_reference": True})
    parent = _node("0", "mixed", children=[child])
    sibling = _node("1", "text")
    fields = {"items": [parent, sibling], "item_count": 2}
    media_refs = [{"path": "0.0", "type": "image", "sdkfileid": "sdk-1"}]

    result = _enrich_nested_media_fields(fields, media_refs, {}, "conv-1", "msg-1")

    assert [i["path"] for i in result["items"]] == ["0", "1"]
    assert result["items"][0]["children"][0]["path"] == "0.0"
    assert result["items"][0]["children"][0]["media"]["status"] == "not_downloaded"
    assert result["items"][1]["type"] == "text"


def test_enrich_nested_media_fields_leaves_non_media_nodes_untouched() -> None:
    from app.routers.conversations import _enrich_nested_media_fields

    node = _node("0", "text")
    fields = {"items": [node], "item_count": 1}
    result = _enrich_nested_media_fields(fields, [], {}, "conv-1", "msg-1")
    assert result["items"][0]["media"] is None


# ---------------------------------------------------------------------------
# Section A — API serialization (full route, every ticket-named nesting case)
# ---------------------------------------------------------------------------


def _setup_downloaded_media(db, tmp_path, sdkfileid: str, suffix: str, body: bytes, tenant_id=_TENANT_A):
    path = tmp_path / f"{sdkfileid}{suffix}"
    path.write_bytes(body)
    _insert_media_file(
        db, archive_message_id=0, tenant_id=tenant_id, sdkfileid=sdkfileid,
        download_status="downloaded", local_path=str(path), storage_backend="local",
        storage_ref=str(path),
    )
    return path


@pytest.mark.parametrize(
    "media_type,suffix,body",
    [
        ("image", ".jpg", b"\xff\xd8\xff" + b"jpeg"),
        ("video", ".mp4", b"\x00\x00\x00\x18ftypmp42" + b"video"),
        ("voice", ".amr", b"#!AMR" + b"voice"),
        ("file", ".pdf", b"%PDF-1.4" + b"file"),
    ],
)
def test_mixed_message_with_each_media_type_serializes_correctly(
    client, db, monkeypatch, tmp_path, media_type, suffix, body
) -> None:
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    sdkfileid = f"sdk-{media_type}-1"
    media_path = tmp_path / f"item{suffix}"
    media_path.write_bytes(body)

    structured_content = _structured_content(
        [_node("0", "text", **{}), _node("1", media_type, media={"has_reference": True})],
        [{"path": "1", "type": media_type, "sdkfileid": sdkfileid}],
    )
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-a", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(
        MediaFile(
            sdkfileid=sdkfileid, archive_message_id=msg.id, tenant_id=_TENANT_A,
            download_status="downloaded", storage_backend="local",
            storage_ref=str(media_path), local_path=str(media_path), file_size=len(body),
            file_type=media_type,
        )
    )
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-a/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    items = resp.json()["messages"][0]["structured_content"]["fields"]["items"]
    node = items[1]
    assert node["path"] == "1"
    assert node["type"] == media_type
    assert node["media"]["status"] == "available"
    assert node["media"]["media_type"] == media_type
    assert node["media"]["access_url"] == f"/api/conversations/room-a/messages/{msg.msgid}/nested-media/1/access"
    _assert_no_internal_leak(json.dumps(resp.json()), sdkfileid=sdkfileid, local_path=str(media_path))


def test_chatrecord_message_with_media_serializes_correctly(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    voice_path = tmp_path / "voice.amr"
    voice_path.write_bytes(b"#!AMR" + b"voice-body")

    structured_content = _structured_content(
        [_node("0", "voice", media={"has_reference": True})],
        [{"path": "0", "type": "voice", "sdkfileid": "sdk-cr-voice"}],
        title="forwarded chat",
    )
    msg = _insert_message(
        db, msgtype="chatrecord", roomid="room-b", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(
        MediaFile(
            sdkfileid="sdk-cr-voice", archive_message_id=msg.id, tenant_id=_TENANT_A,
            download_status="downloaded", storage_backend="local",
            storage_ref=str(voice_path), local_path=str(voice_path), file_size=14,
            file_type="voice",
        )
    )
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-b/messages")
    finally:
        app.dependency_overrides.clear()

    body = resp.json()["messages"][0]["structured_content"]
    assert body["fields"]["title"] == "forwarded chat"
    assert body["fields"]["items"][0]["media"]["status"] == "available"


def test_chatrecord_nested_mixed_media_serializes_correctly(client, db, monkeypatch, tmp_path) -> None:
    """chatrecord -> mixed -> image, per the ticket's named nesting case."""
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "nested.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"nested-jpeg")

    child = _node("0.0", "image", media={"has_reference": True})
    parent = _node("0", "mixed", children=[child])
    structured_content = _structured_content(
        [parent], [{"path": "0.0", "type": "image", "sdkfileid": "sdk-deep-1"}], title="digest"
    )
    msg = _insert_message(
        db, msgtype="chatrecord", roomid="room-c", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(
        MediaFile(
            sdkfileid="sdk-deep-1", archive_message_id=msg.id, tenant_id=_TENANT_A,
            download_status="downloaded", storage_backend="local",
            storage_ref=str(img_path), local_path=str(img_path), file_size=15, file_type="image",
        )
    )
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-c/messages")
    finally:
        app.dependency_overrides.clear()

    outer = resp.json()["messages"][0]["structured_content"]["fields"]["items"][0]
    assert outer["type"] == "mixed"
    child_out = outer["children"][0]
    assert child_out["path"] == "0.0"
    assert child_out["media"]["status"] == "available"
    assert child_out["media"]["access_url"].endswith("/nested-media/0.0/access")


def test_mixed_nested_chatrecord_media_serializes_correctly(client, db, monkeypatch, tmp_path) -> None:
    """mixed -> chatrecord -> file, per the ticket's named nesting case."""
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    file_path = tmp_path / "doc.pdf"
    file_path.write_bytes(b"%PDF-1.4" + b"nested-doc")

    child = _node("0.0", "file", media={"has_reference": True})
    parent = _node("0", "chatrecord", children=[child], fields={"title": "inner digest"})
    structured_content = _structured_content(
        [parent], [{"path": "0.0", "type": "file", "sdkfileid": "sdk-deep-2"}]
    )
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-d", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(
        MediaFile(
            sdkfileid="sdk-deep-2", archive_message_id=msg.id, tenant_id=_TENANT_A,
            download_status="downloaded", storage_backend="local",
            storage_ref=str(file_path), local_path=str(file_path), file_size=18, file_type="file",
        )
    )
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-d/messages")
    finally:
        app.dependency_overrides.clear()

    outer = resp.json()["messages"][0]["structured_content"]["fields"]["items"][0]
    assert outer["type"] == "chatrecord"
    assert outer["fields"]["title"] == "inner digest"
    assert outer["children"][0]["media"]["status"] == "available"


def test_multiple_sibling_media_resolve_independently(client, db, monkeypatch, tmp_path) -> None:
    """Same parent message, several sibling media items -- each node must
    carry its own correct media descriptor, never cross-wired."""
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "a.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"a")
    voice_path = tmp_path / "b.amr"
    voice_path.write_bytes(b"#!AMR" + b"b")

    items = [
        _node("0", "image", media={"has_reference": True}),
        _node("1", "text"),
        _node("2", "voice", media={"has_reference": True}),
    ]
    structured_content = _structured_content(
        items,
        [
            {"path": "0", "type": "image", "sdkfileid": "sdk-sib-a"},
            {"path": "2", "type": "voice", "sdkfileid": "sdk-sib-b"},
        ],
    )
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-e", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(MediaFile(
        sdkfileid="sdk-sib-a", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(img_path),
        local_path=str(img_path), file_size=9, file_type="image",
    ))
    db.add(MediaFile(
        sdkfileid="sdk-sib-b", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="failed", file_type="voice",
    ))
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-e/messages")
    finally:
        app.dependency_overrides.clear()

    out_items = resp.json()["messages"][0]["structured_content"]["fields"]["items"]
    assert out_items[0]["media"]["status"] == "available"
    assert out_items[0]["media"]["access_url"].endswith("/nested-media/0/access")
    assert out_items[1]["media"] is None
    assert out_items[2]["media"]["status"] == "failed"
    assert out_items[2]["media"]["access_url"] is None


def test_deep_multi_level_recursive_media_resolves_at_every_level(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "deep.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"deep")

    leaf = _node("0.0.0", "image", media={"has_reference": True})
    mid = _node("0.0", "mixed", children=[leaf])
    top = _node("0", "mixed", children=[mid])
    structured_content = _structured_content(
        [top], [{"path": "0.0.0", "type": "image", "sdkfileid": "sdk-triple-deep"}]
    )
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-f", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(MediaFile(
        sdkfileid="sdk-triple-deep", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(img_path),
        local_path=str(img_path), file_size=8, file_type="image",
    ))
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-f/messages")
    finally:
        app.dependency_overrides.clear()

    top_out = resp.json()["messages"][0]["structured_content"]["fields"]["items"][0]
    leaf_out = top_out["children"][0]["children"][0]
    assert leaf_out["path"] == "0.0.0"
    assert leaf_out["media"]["status"] == "available"
    assert leaf_out["media"]["access_url"].endswith("/nested-media/0.0.0/access")


# ---------------------------------------------------------------------------
# Section B — nested media access endpoint
# ---------------------------------------------------------------------------


def _insert_mixed_with_media(db, roomid, sdkfileid, tenant_id=_TENANT_A, msgid=None):
    structured_content = _structured_content(
        [_node("0", "image", media={"has_reference": True})],
        [{"path": "0", "type": "image", "sdkfileid": sdkfileid}],
    )
    kwargs = dict(msgtype="mixed", roomid=roomid, msgtime=100, structured_content=structured_content, tenant_id=tenant_id)
    if msgid is not None:
        kwargs["msgid"] = msgid
    return _insert_message(db, **kwargs)


def test_access_route_downloaded_media_succeeds(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "ok.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"ok")
    msg = _insert_mixed_with_media(db, "room-g", "sdk-ok")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-ok", download_status="downloaded",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-g", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json()["access_type"] == "proxy"


def test_access_route_pending_media_returns_404(client, db) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-h", "sdk-pending")
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-pending", download_status="pending")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-h", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_access_route_failed_media_returns_404(client, db) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-i", "sdk-failed")
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-failed", download_status="failed")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-i", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_access_route_provider_outage_returns_503(client, db, monkeypatch) -> None:
    from app.main import app
    import app.routers.conversations as conv

    msg = _insert_mixed_with_media(db, "room-j", "sdk-outage")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-outage", download_status="downloaded",
        storage_backend="qiniu_kodo", storage_ref="tenants/tenant-a/images/1.jpg", local_path=None,
    )

    def _boom(*_a, **_k):
        raise MediaStorageUnavailable("simulated")

    monkeypatch.setattr(conv, "resolve_downloadable_media_file_state", _boom)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-j", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503


def test_access_route_nonexistent_node_path_returns_404(client, db) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-k", "sdk-k")
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-k", download_status="downloaded")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-k", msg.msgid, "99"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_access_route_malformed_path_returns_400(client, db) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-l", "sdk-l")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-l", msg.msgid, "not-a-path"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400


def test_access_route_non_media_node_returns_404(client, db) -> None:
    """path "1" exists (a text node) but has no media reference."""
    from app.main import app

    structured_content = _structured_content(
        [_node("0", "image", media={"has_reference": True}), _node("1", "text")],
        [{"path": "0", "type": "image", "sdkfileid": "sdk-m"}],
    )
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-m", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-m", download_status="downloaded")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-m", msg.msgid, "1"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_access_route_wrong_conversation_returns_404(client, db) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-n", "sdk-n")
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-n", download_status="downloaded")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("some-other-room", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_access_route_wrong_message_id_returns_404(client, db) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-o", "sdk-o")
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-o", download_status="downloaded")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-o", "totally-wrong-msgid", "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_access_route_unauthenticated_request_is_rejected(client, db) -> None:
    from app.main import app
    from app.db.session import get_db

    msg = _insert_mixed_with_media(db, "room-p", "sdk-p")
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-p", download_status="downloaded")

    def _db_gen():
        yield db

    app.dependency_overrides[get_db] = _db_gen
    try:
        resp = client.get(_nested_access_url("room-p", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code in (401, 403)


def test_access_route_cross_tenant_access_returns_404(client, db) -> None:
    """tenant-b authenticated, requesting tenant-a's conversation."""
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-q", "sdk-q", tenant_id=_TENANT_A)
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-q", download_status="downloaded")

    _authed(app, db, _TENANT_B)
    try:
        resp = client.get(_nested_access_url("room-q", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_access_route_same_sdkfileid_different_tenant_does_not_leak(client, db) -> None:
    """Two tenants' rows can coincidentally share an sdkfileid string
    (MediaFile uniqueness is scoped per-tenant) -- tenant-b must never
    resolve to tenant-a's row."""
    from app.main import app

    msg_a = _insert_mixed_with_media(db, "room-r-a", "sdk-shared-across-tenants", tenant_id=_TENANT_A)
    _insert_media_file(db, msg_a.id, _TENANT_A, "sdk-shared-across-tenants", download_status="downloaded")
    msg_b = _insert_mixed_with_media(db, "room-r-b", "sdk-shared-across-tenants", tenant_id=_TENANT_B)
    # tenant-b has NO media_files row for this sdkfileid.

    _authed(app, db, _TENANT_B)
    try:
        resp = client.get(_nested_access_url("room-r-b", msg_b.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_access_route_cross_message_shared_sdkfileid_resolves(client, db, monkeypatch, tmp_path) -> None:
    """The design's core claim (see the RND-200 QA fix report's
    association-model section): a MediaFile row owned by one message is
    still readable through a DIFFERENT message's legitimate reference to
    the same sdkfileid, within the same tenant -- no schema change
    needed."""
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "shared.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"shared")

    msg_owner = _insert_mixed_with_media(db, "room-s-owner", "sdk-cross-msg")
    db.add(MediaFile(
        sdkfileid="sdk-cross-msg", archive_message_id=msg_owner.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(img_path),
        local_path=str(img_path), file_size=9, file_type="image",
    ))
    db.commit()

    msg_referrer = _insert_mixed_with_media(db, "room-s-referrer", "sdk-cross-msg")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-s-referrer", msg_referrer.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200


def test_access_route_sibling_paths_resolve_to_different_media(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    a_path = tmp_path / "a.jpg"
    a_path.write_bytes(b"\xff\xd8\xff" + b"a")
    b_path = tmp_path / "b.jpg"
    b_path.write_bytes(b"\xff\xd8\xff" + b"b")

    structured_content = _structured_content(
        [
            _node("0", "image", media={"has_reference": True}),
            _node("1", "image", media={"has_reference": True}),
        ],
        [
            {"path": "0", "type": "image", "sdkfileid": "sdk-t-a"},
            {"path": "1", "type": "image", "sdkfileid": "sdk-t-b"},
        ],
    )
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-t", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(MediaFile(
        sdkfileid="sdk-t-a", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(a_path),
        local_path=str(a_path), file_size=6, file_type="image",
    ))
    db.add(MediaFile(
        sdkfileid="sdk-t-b", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(b_path),
        local_path=str(b_path), file_size=6, file_type="image",
    ))
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp_a = client.get(_nested_bytes_url("room-t", msg.msgid, "0"))
        resp_b = client.get(_nested_bytes_url("room-t", msg.msgid, "1"))
    finally:
        app.dependency_overrides.clear()

    assert resp_a.content == b"\xff\xd8\xff" + b"a"
    assert resp_b.content == b"\xff\xd8\xff" + b"b"


def test_access_route_deep_legal_path_resolves(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "deep.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"deep")

    deep_path = "0.1.2.0.1"
    structured_content = {
        "fields": {"items": []}, "raw": {}, "parse_warnings": [],
        "media_refs": [{"path": deep_path, "type": "image", "sdkfileid": "sdk-deep-path"}],
    }
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-u", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(MediaFile(
        sdkfileid="sdk-deep-path", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(img_path),
        local_path=str(img_path), file_size=8, file_type="image",
    ))
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-u", msg.msgid, deep_path))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200


def test_access_route_overly_long_path_returns_400(client, db) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-v", "sdk-v")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-v", msg.msgid, "0" * 100))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "attempt",
    [
        "..%2F..%2Fetc%2Fpasswd",
        "0%2F..%2F..%2F1",
        "..",
        "%2e%2e",
    ],
)
def test_access_route_path_traversal_attempts_are_rejected(client, db, attempt) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-w", "sdk-w")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/room-w/messages/{msg.msgid}/nested-media/{attempt}/access")
    finally:
        app.dependency_overrides.clear()
    # Either FastAPI's own router rejects the malformed path (404, never
    # matching the route) or _validate_nested_media_path rejects it
    # (400) -- both are acceptable "rejected", never a 200 and never any
    # file content.
    assert resp.status_code in (400, 404)
    assert "passwd" not in resp.text


def test_bytes_route_never_leaks_internal_identifiers(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "leak-check.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"leakcheck")
    msg = _insert_mixed_with_media(db, "room-x", "sdk-leak-check")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-leak-check", download_status="downloaded",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )

    _authed(app, db, _TENANT_A)
    try:
        access_resp = client.get(_nested_access_url("room-x", msg.msgid, "0"))
        bytes_resp = client.get(_nested_bytes_url("room-x", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()

    combined = json.dumps(access_resp.json()) + str(bytes_resp.headers)
    assert "sdk-leak-check" not in combined
    assert str(img_path) not in combined


# ---------------------------------------------------------------------------
# Section C — storage backend parity (local proxy + Qiniu signed_url)
# ---------------------------------------------------------------------------


def test_local_backend_returns_proxy_access_type(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "local.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"local")
    msg = _insert_mixed_with_media(db, "room-y", "sdk-local-backend")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-local-backend", download_status="downloaded",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-y", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()

    body = resp.json()
    assert body["access_type"] == "proxy"
    assert body["storage_backend"] == "local"
    assert body["url"] == _nested_bytes_url("room-y", msg.msgid, "0")


def test_qiniu_backend_returns_signed_url_access_type(client, db, monkeypatch) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-z", "sdk-qiniu-backend")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-qiniu-backend", download_status="downloaded",
        storage_backend="qiniu_kodo", storage_ref="tenants/tenant-a/images/1.jpg", local_path=None,
    )
    _patch_cloud_provider(monkeypatch, {"tenants/tenant-a/images/1.jpg": b"fake"})

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-z", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()

    body = resp.json()
    assert resp.status_code == 200
    assert body["access_type"] == "signed_url"
    assert body["storage_backend"] == "qiniu_kodo"
    # The signed URL legitimately embeds the object key in its path (that
    # is what makes it a *signed* URL for that object) -- this is not an
    # internal-identifier leak, unlike sdkfileid/local_path/raw oss_key,
    # none of which ever appear anywhere in this response.
    assert body["url"] == _FAKE_SIGNED_URL
    assert "sdk-qiniu-backend" not in json.dumps(body)


def test_qiniu_backend_object_key_tenant_mismatch_returns_404(client, db, monkeypatch) -> None:
    """object_key_tenant_prefix_matches defense-in-depth, reused
    unchanged for the nested route."""
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-aa", "sdk-qiniu-mismatch")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-qiniu-mismatch", download_status="downloaded",
        storage_backend="qiniu_kodo", storage_ref="tenants/tenant-b/images/1.jpg", local_path=None,
    )
    _patch_cloud_provider(monkeypatch, {"tenants/tenant-b/images/1.jpg": b"fake"})

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-aa", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_qiniu_backend_bytes_route_proxies_through_provider(client, db, monkeypatch) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-bb", "sdk-qiniu-bytes")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-qiniu-bytes", download_status="downloaded",
        storage_backend="qiniu_kodo", storage_ref="tenants/tenant-a/images/2.jpg", local_path=None,
    )
    _patch_cloud_provider(monkeypatch, {"tenants/tenant-a/images/2.jpg": b"\xff\xd8\xffcloud-bytes"})

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_bytes_url("room-bb", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xffcloud-bytes"


# ---------------------------------------------------------------------------
# Section D — regression: non-nested media / existing routes unaffected
# ---------------------------------------------------------------------------


def test_regular_image_message_structured_content_stays_none(client, db) -> None:
    from app.main import app

    _insert_message(
        db, msgtype="image", roomid="room-cc", msgtime=100, sdkfileid="sdk-plain-image",
        tenant_id=_TENANT_A,
    )
    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-cc/messages")
    finally:
        app.dependency_overrides.clear()
    out = resp.json()["messages"][0]
    assert out["structured_content"] is None
    assert out["media_type"] == "image"


def test_existing_top_level_media_route_still_works_for_image(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "regression.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"regression")
    msg = _insert_message(
        db, msgtype="image", roomid="room-dd", msgtime=100, sdkfileid="sdk-regression",
        tenant_id=_TENANT_A,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-regression", download_status="downloaded",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/room-dd/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xff" + b"regression"


def test_existing_top_level_media_access_route_still_works(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "regression2.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"regression2")
    msg = _insert_message(
        db, msgtype="image", roomid="room-ee", msgtime=100, sdkfileid="sdk-regression2",
        tenant_id=_TENANT_A,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-regression2", download_status="downloaded",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/room-ee/messages/{msg.msgid}/media/access")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["access_type"] == "proxy"


def test_mixed_message_without_any_media_reference_is_unaffected(client, db) -> None:
    """A mixed message with only text children (no media_refs at all)
    must serialize exactly as before -- the enrichment step is a no-op."""
    from app.main import app

    structured_content = _structured_content([_node("0", "text")], [])
    _insert_message(
        db, msgtype="mixed", roomid="room-ff", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-ff/messages")
    finally:
        app.dependency_overrides.clear()
    out = resp.json()["messages"][0]["structured_content"]
    assert out["fields"]["items"][0]["media"] is None


def test_old_style_structured_content_without_media_refs_key_degrades_safely(client, db) -> None:
    """A historical row decrypted before RND-200's media_refs key existed
    (structured_content present but no "media_refs" key at all) must not
    error -- enrichment treats a missing media_refs as an empty list."""
    from app.main import app

    structured_content = {
        "fields": {"items": [_node("0", "image", media={"has_reference": True})], "item_count": 1},
        "raw": {},
        "parse_warnings": [],
        # no "media_refs" key at all
    }
    _insert_message(
        db, msgtype="mixed", roomid="room-gg", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-gg/messages")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    node = resp.json()["messages"][0]["structured_content"]["fields"]["items"][0]
    assert node["media"]["status"] == "not_downloaded"


# ---------------------------------------------------------------------------
# Section E — API compatibility contract
# ---------------------------------------------------------------------------


def test_mixed_and_chatrecord_media_type_is_structured_by_design(client, db) -> None:
    """Pins down the intentional RND-200 registry change: mixed/chatrecord
    classify as media_type="structured" (PARTIAL support_status,
    category != MEDIA) -- documented here as a deliberate contract, not
    an accidental side effect."""
    from app.main import app

    _insert_message(db, msgtype="mixed", roomid="room-hh", msgtime=100, tenant_id=_TENANT_A)
    _insert_message(
        db, msgtype="chatrecord", roomid="room-ii", msgtime=100, tenant_id=_TENANT_A
    )
    _authed(app, db, _TENANT_A)
    try:
        resp_mixed = client.get("/api/conversations/room-hh/messages")
        resp_chatrecord = client.get("/api/conversations/room-ii/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp_mixed.json()["messages"][0]["media_type"] == "structured"
    assert resp_mixed.json()["messages"][0]["media_status"] is None
    assert resp_chatrecord.json()["messages"][0]["media_type"] == "structured"
    assert resp_chatrecord.json()["messages"][0]["support_status"] == "partial"


def test_nested_media_node_shape_no_longer_uses_bare_has_reference_placeholder(client, db, monkeypatch, tmp_path) -> None:
    """Explicit, intentional contract-evolution pin: a media-bearing
    node's "media" value is now the full descriptor shape (status/
    media_type/mime_type/size_bytes/access_url), replacing the RND-200
    placeholder {"has_reference": bool} the QA fix ticket flagged as
    insufficient. This is a deliberate, documented shape change, not
    silently backward compatible with a hypothetical consumer reading
    "has_reference" directly."""
    from app.main import app

    structured_content = _structured_content(
        [_node("0", "image", media={"has_reference": True})],
        [{"path": "0", "type": "image", "sdkfileid": "sdk-contract"}],
    )
    _insert_message(
        db, msgtype="mixed", roomid="room-jj", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-jj/messages")
    finally:
        app.dependency_overrides.clear()

    media = resp.json()["messages"][0]["structured_content"]["fields"]["items"][0]["media"]
    assert "has_reference" not in media
    assert set(media.keys()) == {
        "status", "media_type", "mime_type", "size_bytes", "access_url",
        "thumbnail_access_url", "image_width", "image_height",
    }


def test_structured_content_top_level_shape_remains_backward_compatible(client, db) -> None:
    """The outer structured_content contract -- {"fields", "parse_warnings"}
    -- is unchanged; only the per-node "media" value's shape changed for
    mixed/chatrecord specifically."""
    from app.main import app

    _insert_message(
        db, msgtype="link", roomid="room-kk", msgtime=100,
        structured_content={"fields": {"title": "x", "url": None}, "raw": {}, "parse_warnings": []},
        tenant_id=_TENANT_A,
    )
    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-kk/messages")
    finally:
        app.dependency_overrides.clear()
    out = resp.json()["messages"][0]["structured_content"]
    assert set(out.keys()) == {"fields", "parse_warnings"}
    assert out["fields"] == {"title": "x", "url": None}


def test_media_refs_never_serialized_into_any_api_response(client, db, monkeypatch, tmp_path) -> None:
    """The parser-internal media_refs list (raw sdkfileids) must never
    appear verbatim in structured_content_out, regardless of enrichment."""
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "no-leak.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"x")
    structured_content = _structured_content(
        [_node("0", "image", media={"has_reference": True})],
        [{"path": "0", "type": "image", "sdkfileid": "sdk-must-not-leak"}],
    )
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-ll", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-must-not-leak", download_status="downloaded",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )
    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-ll/messages")
    finally:
        app.dependency_overrides.clear()
    assert "sdk-must-not-leak" not in resp.text
    assert "media_refs" not in resp.text


# ---------------------------------------------------------------------------
# Security regression — nested media access must never expose media_id
# (the internal MediaFile primary key) or any other internal identifier.
#
# Independent QA's second-round finding: the nested /access endpoint
# reused MediaAccessOut verbatim, which carries media_id — an accepted
# exposure for the single-media-per-message TOP-LEVEL contract, but not
# part of the approved nested-media public contract (parent message +
# node path is the only public handle). Fixed by introducing a dedicated
# NestedMediaAccessOut response model with no media_id field at all —
# these tests hit the real route and inspect the actual serialized JSON,
# not just the Python model's declared fields, per QA's explicit
# instruction not to test only at the model-attribute level.
# ---------------------------------------------------------------------------

_FORBIDDEN_RESPONSE_KEYS = ("media_id", "sdkfileid", "object_key", "local_path", "storage_path")


def _assert_no_forbidden_keys(value) -> None:
    """Recursively walk a decoded JSON response body and assert none of
    the forbidden keys appear anywhere, at any nesting depth — not just
    at the top level."""
    if isinstance(value, dict):
        for key, sub_value in value.items():
            assert key not in _FORBIDDEN_RESPONSE_KEYS, f"forbidden key {key!r} found in response"
            _assert_no_forbidden_keys(sub_value)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_keys(item)


def test_nested_access_local_response_has_no_forbidden_keys(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "sec-local.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"sec-local")
    msg = _insert_mixed_with_media(db, "room-sec-local", "sdk-sec-local")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-sec-local", download_status="downloaded",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-sec-local", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert "media_id" not in body
    assert "sdkfileid" not in body
    assert "object_key" not in body
    assert "local_path" not in body
    assert "storage_path" not in body
    _assert_no_forbidden_keys(body)
    # Functional behavior preserved: the descriptor still carries
    # everything a consumer needs, minus the internal id.
    assert body["storage_backend"] == "local"
    assert body["access_type"] == "proxy"
    assert body["url"] == _nested_bytes_url("room-sec-local", msg.msgid, "0")
    assert body["mime_type"] == "image/jpeg"
    assert "size_bytes" in body  # present in the contract (value depends on the MediaFile row)
    assert body["filename"] is None


def test_nested_access_qiniu_response_has_no_forbidden_keys(client, db, monkeypatch) -> None:
    from app.main import app

    msg = _insert_mixed_with_media(db, "room-sec-qiniu", "sdk-sec-qiniu")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-sec-qiniu", download_status="downloaded",
        storage_backend="qiniu_kodo", storage_ref="tenants/tenant-a/images/9.jpg", local_path=None,
    )
    _patch_cloud_provider(monkeypatch, {"tenants/tenant-a/images/9.jpg": b"fake"})

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-sec-qiniu", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert "media_id" not in body
    assert "sdkfileid" not in body
    assert "object_key" not in body
    assert "local_path" not in body
    assert "storage_path" not in body
    _assert_no_forbidden_keys(body)
    assert body["access_type"] == "signed_url"
    assert body["url"] == _FAKE_SIGNED_URL
    assert body["expires_at"] is not None


def test_nested_access_mixed_child_media_has_no_forbidden_keys(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "sec-mixed.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"sec-mixed")
    msg = _insert_mixed_with_media(db, "room-sec-mixed", "sdk-sec-mixed")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-sec-mixed", download_status="downloaded",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-sec-mixed", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    body = resp.json()
    assert "media_id" not in body
    _assert_no_forbidden_keys(body)


def test_nested_access_chatrecord_child_media_has_no_forbidden_keys(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    voice_path = tmp_path / "sec-cr.amr"
    voice_path.write_bytes(b"#!AMR" + b"sec-cr")
    structured_content = _structured_content(
        [_node("0", "voice", media={"has_reference": True})],
        [{"path": "0", "type": "voice", "sdkfileid": "sdk-sec-cr"}],
        title="digest",
    )
    msg = _insert_message(
        db, msgtype="chatrecord", roomid="room-sec-cr", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(MediaFile(
        sdkfileid="sdk-sec-cr", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(voice_path),
        local_path=str(voice_path), file_size=11, file_type="voice",
    ))
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-sec-cr", msg.msgid, "0"))
    finally:
        app.dependency_overrides.clear()
    body = resp.json()
    assert "media_id" not in body
    _assert_no_forbidden_keys(body)


def test_nested_access_deeply_nested_media_has_no_forbidden_keys(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "sec-deep.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"sec-deep")
    leaf = _node("0.0.0", "image", media={"has_reference": True})
    mid = _node("0.0", "mixed", children=[leaf])
    top = _node("0", "mixed", children=[mid])
    structured_content = _structured_content(
        [top], [{"path": "0.0.0", "type": "image", "sdkfileid": "sdk-sec-deep"}]
    )
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-sec-deep", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(MediaFile(
        sdkfileid="sdk-sec-deep", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(img_path),
        local_path=str(img_path), file_size=12, file_type="image",
    ))
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url("room-sec-deep", msg.msgid, "0.0.0"))
        timeline_resp = client.get("/api/conversations/room-sec-deep/messages")
    finally:
        app.dependency_overrides.clear()

    access_body = resp.json()
    assert "media_id" not in access_body
    _assert_no_forbidden_keys(access_body)
    # Also walk the full nested timeline structure -- forbidden keys must
    # never appear at any depth of the recursive structured_content tree.
    _assert_no_forbidden_keys(timeline_resp.json())


def test_nested_access_multiple_sibling_media_have_no_forbidden_keys(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    a_path = tmp_path / "sec-sib-a.jpg"
    a_path.write_bytes(b"\xff\xd8\xff" + b"a")
    b_path = tmp_path / "sec-sib-b.amr"
    b_path.write_bytes(b"#!AMR" + b"b")

    structured_content = _structured_content(
        [
            _node("0", "image", media={"has_reference": True}),
            _node("1", "voice", media={"has_reference": True}),
        ],
        [
            {"path": "0", "type": "image", "sdkfileid": "sdk-sec-sib-a"},
            {"path": "1", "type": "voice", "sdkfileid": "sdk-sec-sib-b"},
        ],
    )
    msg = _insert_message(
        db, msgtype="mixed", roomid="room-sec-sib", msgtime=100,
        structured_content=structured_content, tenant_id=_TENANT_A,
    )
    db.add(MediaFile(
        sdkfileid="sdk-sec-sib-a", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(a_path),
        local_path=str(a_path), file_size=7, file_type="image",
    ))
    db.add(MediaFile(
        sdkfileid="sdk-sec-sib-b", archive_message_id=msg.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(b_path),
        local_path=str(b_path), file_size=6, file_type="voice",
    ))
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp_a = client.get(_nested_access_url("room-sec-sib", msg.msgid, "0"))
        resp_b = client.get(_nested_access_url("room-sec-sib", msg.msgid, "1"))
        timeline_resp = client.get("/api/conversations/room-sec-sib/messages")
    finally:
        app.dependency_overrides.clear()

    _assert_no_forbidden_keys(resp_a.json())
    _assert_no_forbidden_keys(resp_b.json())
    _assert_no_forbidden_keys(timeline_resp.json())


def test_nested_access_out_model_declares_no_media_id_field() -> None:
    """Model-level guard, in addition to (not instead of) the route-level
    JSON assertions above: NestedMediaAccessOut must never gain a
    media_id field back, e.g. via a careless future edit that copies
    MediaAccessOut's fields wholesale."""
    from app.routers.conversations import NestedMediaAccessOut

    assert "media_id" not in NestedMediaAccessOut.model_fields


def test_top_level_media_access_out_still_declares_media_id() -> None:
    """Explicit confirmation this fix is scoped to the nested contract
    only -- the existing top-level MediaAccessOut model (and therefore
    get_message_media_access's response) is untouched."""
    from app.routers.conversations import MediaAccessOut

    assert "media_id" in MediaAccessOut.model_fields


def test_top_level_media_access_route_still_returns_media_id(client, db, monkeypatch, tmp_path) -> None:
    """End-to-end confirmation of top-level backward compatibility: the
    pre-existing /media/access route's response shape (including
    media_id) is byte-for-byte unchanged by this fix."""
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img_path = tmp_path / "top-level.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"top-level")
    msg = _insert_message(
        db, msgtype="image", roomid="room-top-level", msgtime=100,
        sdkfileid="sdk-top-level", tenant_id=_TENANT_A,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-top-level", download_status="downloaded",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/room-top-level/messages/{msg.msgid}/media/access")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert "media_id" in body
    assert isinstance(body["media_id"], int)
    assert set(body.keys()) == {
        "media_id", "storage_backend", "access_type", "url", "expires_at",
        "content_type", "size_bytes",
    }
