"""
Tests for RND-201 — conversation timeline API contract for revoke
association.

Full-stack tests: a real sqlite-backed DB session + FastAPI TestClient
hitting GET /api/conversations/{id}/messages, so
app.routers.conversations._load_revocations_map's actual SQL (including
its OR-across-two-columns filter) executes for real rather than being
mocked out — same pattern as test_tenant_media_access.py /
test_nested_media_access.py.

Fixtures are built by calling app.revoke_reconciliation.reconcile_
revoke_event directly (the same function the real decrypt pipeline
calls), not by hand-setting is_revoked/message_revocations rows, so
these tests exercise the real integration between the reconciler and the
timeline serializer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.db.models import MediaFile
from app.revoke_reconciliation import PENDING_TO_MISSING_THRESHOLD, reconcile_revoke_event
from tests.test_reachability_audit import _TENANT_A, _TENANT_B, _insert_message, db  # noqa: F401 -- pytest fixture, must be imported to be discovered


def _revoke_structured_content(pre_msgid: "str | None") -> dict:
    if not pre_msgid:
        return {"fields": None, "raw": {}, "parse_warnings": ["missing_pre_msgid"]}
    return {"fields": {"pre_msgid": pre_msgid}, "raw": {"pre_msgid": pre_msgid}, "parse_warnings": []}


def _insert_revoke_event(db, *, pre_msgid, tenant_id=_TENANT_A, msgtime=5000, **kwargs):
    return _insert_message(
        db,
        msgtype="revoke",
        structured_content=_revoke_structured_content(pre_msgid),
        tenant_id=tenant_id,
        msgtime=msgtime,
        **kwargs,
    )


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


def _get_messages(client, app, db, tenant_id, conversation_id):
    from app.main import app as real_app

    _authed(real_app, db, tenant_id)
    try:
        resp = client.get(f"/api/conversations/{conversation_id}/messages")
    finally:
        real_app.dependency_overrides.clear()
    return resp


# ---------------------------------------------------------------------------
# Original-first: original exists, matching revoke is linked
# ---------------------------------------------------------------------------


def test_linked_original_keeps_content_and_position_no_duplicate_row(client, db) -> None:
    from app.main import app

    _insert_message(
        db, msgid="orig-1", msgtype="text", content_text="hello there",
        sender="staff_a", roomid="room1", msgtime=1000, seq=1, tenant_id=_TENANT_A,
    )
    revoke_event = _insert_revoke_event(db, pre_msgid="orig-1", roomid="room1", seq=2)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    resp = _get_messages(client, app, db, _TENANT_A, "room1")
    assert resp.status_code == 200
    messages = resp.json()["messages"]

    # Exactly one visible row -- the original, not a second standalone
    # revoke row.
    assert len(messages) == 1
    m = messages[0]
    assert m["msgid"] == "orig-1"
    assert m["content_text"] == "hello there"
    assert m["sender"] == "staff_a"
    assert m["is_revoked"] is True
    assert m["revoke_association_status"] == "linked"
    assert m["revoke_event_msgid"] == revoke_event.msgid
    assert m["revoked_at"] == 5000


def test_revoked_structured_message_keeps_structured_content(client, db) -> None:
    from app.main import app

    _insert_message(
        db, msgid="orig-loc", msgtype="location", roomid="room2",
        structured_content={"fields": {"address": "1600 Amphitheatre"}, "raw": {}, "parse_warnings": []},
        msgtime=1000, seq=1, tenant_id=_TENANT_A,
    )
    revoke_event = _insert_revoke_event(db, pre_msgid="orig-loc", roomid="room2", seq=2)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    resp = _get_messages(client, app, db, _TENANT_A, "room2")
    messages = resp.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["is_revoked"] is True
    assert messages[0]["structured_content"]["fields"] == {"address": "1600 Amphitheatre"}


# ---------------------------------------------------------------------------
# Revoke-first: standalone pending / original_missing / malformed rows
# ---------------------------------------------------------------------------


def test_pending_revoke_shown_standalone_with_no_fabricated_content(client, db) -> None:
    from app.main import app

    revoke_event = _insert_revoke_event(db, pre_msgid="not-here-yet", roomid="room3", seq=1)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    resp = _get_messages(client, app, db, _TENANT_A, "room3")
    messages = resp.json()["messages"]
    assert len(messages) == 1
    m = messages[0]
    assert m["msgid"] == revoke_event.msgid
    assert m["is_revoked"] is False
    assert m["revoke_association_status"] == "pending"
    assert m["content_text"] is None


def test_original_missing_after_aging_threshold(client, db) -> None:
    from app.main import app

    revoke_event = _insert_revoke_event(db, pre_msgid="ghost", roomid="room4", seq=1)
    revocation = reconcile_revoke_event(db, revoke_event)
    db.commit()

    # Age the association past the display-only threshold.
    old_created_at = datetime.now(timezone.utc) - PENDING_TO_MISSING_THRESHOLD - timedelta(hours=1)
    revocation.created_at = old_created_at
    db.commit()

    resp = _get_messages(client, app, db, _TENANT_A, "room4")
    messages = resp.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["revoke_association_status"] == "original_missing"
    assert messages[0]["content_text"] is None

    # The persisted MessageRevocation status is still "pending" -- still
    # eligible for reconciliation if the original ever shows up.
    db.refresh(revocation)
    assert revocation.status == "pending"


def test_malformed_revoke_event_shown_with_malformed_status(client, db) -> None:
    from app.main import app

    revoke_event = _insert_revoke_event(db, pre_msgid=None, roomid="room5", seq=1)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    resp = _get_messages(client, app, db, _TENANT_A, "room5")
    messages = resp.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["revoke_association_status"] == "malformed"
    assert messages[0]["content_text"] is None


# ---------------------------------------------------------------------------
# Ordering / no duplicate rows
# ---------------------------------------------------------------------------


def test_revoked_original_stays_at_its_chronological_position(client, db) -> None:
    from app.main import app

    _insert_message(db, msgid="m1", msgtype="text", content_text="first", roomid="room6", msgtime=100, seq=1, tenant_id=_TENANT_A)
    _insert_message(db, msgid="m2", msgtype="text", content_text="second (will be revoked)", roomid="room6", msgtime=200, seq=2, tenant_id=_TENANT_A)
    _insert_message(db, msgid="m3", msgtype="text", content_text="third", roomid="room6", msgtime=300, seq=3, tenant_id=_TENANT_A)
    revoke_event = _insert_revoke_event(db, pre_msgid="m2", roomid="room6", msgtime=500, seq=4)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    resp = _get_messages(client, app, db, _TENANT_A, "room6")
    messages = resp.json()["messages"]

    # 3 original messages visible, the standalone revoke event folded away
    # -- never a 4th/duplicate row.
    assert [m["msgid"] for m in messages] == ["m1", "m2", "m3"]
    assert messages[1]["is_revoked"] is True
    assert messages[1]["content_text"] == "second (will be revoked)"


def test_pagination_never_duplicates_or_skips_messages_when_a_page_boundary_folds_a_revoke_row(
    client, db
) -> None:
    """RND-201 round 2 QA finding: paginate -> fold linked revoke rows ->
    visible rows can mean a page returns fewer than `limit` visible
    messages when one of the `limit` underlying rows gets folded away
    (documented, accepted trade-off -- see get_conversation_messages()'s
    docstring/comments; not redesigned in this ticket per explicit
    instruction to avoid pagination-architecture changes unless the fix
    is trivial and non-architectural). What MUST still hold regardless,
    and what this test actually verifies: walking every page via
    pagination.next_before, oldest to newest, produces exactly the full
    set of non-folded messages, in order, with no duplicates and no
    silently-skipped (neither shown nor accounted for) messages.
    """
    from app.main import app

    # 5 plain messages plus one revoke event that folds into m3, spread
    # across a `limit=3` page boundary deliberately (m3/revoke sit right
    # at the boundary between the newest and next-older page).
    for i in range(1, 6):
        _insert_message(
            db, msgid=f"m{i}", msgtype="text", content_text=f"message {i}",
            roomid="room-pagination", msgtime=i * 100, seq=i, tenant_id=_TENANT_A,
        )
    revoke_event = _insert_revoke_event(db, pre_msgid="m3", roomid="room-pagination", msgtime=550, seq=6)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    seen_msgids = []
    before = None
    for _ in range(10):  # generous upper bound on page-walk iterations
        from app.auth import get_current_user
        from app.db.session import get_db
        from unittest.mock import MagicMock

        def _db_gen():
            yield db

        app.dependency_overrides[get_current_user] = lambda: (MagicMock(), _TENANT_A)
        app.dependency_overrides[get_db] = _db_gen
        try:
            url = "/api/conversations/room-pagination/messages?limit=3"
            if before is not None:
                url += f"&before={before}"
            resp = client.get(url)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        page_msgids = [m["msgid"] for m in data["messages"]]
        seen_msgids = page_msgids + seen_msgids  # pages arrive newest-window-first
        if not data["pagination"]["has_older"]:
            break
        before = data["pagination"]["next_before"]

    # Every non-revoke message shows up exactly once, in chronological
    # order, and the linked revoke event never appears as its own entry.
    assert seen_msgids == ["m1", "m2", "m3", "m4", "m5"]
    assert revoke_event.msgid not in seen_msgids


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_revoke_never_crosses_tenant_in_timeline_api(client, db) -> None:
    from app.main import app

    shared_msgid = "cross-tenant-collision"
    _insert_message(db, msgid=shared_msgid, msgtype="text", content_text="tenant a", roomid="roomX", msgtime=100, seq=1, tenant_id=_TENANT_A)
    _insert_message(db, msgid=shared_msgid, msgtype="text", content_text="tenant b", roomid="roomX", msgtime=100, seq=1, tenant_id=_TENANT_B)

    revoke_event = _insert_revoke_event(db, pre_msgid=shared_msgid, roomid="roomX", tenant_id=_TENANT_A, seq=2)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    resp_a = _get_messages(client, app, db, _TENANT_A, "roomX")
    messages_a = resp_a.json()["messages"]
    assert len(messages_a) == 1
    assert messages_a[0]["is_revoked"] is True
    assert messages_a[0]["content_text"] == "tenant a"

    resp_b = _get_messages(client, app, db, _TENANT_B, "roomX")
    messages_b = resp_b.json()["messages"]
    assert len(messages_b) == 1
    assert messages_b[0]["is_revoked"] is False
    assert messages_b[0]["content_text"] == "tenant b"


# ---------------------------------------------------------------------------
# Media preservation via the API
# ---------------------------------------------------------------------------


def test_revoked_image_message_media_access_unaffected(client, db, monkeypatch, tmp_path) -> None:
    from app.main import app

    media_root = tmp_path / "media"
    media_root.mkdir()
    img_path = media_root / "photo.jpg"
    img_path.write_bytes(b"fake-jpeg-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    original = _insert_message(
        db, msgid="img-1", msgtype="image", roomid="room7", sdkfileid="sdk-img-1",
        msgtime=100, seq=1, tenant_id=_TENANT_A,
    )
    media = MediaFile(
        sdkfileid="sdk-img-1", archive_message_id=original.id, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(img_path),
        local_path=str(img_path), file_type="image",
    )
    db.add(media)
    db.commit()

    revoke_event = _insert_revoke_event(db, pre_msgid="img-1", roomid="room7", seq=2)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    resp = _get_messages(client, app, db, _TENANT_A, "room7")
    messages = resp.json()["messages"]
    assert len(messages) == 1
    m = messages[0]
    assert m["is_revoked"] is True
    # Media status/url is fully unaffected by the revoke -- still
    # available, still fetchable via the same route as before.
    assert m["media_status"] == "available"
    assert m["media_url"] is not None

    _authed(app, db, _TENANT_A)
    try:
        media_resp = client.get(m["media_url"])
    finally:
        app.dependency_overrides.clear()
    assert media_resp.status_code == 200
    assert media_resp.content == b"fake-jpeg-bytes"


# ---------------------------------------------------------------------------
# Ordinary messages unaffected
# ---------------------------------------------------------------------------


def test_ordinary_message_has_no_revoke_fields_set(client, db) -> None:
    from app.main import app

    _insert_message(db, msgid="plain-1", msgtype="text", content_text="just a message", roomid="room8", msgtime=100, seq=1, tenant_id=_TENANT_A)

    resp = _get_messages(client, app, db, _TENANT_A, "room8")
    messages = resp.json()["messages"]
    assert len(messages) == 1
    m = messages[0]
    assert m["is_revoked"] is False
    assert m["revoked_at"] is None
    assert m["revoke_event_msgid"] is None
    assert m["revoke_association_status"] is None
