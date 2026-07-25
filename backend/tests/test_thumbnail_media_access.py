"""API tests for RND-207 thumbnail media access + fixed-window signing.

Proves the /media/access route:
  - signs the THUMBNAIL object (not the original) for variant=thumb, and the
    original otherwise;
  - falls back to the original when variant=thumb is asked for a row with no
    generated thumbnail;
  - reports size_bytes only for the original (None for a thumbnail);
  - stamps expires_at at a fixed window boundary (stable within the window);
  - still enforces the object-key tenant-prefix check on the thumbnail ref.

Reuses the seeding/auth helpers and the sqlite `db` fixture from the existing
descriptor tests; a small recording provider echoes the ref it was asked to
sign so thumbnail-vs-original selection is directly observable.
"""

from __future__ import annotations


from tests.test_media_access_descriptor import (  # noqa: F401
    _FakeCloudProviderWithSignedUrl,
    _access_url,
    _authed,
    client,
)
from tests.test_reachability_audit import (  # noqa: F401
    _TENANT_A,
    _insert_message,
    db,
)
from tests.test_tenant_media_access import _insert_media_file


class _RecordingProvider(_FakeCloudProviderWithSignedUrl):
    """Reuses the signed-url fake's servability behavior (exists/read_bytes)
    but echoes the ref it is asked to sign into the URL, so tests can observe
    which object (original vs thumbnail) was selected. Never a real Qiniu
    call. The original key must be present in `objects` so the servability
    check passes; the thumbnail key is only ever passed to get_download_url."""

    def get_download_url(self, storage_ref, expires_in=None, deadline=None):
        if not storage_ref:
            from app.media_storage import MediaObjectNotFound

            raise MediaObjectNotFound("missing")
        return f"https://media-origin.example.test/{storage_ref}?e={deadline}&token=fake"


def _patch_recording_provider(monkeypatch, objects) -> None:
    from app import media_storage
    from app.services import media_access

    provider = _RecordingProvider(objects)
    real_factory = media_storage.get_media_storage_provider

    def _fake_factory(storage_backend=None):
        backend = storage_backend or media_storage.get_configured_write_backend_name()
        if backend == "qiniu_kodo":
            return provider
        return real_factory(storage_backend)

    monkeypatch.setattr(media_storage, "get_media_storage_provider", _fake_factory)
    monkeypatch.setattr(media_access, "get_media_storage_provider", _fake_factory)


def _seed_qiniu_image(db, *, roomid, msgid, sdkfileid, key, thumb_ref=None,
                      thumb_status=None, width=None, height=None, file_size=1234):
    msg = _insert_message(
        db, msgid=msgid, msgtype="image", sender="staff_a", roomid=roomid,
        sdkfileid=sdkfileid, tenant_id=_TENANT_A, msgtime=100,
    )
    mf = _insert_media_file(
        db, msg.id, _TENANT_A, sdkfileid,
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )
    mf.file_size = file_size
    mf.thumbnail_ref = thumb_ref
    mf.thumbnail_status = thumb_status
    mf.image_width = width
    mf.image_height = height
    db.commit()
    return msg


