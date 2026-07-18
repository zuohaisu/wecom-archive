"""
Tests for RND-199 — generalizing media serving (storage-state resolution,
classification, and the /media + /media/access routes) from image-only to
every message type the media pipeline downloads: image/voice/video/file
(exposed through the timeline too) and emotion (servable directly through
the route, but intentionally not surfaced by the timeline — see
app/media_storage.py's SERVABLE_MEDIA_MSGTYPES / SUPPORTED_MIGRATION_MEDIA_TYPES
docstrings for why).

Regression coverage: every assertion this file makes about "image"
behavior must match tests/test_media_download.py's existing image-only
assertions — proving the generalized functions are drop-in equivalents
for image, not just "close enough".

Run (from backend/):
    pytest tests/test_generic_media_serving.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_staff_seats import _msg


# ---------------------------------------------------------------------------
# app.media_storage — resolve_downloadable_media_file_state / _state /
# resolve_servable_downloadable_media_path
# ---------------------------------------------------------------------------


def test_resolve_downloadable_media_file_state_servable_for_video(tmp_path, monkeypatch) -> None:
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake mp4 bytes")

    assert media_storage.resolve_downloadable_media_file_state(str(video)) == "servable"
    assert media_storage.resolve_servable_downloadable_media_path(str(video)) == video.resolve()


def test_resolve_downloadable_media_file_state_servable_for_voice_and_file(tmp_path, monkeypatch) -> None:
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    voice = tmp_path / "clip.amr"
    voice.write_bytes(b"fake amr bytes")
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"fake pdf bytes")

    assert media_storage.resolve_downloadable_media_file_state(str(voice)) == "servable"
    assert media_storage.resolve_downloadable_media_file_state(str(doc)) == "servable"


def test_resolve_downloadable_media_file_state_unsupported_extension(tmp_path, monkeypatch) -> None:
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    bad = tmp_path / "archive.bmp"
    bad.write_bytes(b"BM fake bmp bytes")

    assert media_storage.resolve_downloadable_media_file_state(str(bad)) == "unsupported_type"
    assert media_storage.resolve_servable_downloadable_media_path(str(bad)) is None


def test_resolve_downloadable_media_file_state_missing(tmp_path, monkeypatch) -> None:
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    assert media_storage.resolve_downloadable_media_file_state(str(tmp_path / "gone.mp4")) == "missing"


@pytest.mark.parametrize("ext,body", [(".jpg", b"jpeg"), (".png", b"png"), (".gif", b"gif"), (".webp", b"webp")])
def test_resolve_downloadable_media_file_state_matches_image_only_predicate(ext, body, tmp_path, monkeypatch) -> None:
    """Parity check (RND-199): for every image extension the image-only
    resolve_image_file_state already accepts, the generic predicate must
    agree exactly — image behavior is unchanged by this generalization."""
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img = tmp_path / f"photo{ext}"
    img.write_bytes(body)

    assert (
        media_storage.resolve_downloadable_media_file_state(str(img))
        == media_storage.resolve_image_file_state(str(img))
        == "servable"
    )


def test_resolve_downloadable_media_state_uses_row_storage_fields(tmp_path, monkeypatch) -> None:
    from app import media_storage

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake mp4 bytes")

    row = SimpleNamespace(storage_backend="local", storage_ref=str(video), local_path=None)
    assert media_storage.resolve_downloadable_media_state(row) == "servable"

    missing_row = SimpleNamespace(storage_backend=None, storage_ref=None, local_path=None)
    assert media_storage.resolve_downloadable_media_state(missing_row) == "missing"


def test_servable_media_msgtypes_is_migration_types_plus_emotion() -> None:
    from app import media_storage

    assert media_storage.SERVABLE_MEDIA_MSGTYPES == (
        media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES | frozenset({"emotion"})
    )
    assert "emotion" not in media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES
    assert "emotion" in media_storage.SERVABLE_MEDIA_MSGTYPES


# ---------------------------------------------------------------------------
# app.media_classification.resolve_downloadable_media_status — parity with
# resolve_image_media_status for "image", plus video/voice/file coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "download_status,file_state,expected_status,expected_reason",
    [
        ("downloaded", "servable", "available", None),
        ("downloaded", "missing", "failed", "media_file_missing_on_disk"),
        ("downloaded", "unsupported_type", "failed", "media_file_type_unsupported"),
        ("downloaded", "unavailable", "unavailable", "media_storage_unavailable"),
        ("failed", "missing", "failed", "media_download_failed"),
        ("pending", "missing", "not_downloaded", "video_playback_not_implemented"),
        (None, "missing", "not_downloaded", "video_playback_not_implemented"),
    ],
)
def test_resolve_downloadable_media_status_video(
    download_status, file_state, expected_status, expected_reason
) -> None:
    from app.media_classification import classify_media, resolve_downloadable_media_status

    base = classify_media("video", has_sdkfileid=True)
    result = resolve_downloadable_media_status(base, download_status, file_state)
    assert result.media_type == "video"
    assert result.media_status == expected_status
    assert result.unsupported_reason == expected_reason


def test_resolve_downloadable_media_status_matches_resolve_image_media_status_for_image() -> None:
    from app.media_classification import (
        classify_media,
        resolve_downloadable_media_status,
        resolve_image_media_status,
    )

    base = classify_media("image", has_sdkfileid=True)
    for download_status, file_state in [
        ("downloaded", "servable"),
        ("downloaded", "missing"),
        ("downloaded", "unsupported_type"),
        ("downloaded", "unavailable"),
        ("failed", "missing"),
        ("pending", "missing"),
        (None, "missing"),
    ]:
        assert resolve_downloadable_media_status(base, download_status, file_state) == (
            resolve_image_media_status(base, download_status, file_state)
        )


def test_resolve_downloadable_media_status_is_noop_for_emotion() -> None:
    """emotion collapses to media_type="unsupported" in classify_media()
    (an intentional, tested contract) — resolve_downloadable_media_status
    must never touch it, so the timeline's emotion rendering is unchanged
    by this ticket."""
    from app.media_classification import classify_media, resolve_downloadable_media_status

    base = classify_media("emotion", has_sdkfileid=True)
    assert base.media_type == "unsupported"
    result = resolve_downloadable_media_status(base, "downloaded", "servable")
    assert result == base


# ---------------------------------------------------------------------------
# /api/conversations/{id}/messages/{msgid}/media and .../media/access —
# generalized to voice/video/file (and, route-only, emotion).
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _override_db_for_media_route(all_msgs, media_files):
    from app.db.models import ArchiveMessageRecipient, Contact, MediaFile, MessageRevocation

    def _override_db():
        mock = MagicMock()

        msg_q = MagicMock()
        msg_q.filter.return_value = msg_q
        msg_q.all.return_value = list(all_msgs)

        empty_q = MagicMock()
        empty_q.filter.return_value = empty_q
        empty_q.all.return_value = []

        media_q = MagicMock()
        media_q.filter.return_value = media_q
        media_q.all.return_value = list(media_files)
        media_q.first.return_value = media_files[0] if media_files else None

        def _query(model):
            if model is ArchiveMessageRecipient:
                return empty_q
            if model is Contact:
                return empty_q
            if model is MediaFile:
                return media_q
            if model is MessageRevocation:
                return empty_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    return _override_db


def _install_overrides(app, override_db):
    from app.auth import get_current_user
    from app.db.session import get_db

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = override_db


@pytest.mark.parametrize(
    "msgtype,filename,body,content_type",
    [
        ("voice", "clip.amr", b"fake amr bytes", "audio/amr"),
        ("video", "clip.mp4", b"fake mp4 bytes", "video/mp4"),
        ("file", "report.pdf", b"fake pdf bytes", "application/pdf"),
    ],
)
def test_media_route_serves_downloaded_non_image_types(
    client, monkeypatch, tmp_path, msgtype, filename, body, content_type
) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    stored = media_root / filename
    stored.write_bytes(body)

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype=msgtype, sdkfileid="sdk-1")]
    all_msgs[0].msgid = "m-1"
    media_files = [
        SimpleNamespace(
            id=1,
            archive_message_id=1,
            download_status="downloaded",
            storage_backend="local",
            storage_ref=str(stored),
            local_path=str(stored),
            file_type=msgtype,
            file_size=len(body),
        )
    ]

    _install_overrides(app, _override_db_for_media_route(all_msgs, media_files))
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.content == body
    assert resp.headers["content-type"].startswith(content_type)


def test_media_route_still_404s_for_a_truly_unsupported_msgtype(client, monkeypatch, tmp_path) -> None:
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="location", sdkfileid=None)]
    all_msgs[0].msgid = "m-1"

    _install_overrides(app, _override_db_for_media_route(all_msgs, []))
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_media_route_serves_downloaded_emotion_directly_even_though_timeline_hides_it(
    client, monkeypatch, tmp_path
) -> None:
    """RND-199: the route itself serves emotion media once downloaded (the
    pipeline's "expose through /media/access" requirement), even though
    classify_media()/the timeline still report emotion as the generic
    "unsupported" placeholder (unchanged rendering contract)."""
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    stored = media_root / "sticker.gif"
    stored.write_bytes(b"fake gif bytes")

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="emotion", sdkfileid="sdk-1")]
    all_msgs[0].msgid = "m-1"
    media_files = [
        SimpleNamespace(
            id=1,
            archive_message_id=1,
            download_status="downloaded",
            storage_backend="local",
            storage_ref=str(stored),
            local_path=str(stored),
            file_type="emotion",
            file_size=14,
        )
    ]

    _install_overrides(app, _override_db_for_media_route(all_msgs, media_files))
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.content == b"fake gif bytes"


def test_timeline_exposes_media_url_for_downloaded_voice_message(client, monkeypatch, tmp_path) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))
    stored = media_root / "clip.amr"
    stored.write_bytes(b"fake amr bytes")

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="voice", sdkfileid="sdk-1")]
    all_msgs[0].msgid = "m-1"
    media_files = [
        SimpleNamespace(
            archive_message_id=1,
            download_status="downloaded",
            storage_backend="local",
            storage_ref=str(stored),
            local_path=str(stored),
            file_type="voice",
        )
    ]

    _install_overrides(app, _override_db_for_media_route(all_msgs, media_files))
    try:
        resp = client.get("/api/conversations/room1/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "voice"
    assert msg["media_status"] == "available"
    assert msg["media_url"] == "/api/conversations/room1/messages/m-1/media"
    assert msg["media_access_url"] == "/api/conversations/room1/messages/m-1/media/access"
    assert "sdk-1" not in resp.text


def test_timeline_does_not_expose_media_url_for_emotion_message(client, monkeypatch, tmp_path) -> None:
    """Unchanged rendering contract (RND-199 out of scope: frontend
    rendering) — even with a downloaded, servable media_files row, the
    timeline never sets media_url/media_access_url for emotion, and
    media_type/media_status stay exactly what they were before this
    ticket."""
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))
    stored = media_root / "sticker.gif"
    stored.write_bytes(b"fake gif bytes")

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="emotion", sdkfileid="sdk-1")]
    all_msgs[0].msgid = "m-1"
    media_files = [
        SimpleNamespace(
            archive_message_id=1,
            download_status="downloaded",
            storage_backend="local",
            storage_ref=str(stored),
            local_path=str(stored),
            file_type="emotion",
        )
    ]

    _install_overrides(app, _override_db_for_media_route(all_msgs, media_files))
    try:
        resp = client.get("/api/conversations/room1/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_type"] == "unsupported"
    assert msg["media_status"] == "unsupported"
    assert msg["media_url"] is None
    assert msg["media_access_url"] is None


def test_media_access_route_returns_proxy_descriptor_for_local_voice(client, monkeypatch, tmp_path) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))
    stored = media_root / "clip.amr"
    stored.write_bytes(b"fake amr bytes")

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="voice", sdkfileid="sdk-1")]
    all_msgs[0].msgid = "m-1"
    media_files = [
        SimpleNamespace(
            id=7,
            archive_message_id=1,
            download_status="downloaded",
            storage_backend="local",
            storage_ref=str(stored),
            local_path=str(stored),
            file_type="voice",
            file_size=len(b"fake amr bytes"),
        )
    ]

    _install_overrides(app, _override_db_for_media_route(all_msgs, media_files))
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media/access")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["storage_backend"] == "local"
    assert body["access_type"] == "proxy"
    assert body["url"] == "/api/conversations/room1/messages/m-1/media"
    assert body["content_type"] == "audio/amr"


# ---------------------------------------------------------------------------
# Image regression: the exact RND-144 scenario (already covered by
# tests/test_media_download.py) must still pass through the now-generic
# route functions unchanged.
# ---------------------------------------------------------------------------


def test_media_route_image_regression_unchanged(client, monkeypatch, tmp_path) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))
    jpeg_bytes = b"\xff\xd8\xff" + b"real-jpeg-body"
    stored = media_root / "photo.jpg"
    stored.write_bytes(jpeg_bytes)

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")]
    all_msgs[0].msgid = "m-1"
    media_files = [
        SimpleNamespace(
            id=1,
            archive_message_id=1,
            download_status="downloaded",
            storage_backend="local",
            storage_ref=str(stored),
            local_path=str(stored),
            file_type="image",
            file_size=len(jpeg_bytes),
        )
    ]

    _install_overrides(app, _override_db_for_media_route(all_msgs, media_files))
    try:
        resp = client.get("/api/conversations/room1/messages/m-1/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.content == jpeg_bytes
    assert resp.headers["content-type"] == "image/jpeg"
