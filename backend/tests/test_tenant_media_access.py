"""
Tests for RND-156 — Multi-tenant media access isolation.

Validates:
  - Tenant A can download its own already-downloaded image media.
  - Tenant A cannot download Tenant B's media via the conversation-scoped
    media route, even with a syntactically valid msgid/conversation_id
    belonging to Tenant B (blocked upstream by _fetch_conversation_messages'
    tenant filter).
  - The media route's media_files lookup is *also* tenant-scoped
    independently of message ownership (RND-156 defense-in-depth): a
    media_files row whose tenant_id does not match the authenticated tenant
    is never served, even if the parent archive_messages row does belong to
    that tenant (a data-integrity edge case, not just the common path) —
    this proves media.tenant_id == current_admin.tenant_id is a real,
    load-bearing check, not decorative.
  - _load_media_files_map() (the timeline serializer's batch lookup) never
    returns another tenant's media_files row.
  - get_or_reset_media_file() (app/media_download.py — RND-199 unified pipeline)
    scopes creation/reset to (tenant_id, sdkfileid): two tenants get
    independent media_files rows even with a colliding sdkfileid, which
    would have hard-failed under the old global UNIQUE(sdkfileid)
    constraint.

Reuses the sqlite-backed schema/fixtures from test_reachability_audit.py.

Run (from backend/):
    pytest tests/test_tenant_media_access.py -v
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest

from app.db.models import MediaFile
from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)


def _insert_media_file(
    db,
    archive_message_id: int,
    tenant_id: str,
    sdkfileid: str,
    download_status: str = "downloaded",
    local_path: str = "/data/x.jpg",
    storage_backend: Optional[str] = None,
    storage_ref: Optional[str] = None,
) -> MediaFile:
    """storage_backend/storage_ref default to None so existing local-mode
    callers (passing only local_path) keep exercising the RND-174
    compatibility fallback (resolve_effective_storage_reference treats a
    populated local_path with no storage_backend as a legacy local row).
    Pass storage_backend="qiniu_kodo" explicitly to simulate a properly
    per-row-tagged Qiniu row — such a row must never also set local_path
    (see app.media_storage module docstring)."""
    mf = MediaFile(
        tenant_id=tenant_id,
        sdkfileid=sdkfileid,
        archive_message_id=archive_message_id,
        download_status=download_status,
        local_path=local_path,
        storage_backend=storage_backend,
        storage_ref=storage_ref,
        file_type="image",
    )
    db.add(mf)
    db.commit()
    db.refresh(mf)
    return mf


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

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), tenant_id)
    app.dependency_overrides[get_db] = _db_gen


# ---------------------------------------------------------------------------
# Media route — full-stack tenant isolation
# ---------------------------------------------------------------------------


def test_tenant_a_can_download_own_media(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.jpg"
    img_path.write_bytes(b"fake-jpeg-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    msg = _insert_message(
        db, msgid="msg-a-1", msgtype="image", sender="staff_a", roomid="roomX",
        sdkfileid="sdk-a-1", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-a-1", local_path=str(img_path))

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomX/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.content == b"fake-jpeg-bytes"


def test_tenant_a_cannot_download_tenant_b_media_via_conversation_route(
    client, db, monkeypatch, tmp_path
) -> None:
    """conversation_id belongs entirely to tenant B — blocked upstream by
    _fetch_conversation_messages before the media lookup ever runs."""
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "secret.jpg"
    img_path.write_bytes(b"tenant-b-secret-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    msg_b = _insert_message(
        db, msgid="msg-b-1", msgtype="image", sender="staff_b", roomid="roomX",
        sdkfileid="sdk-b-1", tenant_id=_TENANT_B, msgtime=100,
    )
    _insert_media_file(db, msg_b.id, _TENANT_B, "sdk-b-1", local_path=str(img_path))

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomX/messages/{msg_b.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert b"tenant-b-secret-bytes" not in resp.content


def test_media_route_rejects_media_file_tenant_mismatch_even_with_matching_message(
    client, db, monkeypatch, tmp_path
) -> None:
    """Defense-in-depth (RND-156): even if a media_files row's tenant_id
    somehow diverged from its parent archive_messages row's tenant_id (a
    data-integrity anomaly), the explicit MediaFile.tenant_id filter in the
    media route must still block it — isolation must not rely solely on
    message ownership transitively implying media ownership."""
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.jpg"
    img_path.write_bytes(b"should-not-be-served")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    # Message belongs to tenant A (so _fetch_conversation_messages finds it
    # for tenant A), but its media_files row is mistagged tenant B.
    msg_a = _insert_message(
        db, msgid="msg-a-2", msgtype="image", sender="staff_a", roomid="roomY",
        sdkfileid="sdk-a-2", tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg_a.id, _TENANT_B, "sdk-a-2", local_path=str(img_path))

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/roomY/messages/{msg_a.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert b"should-not-be-served" not in resp.content


# ---------------------------------------------------------------------------
# _load_media_files_map — timeline serializer batch lookup
# ---------------------------------------------------------------------------


def test_load_media_files_map_excludes_other_tenant_rows(db) -> None:
    from app.routers.conversations import _load_media_files_map

    msg_a = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-1",
        tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg_a.id, _TENANT_A, "sdk-1")

    msg_b = _insert_message(
        db, msgtype="image", sender="staff_b", sdkfileid="sdk-2",
        tenant_id=_TENANT_B, msgtime=100,
    )
    _insert_media_file(db, msg_b.id, _TENANT_B, "sdk-2")

    result_a = _load_media_files_map(db, _TENANT_A, [msg_a.id, msg_b.id])
    assert set(result_a.keys()) == {msg_a.id}

    result_b = _load_media_files_map(db, _TENANT_B, [msg_a.id, msg_b.id])
    assert set(result_b.keys()) == {msg_b.id}


# ---------------------------------------------------------------------------
# get_or_reset_media_file — download worker scoping
# ---------------------------------------------------------------------------


def test_get_or_reset_media_file_scoped_by_tenant_not_just_sdkfileid(db) -> None:
    """Two tenants whose WeCom corps happen to hand back the same sdkfileid
    must get independent media_files rows — under the old global
    UNIQUE(sdkfileid) constraint, the second tenant's insert would have
    failed outright, or a global lookup would have returned the first
    tenant's row."""
    from app.media_download import get_or_reset_media_file

    msg_a = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-shared",
        tenant_id=_TENANT_A, msgtime=100,
    )
    msg_b = _insert_message(
        db, msgtype="image", sender="staff_b", sdkfileid="sdk-shared",
        tenant_id=_TENANT_B, msgtime=100,
    )

    row_a = get_or_reset_media_file(db, _TENANT_A, "sdk-shared", msg_a.id)
    row_b = get_or_reset_media_file(db, _TENANT_B, "sdk-shared", msg_b.id)

    assert row_a is not None and row_b is not None
    assert row_a.id != row_b.id
    assert row_a.tenant_id == _TENANT_A
    assert row_b.tenant_id == _TENANT_B
    assert row_a.archive_message_id == msg_a.id
    assert row_b.archive_message_id == msg_b.id


