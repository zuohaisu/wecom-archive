"""
Tests for RND-230 — Enhanced chat search: multi-dimensional filters
(date / user / staff / msgtype) on GET /api/search/messages.

Phase-one (RED) tests T1-T5 lock in behavior that does not exist yet on
`main`: `q` is currently required (422 on omission) and the query has no
date/user/staff/msgtype filter parameters at all. These are written BEFORE
the implementation change per the RND-230 execution prompt's
reproduce-then-implement contract.

Reuses the sqlite-backed schema/fixtures from test_reachability_audit.py
and the client/auth helpers from test_search_api.py.

Run (from backend/):
    pytest tests/test_rnd_230_search_filters.py -v

# flake8: noqa: F811
"""

from __future__ import annotations

import time

from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    _insert_recipient,
    db,  # noqa: F401 — pytest fixture, must be imported to be discovered
)
from tests.test_search_api import (
    client,  # noqa: F401 — pytest fixture, must be imported to be discovered
    _authed,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


_DAY_MS = 24 * 60 * 60 * 1000


# ---------------------------------------------------------------------------
# Phase one — RED reproduction (T1-T5)
# ---------------------------------------------------------------------------


def test_t1_q_optional_with_msgtype_filter_returns_200(client, db) -> None:
    """T1: omitting q while supplying a filter (msgtype=text) must succeed
    (200), not 422 — RND-230 decision #2, "pure filter mode"."""
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="anything", msgtime=_now_ms(),
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?msgtype=text")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, (
        f"expected q to be optional when a filter is present, "
        f"got {resp.status_code}: {resp.text}"
    )


def test_t2_staff_filter_restricts_to_participant(client, db) -> None:
    """T2: staff=<id> must restrict results to messages where that id
    participates as sender or recipient — currently ignored entirely."""
    from app.main import app

    now = _now_ms()
    msg_match = _insert_message(
        db, msgtype="text", sender="staff_target",
        tenant_id=_TENANT_A, content_text="keyword hit one", msgtime=now,
    )
    _insert_recipient(db, msg_match.id, "contact_a", tenant_id=_TENANT_A)

    msg_other = _insert_message(
        db, msgtype="text", sender="staff_other",
        tenant_id=_TENANT_A, content_text="keyword hit two", msgtime=now,
    )
    _insert_recipient(db, msg_other.id, "contact_b", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword&staff=staff_target")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1, (
        f"expected staff filter to restrict to staff_target's message only, "
        f"got {body['results']}"
    )
    assert body["results"][0]["msgid"] == msg_match.msgid


def test_t3_non_text_msgtype_filter_returns_empty_not_error(client, db) -> None:
    """T3: selecting a msgtype outside the v1 text-only index (e.g. image)
    must be honored (msgtype.in_(...) takes over from the hardcoded
    =='text') and degrade to an empty list, never an error — the documented
    v1 known-limitation, not a bug."""
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword content", msgtime=_now_ms(),
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword&msgtype=image")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []


def test_t4_date_range_filters_to_recent_window(client, db) -> None:
    """T4: date_range=7d must exclude messages older than 7 days —
    currently ignored, so both old and recent rows come back today."""
    from app.main import app

    now = _now_ms()
    msg_recent = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword recent", msgtime=now,
    )
    _insert_recipient(db, msg_recent.id, "contact_a", tenant_id=_TENANT_A)

    msg_old = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword old", msgtime=now - 8 * _DAY_MS,
    )
    _insert_recipient(db, msg_old.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword&date_range=7d")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    msgids = {r["msgid"] for r in body["results"]}
    assert msg_recent.msgid in msgids
    assert msg_old.msgid not in msgids, (
        "date_range=7d must exclude the 8-day-old message"
    )


def test_t5_tenant_isolation_holds_with_filters(client, db) -> None:
    """T5: baseline that must hold both before and after this change —
    filters combined must never leak another tenant's messages."""
    from app.main import app

    now = _now_ms()
    msg_a = _insert_message(
        db, msgtype="text", sender="staff_shared",
        tenant_id=_TENANT_A, content_text="secret shared keyword", msgtime=now,
    )
    _insert_recipient(db, msg_a.id, "contact_a", tenant_id=_TENANT_A)

    msg_b = _insert_message(
        db, msgtype="text", sender="staff_shared",
        tenant_id=_TENANT_B, content_text="secret shared keyword", msgtime=now,
    )
    _insert_recipient(db, msg_b.id, "contact_b", tenant_id=_TENANT_B)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            "/api/search/messages?q=secret&staff=staff_shared&date_range=90d&msgtype=text"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["msgid"] == msg_a.msgid


# ---------------------------------------------------------------------------
# Phase three — expanded coverage of the acceptance checklist
# ---------------------------------------------------------------------------


def test_user_filter_restricts_to_contact_participant(client, db) -> None:
    """`user` (contact-side) works the same way as `staff`, on the contact
    side of the conversation."""
    from app.main import app

    now = _now_ms()
    msg_match = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword one", msgtime=now,
    )
    _insert_recipient(db, msg_match.id, "contact_target", tenant_id=_TENANT_A)

    msg_other = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword two", msgtime=now,
    )
    _insert_recipient(db, msg_other.id, "contact_other", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword&user=contact_target")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["msgid"] == msg_match.msgid


def test_multiple_filters_combine_with_and(client, db) -> None:
    """date + staff + msgtype must all narrow together (AND), not just the
    last one applied."""
    from app.main import app

    now = _now_ms()

    # Matches every filter.
    msg_all = _insert_message(
        db, msgtype="text", sender="staff_target",
        tenant_id=_TENANT_A, content_text="keyword all match", msgtime=now,
    )
    _insert_recipient(db, msg_all.id, "contact_a", tenant_id=_TENANT_A)

    # Right staff, but too old for date_range=7d.
    msg_wrong_date = _insert_message(
        db, msgtype="text", sender="staff_target",
        tenant_id=_TENANT_A, content_text="keyword old", msgtime=now - 30 * _DAY_MS,
    )
    _insert_recipient(db, msg_wrong_date.id, "contact_a", tenant_id=_TENANT_A)

    # Right date, but wrong staff.
    msg_wrong_staff = _insert_message(
        db, msgtype="text", sender="staff_other",
        tenant_id=_TENANT_A, content_text="keyword wrong staff", msgtime=now,
    )
    _insert_recipient(db, msg_wrong_staff.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            "/api/search/messages?q=keyword&staff=staff_target&date_range=7d&msgtype=text"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    msgids = {r["msgid"] for r in body["results"]}
    assert msgids == {msg_all.msgid}


def test_date_from_and_date_to_explicit_bounds(client, db) -> None:
    """Explicit date_from/date_to bound an arbitrary window, independent of
    the date_range presets."""
    from app.main import app

    msg_in_window = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword in window", msgtime=5000,
    )
    _insert_recipient(db, msg_in_window.id, "contact_a", tenant_id=_TENANT_A)

    msg_before = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword before", msgtime=1000,
    )
    _insert_recipient(db, msg_before.id, "contact_a", tenant_id=_TENANT_A)

    msg_after = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword after", msgtime=9000,
    )
    _insert_recipient(db, msg_after.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            "/api/search/messages?q=keyword&date_from=4000&date_to=6000"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    msgids = {r["msgid"] for r in body["results"]}
    assert msgids == {msg_in_window.msgid}


def test_result_includes_msgtype_field(client, db) -> None:
    """MessageSearchResult must expose msgtype so the frontend can render
    the type filter's effect (footer label) without guessing."""
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword content", msgtime=_now_ms(),
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["msgtype"] == "text"


def test_staff_filter_ignores_non_staff_ids(client, db) -> None:
    """staff集合 must be intersected with real staff ids — passing a
    contact-shaped id via `staff` must not accidentally match it as if it
    were staff."""
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="keyword content", msgtime=_now_ms(),
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword&staff=contact_a")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []


def test_group_message_response_exposes_full_contact_and_staff_participant_sets(client, db) -> None:
    """QA remediation (RND-230 finding C12): a group message's response
    row must carry every contact/staff participant (sender + all
    recipients), not just the single entity_id picked for navigation —
    otherwise the frontend's "用户" filter has no way to discover a
    contact who only ever appears as a recipient of a staff-authored
    group message, even though `user=<contact>` genuinely narrows to
    them (see test_user_filter_narrows_group_message_by_recipient below)."""
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_alice",
        tenant_id=_TENANT_A, content_text="keyword group broadcast",
        msgtime=_now_ms(), roomid="room1",
    )
    _insert_recipient(db, msg.id, "contact_bob", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_carol", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["entity_id"] == "staff_alice"
    assert result["entity_type"] == "staff"
    assert sorted(result["contact_ids"]) == ["contact_bob", "contact_carol"]
    assert result["staff_ids"] == ["staff_alice"]


def test_user_filter_narrows_group_message_by_recipient(client, db) -> None:
    """The backend side of C12: `user=<contact>` must narrow a group
    message down by ANY recipient, not just the sender — this already
    worked before the C12 fix (it is a pure SQL participant filter); the
    fix is that the UI can now discover this contact as an option at all."""
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_alice",
        tenant_id=_TENANT_A, content_text="keyword group broadcast",
        msgtime=_now_ms(), roomid="room1",
    )
    _insert_recipient(db, msg.id, "contact_bob", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_carol", tenant_id=_TENANT_A)

    other_msg = _insert_message(
        db, msgtype="text", sender="staff_alice",
        tenant_id=_TENANT_A, content_text="keyword unrelated room",
        msgtime=_now_ms(), roomid="room2",
    )
    _insert_recipient(db, other_msg.id, "contact_dave", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword&user=contact_carol")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["msgid"] == msg.msgid
