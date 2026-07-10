"""
Tests for RND-174 — serving Qiniu-backed media through the existing
controlled backend route, per-row storage resolution, and mixed local/
Qiniu storage in the same deployment.

Scope: GET /api/conversations/{conversation_id}/messages/{msgid}/media and
the timeline route, when a media_files row is Qiniu-backed
(storage_backend="qiniu_kodo", storage_ref=<object key>) — proving the
route's tenant authorization sequence (authenticate -> resolve tenant ->
tenant-scoped message/media lookup -> ownership check) still runs *before*
any provider call, that the provider is resolved from the row's own
storage_backend/storage_ref rather than the deployment-wide default write
provider (RND-174 QA fix), that a missing remote object 404s while a
provider outage does not, and that no Qiniu credential or provider error
text ever reaches the response.

Never calls the real Qiniu service — the storage provider used by the
route is swapped for an in-memory fake object store (see _FakeCloudProvider
below); the real QiniuStorageProvider SDK boundary is covered separately in
tests/test_qiniu_storage.py.

Run (from backend/):
    pytest tests/test_qiniu_media_serving.py -v
"""

from __future__ import annotations

import pytest

from app.media_storage import MediaObjectNotFound, MediaStorageUnavailable
from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)
from tests.test_tenant_media_access import _insert_media_file


class _FakeCloudProvider:
    """Stand-in for QiniuStorageProvider: an in-memory object store
    implementing just the MediaStorageProvider surface the route and
    resolve_image_file_state() actually use — including the RND-174 error
    classification (MediaObjectNotFound vs MediaStorageUnavailable)."""

    def __init__(self, objects: dict, unavailable: bool = False) -> None:
        self._objects = objects
        self._unavailable = unavailable

    def supports_local_path(self) -> bool:
        return False

    def exists(self, storage_ref) -> bool:
        if self._unavailable:
            raise MediaStorageUnavailable("simulated provider outage")
        return bool(storage_ref) and storage_ref in self._objects

    def read_bytes(self, storage_ref: str) -> bytes:
        if self._unavailable:
            raise MediaStorageUnavailable("simulated provider outage")
        if storage_ref not in self._objects:
            raise MediaObjectNotFound("media object is missing")
        return self._objects[storage_ref]

    def get_local_path(self, storage_ref):
        return None


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


def _patch_cloud_provider(monkeypatch, objects: dict, unavailable: bool = False) -> None:
    """Make both call sites that resolve a provider for the "qiniu_kodo"
    backend (app.media_storage.resolve_image_file_state's internal lookup,
    and the media route's own direct lookup) return the same fake provider
    — while leaving the "local" backend's real LocalStorageProvider
    resolution untouched, so a mixed local+Qiniu deployment can be
    exercised in the same test (see the mixed-storage tests below)."""
    from app import media_storage
    from app.routers import conversations as conv

    provider = _FakeCloudProvider(objects, unavailable=unavailable)
    real_factory = media_storage.get_media_storage_provider

    def _fake_factory(storage_backend=None):
        if (storage_backend or media_storage.get_configured_write_backend_name()) == "qiniu_kodo":
            return provider
        return real_factory(storage_backend)

    monkeypatch.setattr(media_storage, "get_media_storage_provider", _fake_factory)
    monkeypatch.setattr(conv, "get_media_storage_provider", _fake_factory)


# ---------------------------------------------------------------------------
# Positive: authenticated same-tenant retrieval, per-row backend resolution
# ---------------------------------------------------------------------------


