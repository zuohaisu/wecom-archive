"""
Tests for RND-187 Developer Acceptance fix round 2 — Cache-Control: no-store
must appear on EVERY response GET .../media/access can produce, success or
error, any status code — including a 401 raised by get_current_user() while
resolving dependencies, which runs before the route body and therefore
never touches the response.headers mutation made inside it.

Root cause (see MediaAccessNoStoreMiddleware in
backend/app/routers/conversations.py): the route set
response.headers["Cache-Control"] on the injected Response parameter, which
FastAPI only copies onto the final response when the route body actually
returns a value. Every error path instead goes through FastAPI/Starlette's
own exception-to-Response conversion, which builds a fresh Response that
never sees that mutation. The fix is a response-side middleware, scoped by
exact path match, that inspects the fully-built outgoing response for this
one endpoint and adds/overwrites the header regardless of how that response
was produced.

Reuses the fixtures/fakes from test_media_access_descriptor.py rather than
duplicating them — this file only adds Cache-Control-focused assertions on
top of scenarios that file already exercises for status code / body shape.

Run (from repo root, or from backend/ per every other test file's
convention):
    pytest backend/tests/test_media_access_cache_control.py -v
"""

from __future__ import annotations

from app.media_storage import MediaStorageOperationError
from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)
from tests.test_media_access_descriptor import (
    _FAKE_SIGNED_URL,
    _access_url,
    _authed,
    _patch_cloud_provider,
    client,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)
from tests.test_tenant_media_access import _insert_media_file

_SECRET_MARKERS = ("access_key", "secret_key", "ak=", "sk=")


def _assert_no_store_and_no_leak(resp, expected_status: int, forbidden_strings=()) -> None:
    assert resp.status_code == expected_status
    assert resp.headers["cache-control"] == "no-store"
    body_text = resp.text
    for marker in _SECRET_MARKERS:
        assert marker not in body_text.lower()
    for forbidden in forbidden_strings:
        assert forbidden not in body_text


# ---------------------------------------------------------------------------
# Success responses (200)
# ---------------------------------------------------------------------------


def test_local_media_200_has_no_store(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.jpg"
    img_path.write_bytes(b"fake-jpeg-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    msg = _insert_message(
        db, msgid="msg-cc-local-1", msgtype="image", sender="staff_a", roomid="roomCCLocal",
        sdkfileid="sdk-cc-local-1", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-cc-local-1", local_path=str(img_path))

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomCCLocal", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(resp, 200, forbidden_strings=(str(img_path),))


def test_qiniu_media_200_has_no_store(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/cc-1.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"qiniu-jpeg-bytes"})

    msg = _insert_message(
        db, msgid="msg-cc-q-1", msgtype="image", sender="staff_a", roomid="roomCCQ1",
        sdkfileid="sdk-cc-q-1", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-cc-q-1",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomCCQ1", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(resp, 200, forbidden_strings=(key,))
    assert resp.json()["url"] == _FAKE_SIGNED_URL


# ---------------------------------------------------------------------------
# Error responses — auth / tenant boundary
# ---------------------------------------------------------------------------


def test_unauthenticated_401_has_no_store(client, db) -> None:
    """No session cookie -> get_current_user() itself raises 401 during
    dependency resolution, *before* the route body (and its
    response.headers mutation) ever runs. get_db is overridden to the test
    session so the request reaches get_current_user's own auth check
    rather than failing on a missing real database connection."""
    from app.db.session import get_db
    from app.main import app

    def _db_gen():
        yield db

    app.dependency_overrides[get_db] = _db_gen
    try:
        resp = client.get(_access_url("roomAny", "msg-any"))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(resp, 401)


def test_cross_tenant_404_has_no_store(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-b/images/cc-2.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"tenant-b-secret-bytes"})

    msg_b = _insert_message(
        db, msgid="msg-cc-q-2", msgtype="image", sender="staff_b", roomid="roomCCQ2",
        sdkfileid="sdk-cc-q-2", tenant_id=_TENANT_B, msgtime=100,
    )
    _insert_media_file(
        db, msg_b.id, _TENANT_B, "sdk-cc-q-2",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomCCQ2", msg_b.msgid))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(
        resp, 404, forbidden_strings=(key, "tenant-b-secret-bytes")
    )


def test_media_not_found_404_has_no_store(client, db) -> None:
    from app.main import app

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("room-cc-nonexistent", "msg-cc-nonexistent"))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(resp, 404)


