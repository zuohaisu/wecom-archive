"""
Tests for RND-144 — Download and render image messages in admin timeline.

Scope: image only. See app/media_classification.py (resolve_image_media_status)
and app/media_storage.py (path-safety + content-type resolution) and
app/routers/conversations.py (GET .../messages/{msgid}/media) for the
implementation this file exercises.

The WeCom SDK wrapper (app/sdk/wecom_sdk.py) does not implement media
download, and this RND does not add one — it only serves/renders
media_files rows that are already marked download_status="downloaded"
(e.g. by some future worker), and otherwise preserves the RND-133
placeholder behavior. See the module docstring in app/media_storage.py.

Run (from backend/):
    pytest tests/test_media_download.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_staff_seats import _msg


# ---------------------------------------------------------------------------
# resolve_image_media_status — pure function, no DB/filesystem access
# ---------------------------------------------------------------------------


def test_resolve_image_status_downloaded_and_servable() -> None:
    from app.media_classification import classify_media, resolve_image_media_status

    base = classify_media("image", has_sdkfileid=True)
    result = resolve_image_media_status(base, "downloaded", "servable")
    assert result.media_type == "image"
    assert result.media_status == "available"
    assert result.unsupported_reason is None


def test_resolve_image_status_downloaded_but_file_missing() -> None:
    from app.media_classification import classify_media, resolve_image_media_status

    base = classify_media("image", has_sdkfileid=True)
    result = resolve_image_media_status(base, "downloaded", "missing")
    assert result.media_status == "failed"
    assert result.unsupported_reason == "media_file_missing_on_disk"


def test_resolve_image_status_downloaded_but_unsupported_type() -> None:
    """RND-144 QA fix: a downloaded file that exists but has a disallowed
    extension (e.g. .bmp) must NOT be reported as available — it must use
    the same tri-state (resolve_image_file_state) the media route itself
    checks, so media_status=="available" and the route returning 200 can
    never disagree."""
    from app.media_classification import classify_media, resolve_image_media_status

    base = classify_media("image", has_sdkfileid=True)
    result = resolve_image_media_status(base, "downloaded", "unsupported_type")
    assert result.media_status == "failed"
    assert result.unsupported_reason == "media_file_type_unsupported"
    # The reason must not embed any path/filename fragment.
    assert "/" not in result.unsupported_reason
    assert "." not in result.unsupported_reason


def test_resolve_image_status_failed_download() -> None:
    from app.media_classification import classify_media, resolve_image_media_status

    base = classify_media("image", has_sdkfileid=True)
    result = resolve_image_media_status(base, "failed", "missing")
    assert result.media_status == "failed"
    assert result.unsupported_reason == "media_download_failed"


def test_resolve_image_status_pending_keeps_base() -> None:
    from app.media_classification import classify_media, resolve_image_media_status

    base = classify_media("image", has_sdkfileid=True)
    result = resolve_image_media_status(base, "pending", "missing")
    assert result == base
    assert result.media_status == "not_downloaded"


def test_resolve_image_status_no_media_file_row_keeps_base() -> None:
    from app.media_classification import classify_media, resolve_image_media_status

    base = classify_media("image", has_sdkfileid=True)
    result = resolve_image_media_status(base, None, "missing")
    assert result == base


def test_resolve_image_status_non_image_passthrough() -> None:
    from app.media_classification import classify_media, resolve_image_media_status

    base = classify_media("video", has_sdkfileid=True)
    result = resolve_image_media_status(base, "downloaded", "servable")
    assert result == base


# ---------------------------------------------------------------------------
# media_storage — safe path resolution + content-type detection
# ---------------------------------------------------------------------------


def test_local_storage_provider_save_read_exists_delete(tmp_path) -> None:
    from app.media_storage import LocalStorageProvider

    provider = LocalStorageProvider(tmp_path)
    stored_ref = provider.save_bytes("tenants/tenant-a/images/1.part", b"image-bytes")

    assert provider.exists(stored_ref) is True
    assert provider.read_bytes(stored_ref) == b"image-bytes"
    assert provider.size_bytes(stored_ref) == len(b"image-bytes")
    assert provider.delete(stored_ref) is True
    assert provider.exists(stored_ref) is False


def test_local_storage_provider_blocks_path_escape(tmp_path) -> None:
    from app.media_storage import LocalStorageProvider

    provider = LocalStorageProvider(tmp_path / "media")

    assert provider.exists(str(tmp_path / "outside.jpg")) is False
    assert provider.get_local_path(str(tmp_path / "outside.jpg")) is None


def test_media_storage_provider_factory_defaults_to_local(tmp_path, monkeypatch) -> None:
    from app.media_storage import LocalStorageProvider, get_media_storage_provider

    monkeypatch.delenv("MEDIA_STORAGE_PROVIDER", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))

    provider = get_media_storage_provider()
    assert isinstance(provider, LocalStorageProvider)
    assert provider.supports_local_path() is True


def test_media_root_unset_returns_none(monkeypatch) -> None:
    from app import media_storage

    monkeypatch.delenv("STORAGE_LOCAL_PATH", raising=False)
    assert media_storage.get_media_root() is None
    assert media_storage.resolve_safe_media_path("/anything/x.jpg") is None


def test_resolve_safe_media_path_valid_file(tmp_path, monkeypatch) -> None:
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    f = tmp_path / "tenant-a" / "2026" / "07"
    f.mkdir(parents=True)
    img = f / "photo.jpg"
    img.write_bytes(b"\xff\xd8\xff fake jpeg bytes")

    resolved = media_storage.resolve_safe_media_path(str(img))
    assert resolved is not None
    assert resolved.is_file()


def test_resolve_safe_media_path_missing_file(tmp_path, monkeypatch) -> None:
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    assert media_storage.resolve_safe_media_path(str(tmp_path / "nope.jpg")) is None


def test_resolve_safe_media_path_blocks_traversal(tmp_path, monkeypatch) -> None:
    from app import media_storage

    root = tmp_path / "media_root"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(root))

    traversal_path = str(root / ".." / "outside.jpg")
    assert media_storage.resolve_safe_media_path(traversal_path) is None


def test_resolve_safe_media_path_blocks_symlink_escape(tmp_path, monkeypatch) -> None:
    from app import media_storage

    root = tmp_path / "media_root"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    symlink = root / "escape.jpg"
    try:
        symlink.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(root))
    assert media_storage.resolve_safe_media_path(str(symlink)) is None


def test_detect_image_content_type_allowed_extensions(tmp_path) -> None:
    from app import media_storage

    assert media_storage.detect_image_content_type(tmp_path / "a.jpg") == "image/jpeg"
    assert media_storage.detect_image_content_type(tmp_path / "a.jpeg") == "image/jpeg"
    assert media_storage.detect_image_content_type(tmp_path / "a.png") == "image/png"
    assert media_storage.detect_image_content_type(tmp_path / "a.gif") == "image/gif"
    assert media_storage.detect_image_content_type(tmp_path / "a.WEBP") == "image/webp"


def test_detect_image_content_type_rejects_other_extensions(tmp_path) -> None:
    from app import media_storage

    assert media_storage.detect_image_content_type(tmp_path / "a.exe") is None
    assert media_storage.detect_image_content_type(tmp_path / "a.txt") is None
    assert media_storage.detect_image_content_type(tmp_path / "a") is None


# ---------------------------------------------------------------------------
# resolve_image_file_state / resolve_servable_image_path — the single shared
# predicate used by both the timeline serializer and the media route
# (RND-144 QA fix)
# ---------------------------------------------------------------------------


def test_resolve_image_file_state_servable(tmp_path, monkeypatch) -> None:
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"fake-jpeg-bytes")

    assert media_storage.resolve_image_file_state(str(img)) == "servable"
    assert media_storage.resolve_servable_image_path(str(img)) == img.resolve()


def test_resolve_image_file_state_unsupported_type(tmp_path, monkeypatch) -> None:
    """A file that exists under the media root but has a disallowed
    extension (e.g. .bmp) must be reported as "unsupported_type", not
    "servable" — this is exactly the QA-reported inconsistency: the
    timeline previously treated "exists safely" as sufficient, while the
    route separately rejected the extension."""
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img = tmp_path / "photo.bmp"
    img.write_bytes(b"BM fake bmp bytes")

    assert media_storage.resolve_image_file_state(str(img)) == "unsupported_type"
    assert media_storage.resolve_servable_image_path(str(img)) is None


def test_resolve_image_file_state_missing(tmp_path, monkeypatch) -> None:
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    assert media_storage.resolve_image_file_state(str(tmp_path / "nope.jpg")) == "missing"
    assert media_storage.resolve_servable_image_path(str(tmp_path / "nope.jpg")) is None


# ---------------------------------------------------------------------------
# Timeline API — media_url behavior per media_files.download_status
# ---------------------------------------------------------------------------


def _media_file(archive_message_id, download_status, local_path="/data/media/x.jpg"):
    return SimpleNamespace(
        archive_message_id=archive_message_id,
        download_status=download_status,
        local_path=local_path,
        file_type="image",
    )


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _run_messages_query(client, app, all_msgs, media_files=None):
    from app.auth import get_current_user
    from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact, MediaFile
    from app.db.session import get_db

    media_files = media_files or []

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
        media_q.all.return_value = list(media_files)

        def _query(model):
            if model is ArchiveMessageRecipient:
                return rcpt_q
            if model is Contact:
                return contact_q
            if model is MediaFile:
                return media_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    try:
        return client.get("/api/conversations/room1/messages")
    finally:
        app.dependency_overrides.clear()


def test_timeline_image_no_media_file_row_not_downloaded(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")]
    resp = _run_messages_query(client, app, all_msgs, media_files=[])
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_status"] == "not_downloaded"
    assert msg["media_url"] is None


def test_timeline_image_pending_media_file_no_url(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")]
    media_files = [_media_file(1, "pending")]
    resp = _run_messages_query(client, app, all_msgs, media_files=media_files)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_url"] is None
    assert msg["media_status"] == "not_downloaded"


def test_timeline_image_failed_media_file(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")]
    media_files = [_media_file(1, "failed")]
    resp = _run_messages_query(client, app, all_msgs, media_files=media_files)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_status"] == "failed"
    assert msg["media_url"] is None


def test_timeline_image_downloaded_but_file_missing_on_disk(client, monkeypatch) -> None:
    from app.main import app

    monkeypatch.delenv("STORAGE_LOCAL_PATH", raising=False)
    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")]
    media_files = [_media_file(1, "downloaded", local_path="/nonexistent/x.jpg")]
    resp = _run_messages_query(client, app, all_msgs, media_files=media_files)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_status"] == "failed"
    assert msg["media_url"] is None


def test_timeline_image_downloaded_and_available(client, monkeypatch, tmp_path) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.jpg"
    img_path.write_bytes(b"fake-jpeg-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")]
    all_msgs[0].msgid = "m-1"
    media_files = [_media_file(1, "downloaded", local_path=str(img_path))]
    resp = _run_messages_query(client, app, all_msgs, media_files=media_files)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_status"] == "available"
    assert msg["media_url"] == "/api/conversations/room1/messages/m-1/media"


def test_timeline_image_downloaded_disallowed_extension_not_available(client, monkeypatch, tmp_path) -> None:
    """RND-144 QA blocker regression: a downloaded row pointing at a real,
    safely-resolvable file with a disallowed extension (.bmp) must NOT be
    reported as media_status=="available" and must NOT get a media_url —
    otherwise the timeline promises an image the media route can't serve."""
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.bmp"
    img_path.write_bytes(b"BM fake bmp bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")]
    media_files = [_media_file(1, "downloaded", local_path=str(img_path))]
    resp = _run_messages_query(client, app, all_msgs, media_files=media_files)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_status"] != "available"
    assert msg["media_url"] is None
    assert msg["unsupported_reason"] == "media_file_type_unsupported"
    assert str(img_path) not in resp.text