def test_same_tenant_can_retrieve_qiniu_backed_media(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/1.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"qiniu-jpeg-bytes"})

    msg = _insert_message(
        db, msgid="msg-a-q1", msgtype="image", sender="staff_a", roomid="roomQ",
        sdkfileid="sdk-a-q1", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-a-q1",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.content == b"qiniu-jpeg-bytes"
    assert resp.headers["content-type"] == "image/jpeg"


def test_response_shape_matches_local_provider_contract(client, db, monkeypatch) -> None:
    """Frontend compatibility: same URL shape, plain image bytes, no
    content-disposition — the local-provider route behavior is unchanged."""
    from app.main import app

    key = "tenants/tenant-a/images/2.png"
    _patch_cloud_provider(monkeypatch, {key: b"\x89PNG\r\n\x1a\nfake-png"})

    msg = _insert_message(
        db, msgid="msg-a-q2", msgtype="image", sender="staff_a", roomid="roomQ2",
        sdkfileid="sdk-a-q2", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-a-q2",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ2/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "content-disposition" not in resp.headers


def test_qiniu_row_local_path_never_used_as_storage_reference(client, db, monkeypatch) -> None:
    """RND-174 QA fix: a Qiniu row's storage_ref is authoritative — even if
    local_path somehow also holds a stray/stale value, it must never be
    treated as the Qiniu object key."""
    from app.main import app

    real_key = "tenants/tenant-a/images/7.jpg"
    _patch_cloud_provider(monkeypatch, {real_key: b"real-qiniu-bytes"})

    msg = _insert_message(
        db, msgid="msg-a-q7", msgtype="image", sender="staff_a", roomid="roomQ7",
        sdkfileid="sdk-a-q7", tenant_id=_TENANT_A, msgtime=100,
    )
    # local_path deliberately set to something that is NOT a valid key in
    # the fake bucket — if the route ever fell back to local_path instead
    # of storage_ref, this would 404 instead of returning the real bytes.
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-a-q7",
        local_path="/not/a/real/qiniu/key.jpg",
        storage_backend="qiniu_kodo", storage_ref=real_key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ7/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.content == b"real-qiniu-bytes"


# ---------------------------------------------------------------------------
# Negative: cross-tenant denial (authorization runs before any provider call)
# ---------------------------------------------------------------------------


def test_cross_tenant_request_denied_for_qiniu_backed_media(client, db, monkeypatch) -> None:
    """conversation_id belongs entirely to tenant B — blocked upstream by
    _fetch_conversation_messages before the media_files lookup or any
    provider call runs, same as the local-provider case."""
    from app.main import app

    key = "tenants/tenant-b/images/3.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"tenant-b-secret-bytes"})

    msg_b = _insert_message(
        db, msgid="msg-b-q1", msgtype="image", sender="staff_b", roomid="roomQ3",
        sdkfileid="sdk-b-q1", tenant_id=_TENANT_B, msgtime=100,
    )
    _insert_media_file(
        db, msg_b.id, _TENANT_B, "sdk-b-q1",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ3/messages/{msg_b.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert b"tenant-b-secret-bytes" not in resp.content


def test_media_file_tenant_mismatch_denied_even_with_matching_message(
    client, db, monkeypatch
) -> None:
    """Defense-in-depth (RND-156): a media_files row mistagged to another
    tenant must still be blocked by the explicit MediaFile.tenant_id filter,
    even for Qiniu-backed media, even though the object itself exists and is
    readable."""
    from app.main import app

    key = "tenants/tenant-b/images/4.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"should-not-be-served"})

    msg_a = _insert_message(
        db, msgid="msg-a-q3", msgtype="image", sender="staff_a", roomid="roomQ4",
        sdkfileid="sdk-a-q3", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg_a.id, _TENANT_B, "sdk-a-q3",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ4/messages/{msg_a.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert b"should-not-be-served" not in resp.content


# ---------------------------------------------------------------------------
# Negative: object confirmed missing from the remote bucket -> 404
# ---------------------------------------------------------------------------


def test_missing_remote_object_returns_controlled_404(client, db, monkeypatch) -> None:
    """download_status says "downloaded" but the object is not actually in
    the (fake) bucket — must be a plain 404, not a 500 or a raw SDK error."""
    from app.main import app

    _patch_cloud_provider(monkeypatch, {})  # empty bucket

    msg = _insert_message(
        db, msgid="msg-a-q4", msgtype="image", sender="staff_a", roomid="roomQ5",
        sdkfileid="sdk-a-q4", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-a-q4",
        local_path=None, storage_backend="qiniu_kodo", storage_ref="tenants/tenant-a/images/5.jpg",
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ5/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# RND-174 QA fix #5: a provider outage must not be reported as missing media
# ---------------------------------------------------------------------------


def test_provider_outage_returns_503_not_404(client, db, monkeypatch) -> None:
    from app.main import app

    _patch_cloud_provider(monkeypatch, {"tenants/tenant-a/images/8.jpg": b"x"}, unavailable=True)

    msg = _insert_message(
        db, msgid="msg-a-q8", msgtype="image", sender="staff_a", roomid="roomQ8",
        sdkfileid="sdk-a-q8", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-a-q8",
        local_path=None, storage_backend="qiniu_kodo", storage_ref="tenants/tenant-a/images/8.jpg",
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ8/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503
    assert resp.status_code != 404


def test_provider_outage_response_never_exposes_raw_error(client, db, monkeypatch) -> None:
    from app.main import app

    _patch_cloud_provider(monkeypatch, {}, unavailable=True)

    msg = _insert_message(
        db, msgid="msg-a-q9", msgtype="image", sender="staff_a", roomid="roomQ9",
        sdkfileid="sdk-a-q9", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-a-q9",
        local_path=None, storage_backend="qiniu_kodo", storage_ref="tenants/tenant-a/images/9.jpg",
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ9/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503
    assert "simulated provider outage" not in resp.text
    assert "MediaStorageUnavailable" not in resp.text


def test_unknown_storage_backend_returns_controlled_server_error(client, db, monkeypatch) -> None:
    """A row naming an unsupported/garbage storage_backend must fail
    clearly and safely (500), not crash with a raw traceback and not be
    reported as a plain 404."""
    from app.main import app

    msg = _insert_message(
        db, msgid="msg-a-q10", msgtype="image", sender="staff_a", roomid="roomQ10",
        sdkfileid="sdk-a-q10", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-a-q10",
        local_path=None, storage_backend="not_a_real_backend", storage_ref="whatever",
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ10/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 500
    assert "not_a_real_backend" not in resp.text


# ---------------------------------------------------------------------------
# Mixed storage: local and Qiniu rows served correctly in the same
# deployment, regardless of the current default write provider
# ---------------------------------------------------------------------------


def test_mixed_local_and_qiniu_rows_both_servable_in_same_deployment(
    client, db, monkeypatch, tmp_path
) -> None:
    """A local row and a Qiniu row must each be served through their own
    recorded provider in the same request cycle — proving provider
    selection is genuinely per-row, not a single global choice."""
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    local_img = tmp_path / "local.jpg"
    local_img.write_bytes(b"local-bytes")

    qiniu_key = "tenants/tenant-a/images/11.jpg"
    _patch_cloud_provider(monkeypatch, {qiniu_key: b"qiniu-bytes"})

    local_msg = _insert_message(
        db, msgid="msg-a-local", msgtype="image", sender="staff_a", roomid="roomMixedLocal",
        sdkfileid="sdk-a-local", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, local_msg.id, _TENANT_A, "sdk-a-local",
        local_path=str(local_img), storage_backend="local", storage_ref=str(local_img),
    )

    qiniu_msg = _insert_message(
        db, msgid="msg-a-qiniu", msgtype="image", sender="staff_a", roomid="roomMixedQiniu",
        sdkfileid="sdk-a-qiniu", tenant_id=_TENANT_A, msgtime=200,
    )
    _insert_media_file(
        db, qiniu_msg.id, _TENANT_A, "sdk-a-qiniu",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=qiniu_key,
    )

    _authed(app, db, _TENANT_A)
    try:
        local_resp = client.get(f"/api/conversations/roomMixedLocal/messages/{local_msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    _authed(app, db, _TENANT_A)
    try:
        qiniu_resp = client.get(f"/api/conversations/roomMixedQiniu/messages/{qiniu_msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert local_resp.status_code == 200
    assert local_resp.content == b"local-bytes"
    assert qiniu_resp.status_code == 200
    assert qiniu_resp.content == b"qiniu-bytes"


def test_existing_local_row_still_accessible_after_switching_default_to_qiniu(
    client, db, monkeypatch, tmp_path
) -> None:
    """RND-174 QA fix (core requirement): switching MEDIA_STORAGE_PROVIDER
    to qiniu_kodo (for new writes) must not reinterpret an existing local
    row — it keeps being served via LocalStorageProvider because its own
    storage_backend still says "local"."""
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    local_img = tmp_path / "legacy.jpg"
    local_img.write_bytes(b"legacy-local-bytes")

    # Simulate the default write provider having been switched to Qiniu
    # for new media, while this row predates that switch.
    _patch_cloud_provider(monkeypatch, {})
    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "qiniu_kodo")

    msg = _insert_message(
        db, msgid="msg-a-legacy-local", msgtype="image", sender="staff_a", roomid="roomLegacy",
        sdkfileid="sdk-a-legacy-local", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-a-legacy-local",
        local_path=str(local_img), storage_backend="local", storage_ref=str(local_img),
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomLegacy/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.content == b"legacy-local-bytes"


# ---------------------------------------------------------------------------
# Security: no credential/provider internals ever reach the client
# ---------------------------------------------------------------------------


def test_qiniu_credentials_and_object_key_never_exposed_in_response(client, db, monkeypatch) -> None:
    from app.main import app

    key = "tenants/tenant-a/images/6.jpg"
    _patch_cloud_provider(monkeypatch, {key: b"jpeg-bytes"})

    msg = _insert_message(
        db, msgid="msg-a-q5", msgtype="image", sender="staff_a", roomid="roomQ6",
        sdkfileid="sdk-a-q5-secret", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-a-q5-secret",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomQ6/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert b"sdk-a-q5-secret" not in resp.content
    assert key.encode() not in resp.content
    for header_value in resp.headers.values():
        assert "sdk-a-q5-secret" not in header_value
        assert key not in header_value


# ---------------------------------------------------------------------------
# RND-174 QA fix #2 (second QA pass): the timeline must distinguish a
# provider outage from confirmed-missing media — folding
# MediaStorageUnavailable into "missing" misreported a transient Qiniu
# outage as media_file_missing_on_disk. GET /api/conversations/{id}/messages
# is exercised directly (not the /media route) for all of these.
# ---------------------------------------------------------------------------


def test_timeline_confirmed_missing_qiniu_object_reports_missing(client, db, monkeypatch) -> None:
    """download_status says "downloaded" but the object is not in the
    (fake) bucket — unchanged behavior: media_file_missing_on_disk, no
    media_url."""
    from app.main import app

    _patch_cloud_provider(monkeypatch, {})  # empty bucket

    msg = _insert_message(
        db, msgid="msg-tl-missing", msgtype="image", sender="staff_a", roomid="roomTlMissing",
        sdkfileid="sdk-tl-missing", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-tl-missing",
        local_path=None, storage_backend="qiniu_kodo",
        storage_ref="tenants/tenant-a/images/missing.jpg",
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomTlMissing/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    out = resp.json()["messages"][0]
    assert out["media_status"] == "failed"
    assert out["unsupported_reason"] == "media_file_missing_on_disk"
    assert out["media_url"] is None


def test_timeline_provider_unavailable_reports_unavailable_not_missing(
    client, db, monkeypatch
) -> None:
    """RND-174 QA fix (second pass): a Qiniu outage must be reported as
    "unavailable", never as media_file_missing_on_disk — a temporary
    storage outage is not equivalent to the media actually being gone."""
    from app.main import app

    _patch_cloud_provider(monkeypatch, {}, unavailable=True)

    msg = _insert_message(
        db, msgid="msg-tl-unavail", msgtype="image", sender="staff_a", roomid="roomTlUnavail",
        sdkfileid="sdk-tl-unavail", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-tl-unavail",
        local_path=None, storage_backend="qiniu_kodo",
        storage_ref="tenants/tenant-a/images/unavail.jpg",
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomTlUnavail/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    out = resp.json()["messages"][0]
    assert out["media_status"] == "unavailable"
    assert out["unsupported_reason"] == "media_storage_unavailable"
    assert out["unsupported_reason"] != "media_file_missing_on_disk"
    assert out["media_url"] is None


def test_timeline_unknown_storage_backend_reports_unavailable_not_500(
    client, db, monkeypatch
) -> None:
    """A row naming an unsupported storage_backend must degrade this one
    row to "unavailable" in the timeline — the endpoint itself must not
    500 (that would fail the whole page for one bad row)."""
    from app.main import app

    msg = _insert_message(
        db, msgid="msg-tl-badbackend", msgtype="image", sender="staff_a", roomid="roomTlBad",
        sdkfileid="sdk-tl-badbackend", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-tl-badbackend",
        local_path=None, storage_backend="not_a_real_backend", storage_ref="whatever",
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomTlBad/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    out = resp.json()["messages"][0]
    assert out["media_status"] == "unavailable"
    assert "not_a_real_backend" not in resp.text


def test_timeline_local_provider_missing_file_behaves_same_as_before(
    client, db, monkeypatch, tmp_path
) -> None:
    """Regression: a local-backed row whose file is missing on disk must
    still report media_file_missing_on_disk — the new "unavailable" state
    is additive and must not change local-provider behavior."""
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))

    msg = _insert_message(
        db, msgid="msg-tl-local-missing", msgtype="image", sender="staff_a", roomid="roomTlLocalMissing",
        sdkfileid="sdk-tl-local-missing", tenant_id=_TENANT_A, msgtime=100,
    )
    missing_path = str(tmp_path / "nope.jpg")
    _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-tl-local-missing",
        local_path=missing_path, storage_backend="local", storage_ref=missing_path,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomTlLocalMissing/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    out = resp.json()["messages"][0]
    assert out["media_status"] == "failed"
    assert out["unsupported_reason"] == "media_file_missing_on_disk"


def test_timeline_mixed_local_and_qiniu_conversation_both_classified_correctly(
    client, db, monkeypatch, tmp_path
) -> None:
    """A local row and a Qiniu row in the same conversation must each be
    classified correctly through the timeline — proving per-row provider
    resolution end to end, not just at the /media route."""
    from app.main import app

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    local_img = tmp_path / "mixed-local.jpg"
    local_img.write_bytes(b"local-bytes")

    qiniu_key = "tenants/tenant-a/images/mixed.jpg"
    _patch_cloud_provider(monkeypatch, {qiniu_key: b"qiniu-bytes"})

    local_msg = _insert_message(
        db, msgid="msg-tl-mixed-local", msgtype="image", sender="staff_a", roomid="roomTlMixed",
        sdkfileid="sdk-tl-mixed-local", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(
        db, local_msg.id, _TENANT_A, "sdk-tl-mixed-local",
        local_path=str(local_img), storage_backend="local", storage_ref=str(local_img),
    )

    qiniu_msg = _insert_message(
        db, msgid="msg-tl-mixed-qiniu", msgtype="image", sender="staff_a", roomid="roomTlMixed",
        sdkfileid="sdk-tl-mixed-qiniu", tenant_id=_TENANT_A, msgtime=200,
    )
    _insert_media_file(
        db, qiniu_msg.id, _TENANT_A, "sdk-tl-mixed-qiniu",
        local_path=None, storage_backend="qiniu_kodo", storage_ref=qiniu_key,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomTlMixed/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    messages = {m["msgid"]: m for m in resp.json()["messages"]}
    assert messages["msg-tl-mixed-local"]["media_status"] == "available"
    assert messages["msg-tl-mixed-qiniu"]["media_status"] == "available"


def test_timeline_conversation_still_loads_during_qiniu_outage(client, db, monkeypatch) -> None:
    """The conversation must still load (200, other messages intact) when
    Qiniu is unavailable — only the affected image message degrades."""
    from app.main import app

    _patch_cloud_provider(monkeypatch, {}, unavailable=True)

    text_msg = _insert_message(
        db, msgid="msg-tl-outage-text", msgtype="text", sender="staff_a", roomid="roomTlOutage",
        content_text="hello", tenant_id=_TENANT_A, msgtime=100,
    )
    image_msg = _insert_message(
        db, msgid="msg-tl-outage-image", msgtype="image", sender="staff_a", roomid="roomTlOutage",
        sdkfileid="sdk-tl-outage-image", tenant_id=_TENANT_A, msgtime=200,
    )
    _insert_media_file(
        db, image_msg.id, _TENANT_A, "sdk-tl-outage-image",
        local_path=None, storage_backend="qiniu_kodo",
        storage_ref="tenants/tenant-a/images/outage.jpg",
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomTlOutage/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    messages = {m["msgid"]: m for m in resp.json()["messages"]}
    assert messages["msg-tl-outage-text"]["media_type"] == "text"
    assert messages["msg-tl-outage-image"]["media_status"] == "unavailable"