def test_media_file_tenant_mismatch_404_has_no_store(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-b/images/cc-3.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"should-not-be-served"})

    msg_a = _insert_message(
        db, msgid="msg-cc-q-3", msgtype="image", sender="staff_a", roomid="roomCCQ3",
        sdkfileid="sdk-cc-q-3", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg_a.id, _TENANT_B, "sdk-cc-q-3",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomCCQ3", msg_a.msgid))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(
        resp, 404, forbidden_strings=(key, "should-not-be-served")
    )


def test_object_key_tenant_prefix_mismatch_404_has_no_store(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-b/images/cc-4.jpg"  # wrong prefix for tenant-a
    _patch_cloud_provider(monkeypatch, {key: b"mismatched-prefix-bytes"})

    msg_a = _insert_message(
        db, msgid="msg-cc-q-4", msgtype="image", sender="staff_a", roomid="roomCCQ4",
        sdkfileid="sdk-cc-q-4", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg_a.id, _TENANT_A, "sdk-cc-q-4",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomCCQ4", msg_a.msgid))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(
        resp, 404, forbidden_strings=(key, "mismatched-prefix-bytes")
    )


# ---------------------------------------------------------------------------
# Error responses — provider / configuration failures
# ---------------------------------------------------------------------------


def test_provider_outage_503_has_no_store(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/cc-5.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"bytes"}, unavailable=True)

    msg = _insert_message(
        db, msgid="msg-cc-q-5", msgtype="image", sender="staff_a", roomid="roomCCQ5",
        sdkfileid="sdk-cc-q-5", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-cc-q-5",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomCCQ5", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(resp, 503, forbidden_strings=(key,))


def test_signed_url_generation_failure_502_has_no_store(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/cc-6.jpg"
    _patch_cloud_provider(
        monkeypatch, {key: b"bytes"}, sign_error=MediaStorageOperationError("signing failed")
    )

    msg = _insert_message(
        db, msgid="msg-cc-q-6", msgtype="image", sender="staff_a", roomid="roomCCQ6",
        sdkfileid="sdk-cc-q-6", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-cc-q-6",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomCCQ6", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(resp, 502, forbidden_strings=(key,))


def test_invalid_ttl_config_500_has_no_store(client, db, monkeypatch) -> None:
    """An out-of-range MEDIA_SIGNED_URL_TTL_SECONDS must still 500 with
    no-store — the misconfiguration-error path, not a provider failure."""
    from app.main import app

    key = "tenants/tenant-a/images/cc-7.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"bytes"})
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "99999")

    msg = _insert_message(
        db, msgid="msg-cc-q-7", msgtype="image", sender="staff_a", roomid="roomCCQ7",
        sdkfileid="sdk-cc-q-7", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-cc-q-7",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_access_url("roomCCQ7", msg.msgid))
    finally:
        app.dependency_overrides.clear()

    _assert_no_store_and_no_leak(resp, 500, forbidden_strings=(key,))


# ---------------------------------------------------------------------------
# Negative control — the raw byte-proxy route must NOT be affected
# ---------------------------------------------------------------------------


def test_raw_media_proxy_route_unaffected_by_no_store_middleware(
    client, db, monkeypatch, tmp_path
) -> None:
    """MediaAccessNoStoreMiddleware is scoped to .../media/access only — the
    existing .../media raw byte-proxy route must keep its prior (absent)
    Cache-Control behavior, proving the middleware's path-match scoping
    doesn't leak onto an unrelated endpoint."""
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.jpg"
    img_path.write_bytes(b"fake-jpeg-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    msg = _insert_message(
        db, msgid="msg-cc-proxy-1", msgtype="image", sender="staff_a", roomid="roomCCProxy",
        sdkfileid="sdk-cc-proxy-1", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-cc-proxy-1", local_path=str(img_path))

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomCCProxy/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "cache-control" not in resp.headers
