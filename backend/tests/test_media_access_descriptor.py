"""
Tests for RND-187 — unified media access descriptor
(GET /api/conversations/{conversation_id}/messages/{msgid}/media/access) and
its supporting config/permission primitives.

Scope:
  - MEDIA_SIGNED_URL_TTL_SECONDS config: default, bounds, invalid values.
  - object_key_tenant_prefix_matches(): defense-in-depth object-key check.
  - The access-descriptor route itself: local -> proxy descriptor,
    Qiniu -> signed_url descriptor, tenant isolation, Cache-Control,
    no-secret-leakage, and logging redaction.

Never calls the real Qiniu service — Qiniu-backed rows use the same
_FakeCloudProvider pattern as tests/test_qiniu_media_serving.py, extended
with get_download_url() so the route's signed-URL path is exercised without
network access. The real QiniuStorageProvider.get_download_url() SDK
boundary is covered separately in tests/test_qiniu_storage.py.

Run (from backend/):
    pytest tests/test_media_access_descriptor.py -v
"""

from __future__ import annotations

import logging

import pytest

from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageUnavailable,
    get_signed_url_ttl_seconds,
    object_key_tenant_prefix_matches,
)
from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)
from tests.test_tenant_media_access import _insert_media_file

_FAKE_SIGNED_URL = (
    "https://media.example.com/tenants/tenant-a/images/1.jpg"
    "?e=1999999999&token=fake-signed-token-should-never-leak"
)


# ---------------------------------------------------------------------------
# TTL config (app.media_storage.get_signed_url_ttl_seconds)
# ---------------------------------------------------------------------------


def test_ttl_default_is_900(monkeypatch) -> None:
    monkeypatch.delenv("MEDIA_SIGNED_URL_TTL_SECONDS", raising=False)
    assert get_signed_url_ttl_seconds() == 900


def test_ttl_custom_value_within_bounds(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "300")
    assert get_signed_url_ttl_seconds() == 300


@pytest.mark.parametrize("value", ["60", "3600"])
def test_ttl_boundary_values_accepted(monkeypatch, value) -> None:
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", value)
    assert get_signed_url_ttl_seconds() == int(value)


def test_ttl_below_minimum_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "59")
    with pytest.raises(MediaStorageConfigurationError):
        get_signed_url_ttl_seconds()


def test_ttl_above_maximum_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "3601")
    with pytest.raises(MediaStorageConfigurationError):
        get_signed_url_ttl_seconds()


def test_ttl_non_integer_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "fifteen-minutes")
    with pytest.raises(MediaStorageConfigurationError):
        get_signed_url_ttl_seconds()


def test_ttl_zero_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "0")
    with pytest.raises(MediaStorageConfigurationError):
        get_signed_url_ttl_seconds()


def test_ttl_negative_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "-900")
    with pytest.raises(MediaStorageConfigurationError):
        get_signed_url_ttl_seconds()


# ---------------------------------------------------------------------------
# object_key_tenant_prefix_matches
# ---------------------------------------------------------------------------


def test_object_key_prefix_matches_own_tenant() -> None:
    assert object_key_tenant_prefix_matches("tenants/tenant-a/images/1.jpg", "tenant-a") is True


def test_object_key_prefix_mismatched_tenant() -> None:
    assert object_key_tenant_prefix_matches("tenants/tenant-b/images/1.jpg", "tenant-a") is False


def test_object_key_prefix_empty_ref() -> None:
    assert object_key_tenant_prefix_matches("", "tenant-a") is False
    assert object_key_tenant_prefix_matches(None, "tenant-a") is False


def test_object_key_prefix_key_missing_tenants_segment() -> None:
    assert object_key_tenant_prefix_matches("images/1.jpg", "tenant-a") is False


# ---------------------------------------------------------------------------
# Access-descriptor route — fixtures
# ---------------------------------------------------------------------------


class _FakeCloudProviderWithSignedUrl:
    """Extends the _FakeCloudProvider pattern (test_qiniu_media_serving.py)
    with get_download_url(), so the access-descriptor route's signed-URL
    path can be exercised without any real Qiniu SDK/network call."""

    def __init__(self, objects: dict, unavailable: bool = False, sign_error: Exception = None) -> None:
        self._objects = objects
        self._unavailable = unavailable
        self._sign_error = sign_error

    def supports_local_path(self) -> bool:
        return False

    def exists(self, storage_ref) -> bool:
        if self._unavailable:
            raise MediaStorageUnavailable("simulated provider outage")
        return bool(storage_ref) and storage_ref in self._objects

    def read_bytes(self, storage_ref: str) -> bytes:
        if storage_ref not in self._objects:
            raise MediaObjectNotFound("media object is missing")
        return self._objects[storage_ref]

    def get_local_path(self, storage_ref):
        return None

    def get_download_url(self, storage_ref: str, expires_in=None, deadline=None) -> str:
        if self._sign_error is not None:
            raise self._sign_error
        if not storage_ref:
            raise MediaObjectNotFound("media object is missing")
        return _FAKE_SIGNED_URL


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _authed(app, db_session, tenant_id):
    from app.auth import get_current_user
    from app.db.session import get_db

    def _db_gen():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: (object(), tenant_id)
    app.dependency_overrides[get_db] = _db_gen