def test_timeline_never_exposes_media_file_internal_fields(client, monkeypatch, tmp_path) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.jpg"
    img_path.write_bytes(b"fake-jpeg-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    all_msgs = [
        _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-should-never-appear")
    ]
    media_files = [_media_file(1, "downloaded", local_path=str(img_path))]
    resp = _run_messages_query(client, app, all_msgs, media_files=media_files)
    assert resp.status_code == 200
    body_text = resp.text
    assert "sdk-should-never-appear" not in body_text
    assert str(img_path) not in body_text
    msg = resp.json()["messages"][0]
    for forbidden_field in ("sdkfileid", "local_path", "oss_key", "raw_encrypted_payload", "decrypted_payload"):
        assert forbidden_field not in msg


def test_timeline_text_message_unchanged(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, content_text="hi", msgtype="text")]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "text"
    assert msg["media_url"] is None


def test_timeline_unsupported_msgtype_unchanged(client) -> None:
    from app.main import app

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="emotion")]
    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "unsupported"
    assert msg["media_url"] is None


# ---------------------------------------------------------------------------
# GET /api/conversations/{conversation_id}/messages/{msgid}/media
# ---------------------------------------------------------------------------


@pytest.fixture()
def media_route_env(tmp_path, monkeypatch):
    """Shared fixture: a real media root + one downloaded image file on disk."""
    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.jpg"
    img_path.write_bytes(b"fake-jpeg-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))
    return media_root, img_path


def _override_media_route(app, monkeypatch, *, conv_messages, media_file, auth=True):
    from app.auth import get_current_user
    from app.db.models import MediaFile
    from app.db.session import get_db
    from app.routers import conversations as conv

    monkeypatch.setattr(
        conv, "_fetch_conversation_messages", lambda db, conversation_id, tenant_id: conv_messages
    )

    def _override_db():
        mock = MagicMock()
        media_q = MagicMock()
        media_q.filter.return_value = media_q
        media_q.first.return_value = media_file

        def _query(model):
            if model is MediaFile:
                return media_q
            return MagicMock()

        mock.query.side_effect = _query
        yield mock

    if auth:
        app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db


def test_media_route_requires_auth(client) -> None:
    from app.db.session import get_db
    from app.main import app

    def _override_db_empty():
        yield MagicMock()

    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_media_route_404_when_message_not_in_conversation(client, monkeypatch, media_route_env) -> None:
    """Simulates cross-tenant / cross-conversation access: _fetch_conversation_messages
    (the same tenant-scoped helper the timeline route uses) returns no matching message."""
    from app.main import app

    _override_media_route(app, monkeypatch, conv_messages=[], media_file=None)
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_media_route_rejects_non_image_message(client, monkeypatch, media_route_env) -> None:
    from app.main import app

    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="video", sdkfileid="sdk-1")
    _override_media_route(app, monkeypatch, conv_messages=[msg], media_file=None)
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_media_route_rejects_missing_media_file_row(client, monkeypatch, media_route_env) -> None:
    from app.main import app

    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")
    _override_media_route(app, monkeypatch, conv_messages=[msg], media_file=None)
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_media_route_rejects_not_downloaded_status(client, monkeypatch, media_route_env) -> None:
    from app.main import app

    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")
    mf = _media_file(1, "pending")
    _override_media_route(app, monkeypatch, conv_messages=[msg], media_file=mf)
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_media_route_prevents_path_traversal(client, monkeypatch, media_route_env, tmp_path) -> None:
    from app.main import app

    media_root, _ = media_route_env
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"top-secret")

    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")
    mf = _media_file(1, "downloaded", local_path=str(outside))
    _override_media_route(app, monkeypatch, conv_messages=[msg], media_file=mf)
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404
    assert "top-secret" not in resp.text