# ---------------------------------------------------------------------------
# Migration/backfill coverage — media_files.tenant_id
# ---------------------------------------------------------------------------


def test_media_files_backfilled_to_default_tenant(db) -> None:
    """Historical media_files rows (tenant_id IS NULL, pre-dating RND-156)
    must be backfilled to the default tenant by
    bootstrap_default_tenant.py's generic _step_backfill() — the same
    function already used for archive_messages/archive_message_recipients/
    sync_states/contacts — and the tenant-scoped media lookup must find
    them immediately afterward."""
    from scripts.bootstrap_default_tenant import DEFAULT_TENANT_ID, _step_backfill

    from app.routers.conversations import _load_media_files_map

    msg = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-legacy",
        tenant_id=DEFAULT_TENANT_ID, msgtime=100,
    )
    # Simulate a pre-migration row: tenant_id not yet set.
    legacy_media = _insert_media_file(db, msg.id, None, "sdk-legacy")
    assert legacy_media.tenant_id is None

    _step_backfill(db, "media_files")

    db.refresh(legacy_media)
    assert legacy_media.tenant_id == DEFAULT_TENANT_ID

    result = _load_media_files_map(db, DEFAULT_TENANT_ID, [msg.id])
    assert msg.id in result
    assert result[msg.id].sdkfileid == "sdk-legacy"


def test_media_remains_accessible_after_backfill(client, db, monkeypatch, tmp_path) -> None:
    """End-to-end: a pre-migration media_files row with tenant_id NULL, once
    backfilled to the default tenant, must still be servable through the
    normal conversation-scoped media route — the "existing media remains
    accessible after migration" acceptance criterion."""
    from app.main import app
    from scripts.bootstrap_default_tenant import DEFAULT_TENANT_ID, _step_backfill

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "legacy.jpg"
    img_path.write_bytes(b"legacy-jpeg-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    msg = _insert_message(
        db, msgid="msg-legacy-1", msgtype="image", sender="staff_a", roomid="roomLegacy",
        sdkfileid="sdk-legacy-1", tenant_id=DEFAULT_TENANT_ID, msgtime=100,
    )
    _insert_media_file(db, msg.id, None, "sdk-legacy-1", local_path=str(img_path))

    _step_backfill(db, "media_files")

    _authed(app, db, DEFAULT_TENANT_ID)
    try:
        resp = client.get(f"/api/conversations/roomLegacy/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.content == b"legacy-jpeg-bytes"


def test_media_files_backfill_does_not_touch_already_tagged_rows(db) -> None:
    """_step_backfill only ever fills NULL tenant_id rows — a media_files
    row already correctly tagged for a real (non-default) tenant must be
    left untouched."""
    from scripts.bootstrap_default_tenant import DEFAULT_TENANT_ID, _step_backfill

    legacy_msg = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-legacy-2",
        tenant_id=DEFAULT_TENANT_ID, msgtime=100,
    )
    legacy_media = _insert_media_file(db, legacy_msg.id, None, "sdk-legacy-2")

    other_msg = _insert_message(
        db, msgtype="image", sender="staff_b", sdkfileid="sdk-other",
        tenant_id=_TENANT_B, msgtime=100,
    )
    other_media = _insert_media_file(db, other_msg.id, _TENANT_B, "sdk-other")

    _step_backfill(db, "media_files")

    db.refresh(legacy_media)
    db.refresh(other_media)
    assert legacy_media.tenant_id == DEFAULT_TENANT_ID
    assert other_media.tenant_id == _TENANT_B  # untouched, not overwritten