def _patch_cloud_provider(monkeypatch, objects: dict, **kwargs) -> None:
    from app import media_storage
    from app.services import media_access

    provider = _FakeCloudProviderWithSignedUrl(objects, **kwargs)
    real_factory = media_storage.get_media_storage_provider

    def _fake_factory(storage_backend=None):
        if (storage_backend or media_storage.get_configured_write_backend_name()) == "qiniu_kodo":
            return provider
        return real_factory(storage_backend)

    monkeypatch.setattr(media_storage, "get_media_storage_provider", _fake_factory)
    monkeypatch.setattr(media_access, "get_media_storage_provider", _fake_factory)


def _access_url(conversation_id: str, msgid: str) -> str:
    return f"/api/conversations/{conversation_id}/messages/{msgid}/media/access"


# ---------------------------------------------------------------------------
# Local media -> access_type=proxy
# ---------------------------------------------------------------------------


def test_local_media_returns_proxy_descriptor(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.jpg"
    img_path.write_bytes(b"fake-jpeg-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    msg = _insert_message(
        db, msgid="msg-local-1", msgtype="image", sender="staff_a", roomid="roomL",
        sdkfileid="sdk-local-1", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-local-1", local_path=str(img_path))

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomL", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["storage_backend"] == "local"
    assert body["access_type"] == "proxy"
    assert body["url"] == f"/api/conversations/roomL/messages/{msg.msgid}/media"
    assert body["expires_at"] is None
    assert body["content_type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "no-store"


def test_local_media_never_generates_qiniu_signed_url(client, db, monkeypatch, tmp_path) -> None:
    """access_type must be "proxy", never "signed_url", for a local-backed
    row — even if Qiniu happens to be the deployment's default provider."""
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo2.jpg"
    img_path.write_bytes(b"fake-jpeg-bytes-2")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))
    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "qiniu_kodo")

    msg = _insert_message(
        db, msgid="msg-local-2", msgtype="image", sender="staff_a", roomid="roomL2",
        sdkfileid="sdk-local-2", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-local-2",
        local_path=str(img_path), storage_backend="local", storage_ref=str(img_path),
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomL2", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["access_type"] == "proxy"
    assert "media.example.com" not in resp.json()["url"]


# ---------------------------------------------------------------------------
# Qiniu media -> access_type=signed_url
# ---------------------------------------------------------------------------


def test_qiniu_media_returns_signed_url_descriptor(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/1.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"qiniu-jpeg-bytes"})
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "900")

    msg = _insert_message(
        db, msgid="msg-q-1", msgtype="image", sender="staff_a", roomid="roomQ",
        sdkfileid="sdk-q-1", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-q-1",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomQ", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["storage_backend"] == "qiniu_kodo"
    assert body["access_type"] == "signed_url"
    assert body["url"] == _FAKE_SIGNED_URL
    assert body["expires_at"] is not None
    assert body["media_id"] is not None
    assert resp.headers["cache-control"] == "no-store"


def test_qiniu_media_response_never_exposes_secrets_or_internal_fields(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/2.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"qiniu-jpeg-bytes"})

    msg = _insert_message(
        db, msgid="msg-q-2", msgtype="image", sender="staff_a", roomid="roomQ2",
        sdkfileid="sdk-q-2", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-q-2",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomQ2", msg.msgid))
        raw_text = resp.text
    finally:
        app.dependency_overrides.clear()

    body = resp.json()
    assert "storage_ref" not in body
    assert "local_path" not in body
    assert "sdkfileid" not in body
    assert key not in raw_text  # raw object key never returned
    assert "access_key" not in raw_text.lower()
    assert "secret_key" not in raw_text.lower()


# ---------------------------------------------------------------------------
# Permission tests
# ---------------------------------------------------------------------------


def test_unauthenticated_request_rejected(client, db) -> None:
    """No session cookie -> get_current_user itself raises 401, before the
    route body (and therefore before any media/tenant lookup) ever runs.
    get_db is overridden to the test session so the request reaches
    get_current_user's own auth check rather than failing on a missing
    real database connection."""
    from app.db.session import get_db
    from app.main import app

    def _db_gen():
        yield db

    app.dependency_overrides[get_db] = _db_gen
    try:
        resp = client.get(_access_url("roomAny", "msg-any"))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 401