def test_variant_thumb_signs_thumbnail_and_omits_size(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/1.jpg"
    _patch_recording_provider(monkeypatch, {key: b"bytes"})
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "900")
    thumb = "tenants/tenant-a/thumbnails/1_thumb_v1.jpg"
    msg = _seed_qiniu_image(
        db, roomid="roomT1", msgid="msg-t-1", sdkfileid="sdk-t-1", key=key,
        thumb_ref=thumb, thumb_status="generated", width=800, height=600,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomT1", msg.msgid) + "?variant=thumb")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_type"] == "signed_url"
    assert thumb in body["url"]  # THUMBNAIL was signed, not the original
    assert key not in body["url"]
    assert body["size_bytes"] is None  # thumbnail size not reported
    assert body["content_type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "no-store"


def test_no_variant_signs_original_with_size(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/2.jpg"
    _patch_recording_provider(monkeypatch, {key: b"bytes"})
    thumb = "tenants/tenant-a/thumbnails/2_thumb_v1.jpg"
    msg = _seed_qiniu_image(
        db, roomid="roomT2", msgid="msg-t-2", sdkfileid="sdk-t-2", key=key,
        thumb_ref=thumb, thumb_status="generated", file_size=999,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomT2", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    body = resp.json()
    assert key in body["url"]  # ORIGINAL signed
    assert thumb not in body["url"]
    assert body["size_bytes"] == 999


def test_variant_thumb_falls_back_to_original_when_absent(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/3.jpg"
    _patch_recording_provider(monkeypatch, {key: b"bytes"})
    msg = _seed_qiniu_image(
        db, roomid="roomT3", msgid="msg-t-3", sdkfileid="sdk-t-3", key=key,
        thumb_ref=None, thumb_status=None, file_size=555,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomT3", msg.msgid) + "?variant=thumb")
    finally:
        app.dependency_overrides.clear()

    body = resp.json()
    assert resp.status_code == 200
    assert key in body["url"]  # gracefully fell back to the original
    assert body["size_bytes"] == 555


def test_expires_at_is_window_aligned_and_stable(client, db, monkeypatch) -> None:
    from datetime import datetime

    from app.main import app

    key = "tenants/tenant-a/images/4.jpg"
    _patch_recording_provider(monkeypatch, {key: b"bytes"})
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "900")
    monkeypatch.delenv("MEDIA_SIGNED_URL_WINDOW_SECONDS", raising=False)
    msg = _seed_qiniu_image(
        db, roomid="roomT4", msgid="msg-t-4", sdkfileid="sdk-t-4", key=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        r1 = client.get(_access_url("roomT4", msg.msgid))
        r2 = client.get(_access_url("roomT4", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    e1 = datetime.fromisoformat(r1.json()["expires_at"])
    e2 = datetime.fromisoformat(r2.json()["expires_at"])
    # window-aligned: epoch is a multiple of the 900s window
    assert int(e1.timestamp()) % 900 == 0
    # stable within the same window -> identical expiry (and identical URL)
    assert e1 == e2
    assert r1.json()["url"] == r2.json()["url"]


def test_timeline_exposes_thumbnail_url_and_dims(client, db, monkeypatch) -> None:
    """The messages timeline surfaces thumbnail_access_url (=?variant=thumb)
    and image_width/height for an image row that has a generated thumbnail,
    while media_access_url stays the original."""
    from app.main import app

    key = "tenants/tenant-a/images/6.jpg"
    _patch_recording_provider(monkeypatch, {key: b"bytes"})
    thumb = "tenants/tenant-a/thumbnails/6_thumb_v1.jpg"
    msg = _seed_qiniu_image(
        db, roomid="roomT6", msgid="msg-t-6", sdkfileid="sdk-t-6", key=key,
        thumb_ref=thumb, thumb_status="generated", width=1024, height=768,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomT6/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    row = next(m for m in resp.json()["messages"] if m["msgid"] == msg.msgid)
    assert row["media_status"] == "available"
    assert row["media_access_url"].endswith("/media/access")
    assert row["thumbnail_access_url"] == row["media_access_url"] + "?variant=thumb"
    assert row["image_width"] == 1024
    assert row["image_height"] == 768


def test_timeline_no_thumbnail_leaves_fields_null(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/7.jpg"
    _patch_recording_provider(monkeypatch, {key: b"bytes"})
    msg = _seed_qiniu_image(
        db, roomid="roomT7", msgid="msg-t-7", sdkfileid="sdk-t-7", key=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomT7/messages")
    finally:
        app.dependency_overrides.clear()

    row = next(m for m in resp.json()["messages"] if m["msgid"] == msg.msgid)
    assert row["thumbnail_access_url"] is None
    assert row["image_width"] is None


def test_variant_thumb_tenant_prefix_mismatch_denied(client, db, monkeypatch) -> None:
    """A thumbnail_ref whose tenant prefix disagrees with the row's tenant is
    rejected 404 — the object-key tenant-prefix defense applies to the
    thumbnail ref, not just the original."""
    from app.main import app

    key = "tenants/tenant-a/images/5.jpg"
    _patch_recording_provider(monkeypatch, {key: b"bytes"})
    bad_thumb = "tenants/tenant-b/thumbnails/5_thumb_v1.jpg"  # wrong tenant
    msg = _seed_qiniu_image(
        db, roomid="roomT5", msgid="msg-t-5", sdkfileid="sdk-t-5", key=key,
        thumb_ref=bad_thumb, thumb_status="generated",
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomT5", msg.msgid) + "?variant=thumb")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