def test_media_route_serves_downloaded_image(client, monkeypatch, media_route_env) -> None:
    from app.main import app

    _, img_path = media_route_env
    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")
    mf = _media_file(1, "downloaded", local_path=str(img_path))
    _override_media_route(app, monkeypatch, conv_messages=[msg], media_file=mf)
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == b"fake-jpeg-bytes"
    assert str(img_path) not in resp.text
    assert "content-disposition" not in resp.headers


def test_media_route_rejects_disallowed_extension(client, monkeypatch, media_route_env, tmp_path) -> None:
    """RND-144 QA blocker regression: the media route must reject a
    downloaded, safely-resolvable file with a disallowed extension (.bmp)
    with a plain 404 — the same case the timeline serializer must also
    refuse to call "available" (see
    test_timeline_image_downloaded_disallowed_extension_not_available)."""
    from app.main import app

    media_root, _ = media_route_env
    bmp_path = media_root / "photo.bmp"
    bmp_path.write_bytes(b"BM fake bmp bytes")

    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")
    mf = _media_file(1, "downloaded", local_path=str(bmp_path))
    _override_media_route(app, monkeypatch, conv_messages=[msg], media_file=mf)
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404
    assert str(bmp_path) not in resp.text


def test_timeline_and_media_route_agree_on_disallowed_extension(
    client, monkeypatch, tmp_path
) -> None:
    """End-to-end consistency check for the RND-144 QA blocker: for the same
    downloaded-but-disallowed-extension media_files row, the timeline API
    must not advertise media_url, and the media route (given the same
    underlying file) must not serve it. Both sides are driven by the same
    resolve_image_file_state()/resolve_servable_image_path() predicate."""
    from app.main import app
    from app.routers import conversations as conv

    media_root = tmp_path / "media"
    media_root.mkdir()
    bmp_path = media_root / "photo.bmp"
    bmp_path.write_bytes(b"BM fake bmp bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    msg = _msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")
    media_files = [_media_file(1, "downloaded", local_path=str(bmp_path))]

    timeline_resp = _run_messages_query(client, app, [msg], media_files=media_files)
    assert timeline_resp.status_code == 200
    timeline_msg = timeline_resp.json()["messages"][0]
    assert timeline_msg["media_url"] is None
    assert timeline_msg["media_status"] != "available"

    _override_media_route(app, monkeypatch, conv_messages=[msg], media_file=media_files[0])
    try:
        media_resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()
    assert media_resp.status_code == 404