# ---------------------------------------------------------------------------
# Negative tests — stray cross-tenant media_files rows must not affect the
# download worker's candidate selection, repair selection, or count-only
# metrics (app/media_download.py — RND-199 unified pipeline).
# ---------------------------------------------------------------------------


def test_build_candidate_query_ignores_stray_other_tenant_downloaded_row(db) -> None:
    """A message with no media_files row of its own tenant, but a stray
    other-tenant row (same archive_message_id) marked "downloaded", must
    still be selected as a fresh candidate — the stray row must not make it
    look like this tenant already has a completed download."""
    from app.media_download import build_candidate_query

    msg_a = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-a",
        tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg_a.id, _TENANT_B, "sdk-b-stray", download_status="downloaded")

    candidates = build_candidate_query(db, _TENANT_A, {"image"}, retry=False).all()

    assert [m.id for m in candidates] == [msg_a.id]


def test_build_candidate_query_ignores_stray_other_tenant_pending_row(db) -> None:
    """A stray other-tenant "pending" row must not affect this tenant's
    candidate selection either — eligibility is judged purely on this
    tenant's own media_files row (or lack thereof)."""
    from app.media_download import build_candidate_query

    msg_a = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-a2",
        tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg_a.id, _TENANT_B, "sdk-b-stray2", download_status="pending")

    candidates = build_candidate_query(db, _TENANT_A, {"image"}, retry=False).all()

    assert [m.id for m in candidates] == [msg_a.id]


def test_build_candidate_query_excludes_message_with_own_tenant_downloaded_row(db) -> None:
    """Sanity/control: a message IS correctly excluded once this tenant's
    own media_files row says "downloaded" — proves the stray-row tests
    above are exercising real filtering, not a query that always returns
    everything."""
    from app.media_download import build_candidate_query

    msg_a = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-a-done",
        tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg_a.id, _TENANT_A, "sdk-a-done", download_status="downloaded")

    candidates = build_candidate_query(db, _TENANT_A, {"image"}, retry=False).all()

    assert candidates == []


def test_build_downloaded_repair_query_excludes_stray_other_tenant_row(db) -> None:
    """A "downloaded" media_files row belonging to another tenant must never
    surface in this tenant's stale-repair scan, even when it shares an
    archive_message_id with one of this tenant's own messages and this
    tenant has no media_files row of its own."""
    from app.media_download import build_downloaded_repair_query

    msg_a = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-a3",
        tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg_a.id, _TENANT_B, "sdk-b-stray3", download_status="downloaded")

    results = build_downloaded_repair_query(db, _TENANT_A, {"image"}).all()

    assert results == []


def test_build_downloaded_repair_query_finds_own_row_despite_stray_other_tenant_row(db) -> None:
    """Positive counterpart: tenant A's own "downloaded" row must still be
    found for repair scanning even when a stray tenant-B row exists for the
    same archive_message_id."""
    from app.media_download import build_downloaded_repair_query

    msg_a = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-a4",
        tenant_id=_TENANT_A, msgtime=100,
    )
    own_media = _insert_media_file(db, msg_a.id, _TENANT_A, "sdk-a4", download_status="downloaded")
    _insert_media_file(db, msg_a.id, _TENANT_B, "sdk-b-stray4", download_status="downloaded")

    results = build_downloaded_repair_query(db, _TENANT_A, {"image"}).all()

    assert len(results) == 1
    result_msg, result_media = results[0]
    assert result_msg.id == msg_a.id
    assert result_media.id == own_media.id


def test_count_candidates_with_existing_media_row_ignores_stray_other_tenant_row(db) -> None:
    """--count-only metric must not count a message as "already has a media
    row" on the strength of a stray other-tenant media_files row."""
    from app.media_download import (
        count_candidates_with_existing_media_row,
    )

    msg_a = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-a5",
        tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg_a.id, _TENANT_B, "sdk-b-stray5", download_status="downloaded")

    assert count_candidates_with_existing_media_row(db, _TENANT_A, {"image"}) == 0


def test_count_candidates_with_existing_media_row_counts_own_tenant_row(db) -> None:
    from app.media_download import (
        count_candidates_with_existing_media_row,
    )

    msg_a = _insert_message(
        db, msgtype="image", sender="staff_a", sdkfileid="sdk-a6",
        tenant_id=_TENANT_A, msgtime=100,
    )
    _insert_media_file(db, msg_a.id, _TENANT_A, "sdk-a6", download_status="downloaded")

    assert count_candidates_with_existing_media_row(db, _TENANT_A, {"image"}) == 1
