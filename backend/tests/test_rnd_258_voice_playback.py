"""Acceptance tests for RND-258 browser-playable voice variants.

Voice and meeting-recording originals may be AMR/SILK, neither of which is
reliably decodable by browsers.  The authenticated media descriptor must
therefore select a generated MP3/WAV playback variant when one is available,
while retaining the original as the download fallback.
"""

from __future__ import annotations

from pathlib import Path

from app.db.models import MediaFile
from tests.test_media_access_descriptor import _access_url, _authed, client  # noqa: F401
from tests.test_reachability_audit import _TENANT_A, _insert_message, db  # noqa: F401
from tests.test_tenant_media_access import _insert_media_file


def _seed_voice_with_playback_variant(db, tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    original = media_root / "voice.amr"
    playback = media_root / "voice_play.mp3"
    original.write_bytes(b"#!AMR\noriginal")
    playback.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00playable")

    msg = _insert_message(
        db,
        msgid="voice-with-playback",
        msgtype="voice",
        sender="staff_a",
        roomid="voice-room",
        sdkfileid="voice-sdk",
        tenant_id=_TENANT_A,
        msgtime=100,
    )
    media_file = _insert_media_file(
        db,
        msg.id,
        _TENANT_A,
        "voice-sdk",
        local_path=str(original),
    )
    media_file.file_type = "voice"
    media_file.playback_status = "generated"
    media_file.playback_ref = str(playback)
    db.commit()
    assert isinstance(media_file, MediaFile)
    return msg, media_root, playback


def test_access_descriptor_prefers_generated_voice_playback_variant(
    client, db, monkeypatch, tmp_path
) -> None:
    """RED first: existing access resolution returns the AMR original."""
    from app.main import app

    msg, media_root, playback = _seed_voice_with_playback_variant(db, tmp_path)
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    _authed(app, db, _TENANT_A)
    try:
        response = client.get(_access_url("voice-room", msg.msgid))
        playback_response = client.get(response.json()["url"])
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["content_type"] == "audio/mpeg"
    assert body["url"].endswith("?variant=play")
    assert body["size_bytes"] is None
    assert playback_response.status_code == 200
    assert playback_response.content.startswith(b"ID3")
    assert playback_response.headers["content-type"].startswith("audio/mpeg")
    assert Path(playback).exists()


def test_voice_without_playback_variant_falls_back_to_original_download(
    client, db, monkeypatch, tmp_path
) -> None:
    """No generated derivative keeps the original AMR descriptor/bytes."""
    from app.main import app

    msg, media_root, _playback = _seed_voice_with_playback_variant(db, tmp_path)
    media_file = db.query(MediaFile).filter_by(sdkfileid="voice-sdk").one()
    media_file.playback_status = "unsupported_format"
    media_file.playback_ref = None
    db.commit()
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    _authed(app, db, _TENANT_A)
    try:
        descriptor = client.get(_access_url("voice-room", msg.msgid))
        original = client.get(
            f"/api/conversations/voice-room/messages/{msg.msgid}/media"
        )
    finally:
        app.dependency_overrides.clear()

    assert descriptor.status_code == 200
    assert descriptor.json()["content_type"] == "audio/amr"
    assert descriptor.json()["size_bytes"] is None
    assert original.status_code == 200
    assert original.content == b"#!AMR\noriginal"


def test_qiniu_playback_variant_must_match_authenticated_tenant(
    client, db, monkeypatch
) -> None:
    """The existing object-key prefix defence applies to playback_ref too."""
    from app.main import app
    from tests.test_thumbnail_media_access import _patch_recording_provider

    original_ref = "tenants/tenant-a/voice/48.amr"
    msg = _insert_message(
        db,
        msgid="voice-bad-playback-prefix",
        msgtype="voice",
        sender="staff_a",
        roomid="voice-prefix-room",
        sdkfileid="voice-prefix-sdk",
        tenant_id=_TENANT_A,
        msgtime=101,
    )
    media_file = _insert_media_file(
        db,
        msg.id,
        _TENANT_A,
        "voice-prefix-sdk",
        local_path=None,
        storage_backend="qiniu_kodo",
        storage_ref=original_ref,
    )
    media_file.file_type = "voice"
    media_file.playback_status = "generated"
    media_file.playback_ref = "tenants/tenant-b/voice/48_play.mp3"
    db.commit()
    _patch_recording_provider(monkeypatch, {original_ref: b"#!AMR\noriginal"})

    _authed(app, db, _TENANT_A)
    try:
        response = client.get(
            _access_url("voice-prefix-room", msg.msgid) + "?variant=play"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_voice_placeholder_sets_audio_mime_type() -> None:
    """RED first: the browser element must consume descriptor.content_type."""
    source = (
        Path(__file__).resolve().parents[1]
        / "app/web/static/console/message-renderers.js"
    ).read_text(encoding="utf-8")

    assert "audio.type=desc.content_type||'audio/mpeg';" in source