def test_cross_tenant_access_denied(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-b/images/3.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"tenant-b-secret-bytes"})

    msg_b = _insert_message(
        db, msgid="msg-q-3", msgtype="image", sender="staff_b", roomid="roomQ3",
        sdkfileid="sdk-q-3", tenant_id=_TENANT_B, msgtime=100,
    )
    _insert_media_file(
        db, msg_b.id, _TENANT_B, "sdk-q-3",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomQ3", msg_b.msgid))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert "tenant-b-secret-bytes" not in resp.text
    assert key not in resp.text


def test_media_not_found_returns_404(client, db) -> None:
    from app.main import app

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("room-nonexistent", "msg-nonexistent"))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_media_file_tenant_mismatch_denied_even_with_matching_message(
    client, db, monkeypatch
) -> None:
    """Same RND-156 defense-in-depth as the raw media route: a media_files
    row mistagged to another tenant must be denied even though its parent
    message belongs to the authenticated tenant."""
    from app.main import app

    key = "tenants/tenant-b/images/4.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"should-not-be-served"})

    msg_a = _insert_message(
        db, msgid="msg-q-4", msgtype="image", sender="staff_a", roomid="roomQ4",
        sdkfileid="sdk-q-4", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg_a.id, _TENANT_B, "sdk-q-4",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomQ4", msg_a.msgid))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_object_key_tenant_prefix_mismatch_denied(client, db, monkeypatch) -> None:
    """A media_files row correctly tagged tenant_id=tenant-a, but whose
    storage_ref embeds a different tenant's key prefix (a data-integrity
    anomaly), must be denied signed-URL generation even though the row's
    own tenant_id column matches the authenticated tenant."""
    from app.main import app

    key = "tenants/tenant-b/images/5.jpg"  # wrong prefix for tenant-a
    _patch_cloud_provider(monkeypatch, {key: b"mismatched-prefix-bytes"})

    msg_a = _insert_message(
        db, msgid="msg-q-5", msgtype="image", sender="staff_a", roomid="roomQ5",
        sdkfileid="sdk-q-5", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg_a.id, _TENANT_A, "sdk-q-5",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomQ5", msg_a.msgid))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert "mismatched-prefix-bytes" not in resp.text
    assert key not in resp.text


def test_arbitrary_object_key_cannot_be_supplied_by_client(client, db, monkeypatch) -> None:
    """The route takes no object-key/storage_ref input at all — it is
    resolved entirely server-side from the authorized media_files row —
    so there is no request parameter through which a client could ever
    inject an arbitrary key."""
    from app.main import app

    key = "tenants/tenant-a/images/6.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"real-bytes"})

    msg = _insert_message(
        db, msgid="msg-q-6", msgtype="image", sender="staff_a", roomid="roomQ6",
        sdkfileid="sdk-q-6", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-q-6",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        # Attempting to smuggle an object key via a query param is simply
        # ignored — the route has no such parameter.
        resp = client.get(_access_url("roomQ6", msg.msgid) + "?object_key=tenants/tenant-b/images/other.jpg")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["url"] == _FAKE_SIGNED_URL


# ---------------------------------------------------------------------------
# Provider outage / signing failure -> route error mapping
# ---------------------------------------------------------------------------


def test_provider_outage_returns_503(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/7.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"bytes"}, unavailable=True)

    msg = _insert_message(
        db, msgid="msg-q-7", msgtype="image", sender="staff_a", roomid="roomQ7",
        sdkfileid="sdk-q-7", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-q-7",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomQ7", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503


def test_signed_url_generation_failure_returns_502(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/8.jpg"
    _patch_cloud_provider(
        monkeypatch, {key: b"bytes"}, sign_error=MediaStorageOperationError("signing failed")
    )

    msg = _insert_message(
        db, msgid="msg-q-8", msgtype="image", sender="staff_a", roomid="roomQ8",
        sdkfileid="sdk-q-8", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-q-8",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomQ8", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Logging redaction — signed URL / token must never appear in log records
# ---------------------------------------------------------------------------


def test_signed_url_never_appears_in_log_records(client, db, monkeypatch, caplog) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/9.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"qiniu-jpeg-bytes"})

    msg = _insert_message(
        db, msgid="msg-q-9", msgtype="image", sender="staff_a", roomid="roomQ9",
        sdkfileid="sdk-q-9", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-q-9",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        with caplog.at_level(logging.DEBUG):
            resp = client.get(_access_url("roomQ9", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    all_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert _FAKE_SIGNED_URL not in all_log_text
    assert "fake-signed-token-should-never-leak" not in all_log_text
    assert key not in all_log_text  # raw object key never logged either
