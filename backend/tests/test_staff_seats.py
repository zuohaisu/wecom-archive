"""
Tests for RND-132 — Staff tab (WeCom archive seat) detection and message
timeline pagination.

Production decrypted senders are plain WeCom userids with no "staff_"
prefix (that prefix is only a mock/dev fixture convention), so the Staff
tab showed no data in production before this change. See the module
docstring in app/routers/conversations.py for the two-signal seat
detection design this file exercises.

Validates:
  - _collect_staff_ids() combines the "staff_" prefix signal with the
    admin_users-login signal, and never includes plain contacts.
  - /api/monitored-accounts ranks seats active-first (most recent
    latest_message_time), keeps historical seats visible, and is
    tenant-scoped.
  - /api/monitored-accounts still requires auth.
  - /api/conversations?mode=staff returns both direct and group sessions
    for a seat, sorted by latest_message_time descending.
  - /api/conversations/{id}/messages defaults to the latest 20 messages in
    ascending msgtime order, and a `before` cursor returns older messages.
  - Display-name fallback still applies when contacts.name is missing for
    a staff seat.

QA follow-up (post-review fixes):
  - Pagination cursor is a compound (msgtime, id) pair, not msgtime alone —
    regression coverage for the case where 20+ messages share one msgtime
    (a msgtime-only cursor with strict "<" would silently drop the
    remainder). See _encode_message_cursor / _decode_message_cursor.
  - Seat active/history ranking uses _latest_own_participation_time()
    (sender/recipient rows only), never the group-room-expanded set from
    _fetch_messages_for_entity() — regression coverage for a historical
    seat that must not inherit a later, unrelated message in a group room
    it once participated in.

Run (from backend/):
    pytest tests/test_staff_seats.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _msg(id, sender, roomid=None, msgtime=0, content_text="", msgtype="text", sdkfileid=None):
    return SimpleNamespace(
        id=id,
        msgid=f"m-{id}",
        sender=sender,
        roomid=roomid,
        msgtime=msgtime,
        msgtype=msgtype,
        content_text=content_text,
        decrypt_status="success",
        sdkfileid=sdkfileid,
    )


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _override_db_empty():
    yield MagicMock()


# ---------------------------------------------------------------------------
# _collect_staff_ids — combines prefix + admin-login signals
# ---------------------------------------------------------------------------


def test_collect_staff_ids_combines_prefix_and_admin_login_signals() -> None:
    from app.routers.conversations import _collect_staff_ids

    mock = MagicMock()

    sender_q = MagicMock()
    sender_q.filter.return_value = sender_q
    sender_q.distinct.return_value = sender_q
    sender_q.all.return_value = [
        ("staff_legacy",),
        ("real_wecom_user_001",),
        ("contact_zhangsan",),
    ]

    recipient_q = MagicMock()
    recipient_q.filter.return_value = recipient_q
    recipient_q.distinct.return_value = recipient_q
    recipient_q.all.return_value = [("contact_lisi",)]

    # This admin has logged into the console AND appears in the archive.
    admin_q = MagicMock()
    admin_q.filter.return_value = admin_q
    admin_q.distinct.return_value = admin_q
    admin_q.all.return_value = [("real_wecom_user_001",), ("never_seen_admin",)]

    def _query(target):
        key = getattr(target, "key", None)
        if key == "sender":
            return sender_q
        if key == "receiver_userid":
            return recipient_q
        if key == "wecom_user_id":
            return admin_q
        raise AssertionError(f"unexpected query target: {target}")

    mock.query.side_effect = _query

    staff_ids = _collect_staff_ids(mock, "tenant-a")

    assert staff_ids == {"staff_legacy", "real_wecom_user_001"}
    assert "contact_zhangsan" not in staff_ids
    assert "contact_lisi" not in staff_ids
    # An admin_users row that never appears in the archive is not a seat.
    assert "never_seen_admin" not in staff_ids


def test_collect_staff_ids_falls_back_to_prefix_when_no_admin_users() -> None:
    from app.routers.conversations import _collect_staff_ids

    mock = MagicMock()

    sender_q = MagicMock()
    sender_q.filter.return_value = sender_q
    sender_q.distinct.return_value = sender_q
    sender_q.all.return_value = [("staff_yingzi",), ("contact_zhangsan",)]

    recipient_q = MagicMock()
    recipient_q.filter.return_value = recipient_q
    recipient_q.distinct.return_value = recipient_q
    recipient_q.all.return_value = []

    admin_q = MagicMock()
    admin_q.filter.return_value = admin_q
    admin_q.distinct.return_value = admin_q
    admin_q.all.return_value = []

    def _query(target):
        key = getattr(target, "key", None)
        if key == "sender":
            return sender_q
        if key == "receiver_userid":
            return recipient_q
        if key == "wecom_user_id":
            return admin_q
        raise AssertionError(f"unexpected query target: {target}")

    mock.query.side_effect = _query

    assert _collect_staff_ids(mock, "tenant-a") == {"staff_yingzi"}


# ---------------------------------------------------------------------------
# /api/monitored-accounts — ranking, tenant scoping, auth
# ---------------------------------------------------------------------------


def test_monitored_accounts_ranks_active_seat_first_and_keeps_history(
    client, monkeypatch
) -> None:
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app
    import app.routers.conversations as conv

    seen_tenant_ids = []

    def fake_collect_staff_ids(db, tenant_id):
        seen_tenant_ids.append(tenant_id)
        return {"real_wecom_user_001", "staff_old_account"}

    own_participation_times = {"real_wecom_user_001": 5000, "staff_old_account": 1000}

    def fake_latest_own_participation(db, entity_id, tenant_id):
        return own_participation_times.get(entity_id)

    def fake_fetch_messages(db, entity_id, tenant_id):
        if entity_id == "real_wecom_user_001":
            return [_msg(1, "real_wecom_user_001", msgtime=5000)]
        if entity_id == "staff_old_account":
            return [_msg(2, "staff_old_account", msgtime=1000)]
        return []

    monkeypatch.setattr(conv, "_collect_staff_ids", fake_collect_staff_ids)
    monkeypatch.setattr(conv, "_latest_own_participation_time", fake_latest_own_participation)
    monkeypatch.setattr(conv, "_fetch_messages_for_entity", fake_fetch_messages)
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(conv, "_load_display_names", lambda db, tenant_id: {})

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get("/api/monitored-accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

        active, history = data[0], data[1]
        # Real production userid (no "staff_" prefix) is correctly surfaced.
        assert active["staff_id"] == "real_wecom_user_001"
        assert active["seat_status"] == "active"
        assert active["is_active_archive_seat"] is True
        assert active["latest_message_time"] == 5000

        assert history["staff_id"] == "staff_old_account"
        assert history["seat_status"] == "history"
        assert history["is_active_archive_seat"] is False
        # Historical seats remain visible, not hidden.
        assert history["latest_message_time"] == 1000

        assert seen_tenant_ids == ["tenant-a"]
    finally:
        app.dependency_overrides.clear()


def test_monitored_accounts_empty_when_no_seats_identified(client, monkeypatch) -> None:
    """No formal source and no signal match -> empty list, never a noisy dump of senders."""
    import app.routers.conversations as conv
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    monkeypatch.setattr(conv, "_collect_staff_ids", lambda db, tenant_id: set())

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get("/api/monitored-accounts")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        app.dependency_overrides.clear()


def test_monitored_accounts_display_name_falls_back_when_contact_name_missing(
    client, monkeypatch
) -> None:
    """RND-130 backfill blocked -> contacts.name may be missing; seat must still show a label."""
    import app.routers.conversations as conv
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    monkeypatch.setattr(
        conv, "_collect_staff_ids", lambda db, tenant_id: {"real_wecom_user_001"}
    )
    monkeypatch.setattr(
        conv, "_latest_own_participation_time", lambda db, entity_id, tenant_id: 100
    )
    monkeypatch.setattr(
        conv,
        "_fetch_messages_for_entity",
        lambda db, entity_id, tenant_id: [_msg(1, entity_id, msgtime=100)],
    )
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(conv, "_load_display_names", lambda db, tenant_id: {})

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get("/api/monitored-accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["display_name"] == "real_wecom_user_001"
        assert data[0]["display_name"].strip() != ""
    finally:
        app.dependency_overrides.clear()


def test_monitored_accounts_requires_auth_still_blocked(client) -> None:
    """Unauthenticated access to the Staff list endpoint remains blocked (RND-110 unchanged)."""
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get("/api/monitored-accounts")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /api/conversations?mode=staff — direct + group sessions, sorted
# ---------------------------------------------------------------------------


def test_staff_sessions_include_direct_and_group_conversations(client, monkeypatch) -> None:
    import app.routers.conversations as conv
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    messages = [
        _msg(1, "real_wecom_user_001", msgtime=100, content_text="hi"),
        _msg(2, "real_wecom_user_001", roomid="room_1", msgtime=300, content_text="group hi"),
    ]
    recipients_map = {1: ["contact_zhangsan"], 2: ["contact_lisi"]}

    monkeypatch.setattr(
        conv, "_fetch_messages_for_entity", lambda db, entity_id, tenant_id: messages
    )
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: recipients_map)
    monkeypatch.setattr(conv, "_load_display_names", lambda db, tenant_id: {})
    monkeypatch.setattr(
        conv, "_collect_staff_ids", lambda db, tenant_id: {"real_wecom_user_001"}
    )

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get(
            "/api/conversations?mode=staff&staff_id=real_wecom_user_001"
        )
        assert resp.status_code == 200
        data = resp.json()
        types = {c["conversation_type"] for c in data}
        assert types == {"direct", "group"}
        # Sorted by latest_message_time descending -> group (300) before direct (100).
        assert data[0]["conversation_type"] == "group"
        assert data[1]["conversation_type"] == "direct"
    finally:
        app.dependency_overrides.clear()


def test_staff_sessions_sorted_by_latest_message_time_desc(client, monkeypatch) -> None:
    import app.routers.conversations as conv
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    messages = [
        _msg(1, "real_wecom_user_001", roomid="room_a", msgtime=100),
        _msg(2, "real_wecom_user_001", roomid="room_b", msgtime=500),
        _msg(3, "real_wecom_user_001", roomid="room_c", msgtime=300),
    ]

    monkeypatch.setattr(
        conv, "_fetch_messages_for_entity", lambda db, entity_id, tenant_id: messages
    )
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(conv, "_load_display_names", lambda db, tenant_id: {})
    monkeypatch.setattr(
        conv, "_collect_staff_ids", lambda db, tenant_id: {"real_wecom_user_001"}
    )

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get(
            "/api/conversations?mode=staff&staff_id=real_wecom_user_001"
        )
        assert resp.status_code == 200
        times = [c["last_message_time"] for c in resp.json()]
        assert times == sorted(times, reverse=True)
        assert times == [500, 300, 100]
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /api/conversations/{id}/messages — default latest-20 ascending + before cursor
# ---------------------------------------------------------------------------


def test_conversation_messages_default_returns_latest_20_ascending(client) -> None:
    from app.auth import get_current_user
    from app.db.models import ArchiveMessageRecipient, Contact, MediaFile, MessageRevocation
    from app.db.session import get_db
    from app.main import app

    all_msgs = [
        _msg(i, "staff_a" if i % 2 == 0 else "contact_b", roomid="room1", msgtime=1000 + i)
        for i in range(25)
    ]

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
        media_q.all.return_value = []

        revocation_q = MagicMock()
        revocation_q.filter.return_value = revocation_q
        revocation_q.all.return_value = []

        def _query(model):
            if model is ArchiveMessageRecipient:
                return rcpt_q
            if model is Contact:
                return contact_q
            if model is MediaFile:
                return media_q
            if model is MessageRevocation:
                return revocation_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    try:
        resp = client.get("/api/conversations/room1/messages")
        assert resp.status_code == 200
        data = resp.json()
        msgs = data["messages"]
        assert len(msgs) == 20

        times = [m["msgtime"] for m in msgs]
        assert times == sorted(times)  # ascending, oldest first
        assert times[0] == 1005
        assert times[-1] == 1024
        assert data["pagination"]["has_older"] is True
        # Compound cursor: "<msgtime>:<id>" (id=5 has msgtime=1005 in this fixture).
        assert data["pagination"]["next_before"] == "1005:5"

        # "Load older" with the returned cursor.
        cursor = data["pagination"]["next_before"]
        resp2 = client.get(f"/api/conversations/room1/messages?before={cursor}")
        assert resp2.status_code == 200
        data2 = resp2.json()
        times2 = [m["msgtime"] for m in data2["messages"]]
        assert times2 == [1000, 1001, 1002, 1003, 1004]
        assert data2["pagination"]["has_older"] is False
        assert data2["pagination"]["next_before"] is None
    finally:
        app.dependency_overrides.clear()


def test_conversation_messages_requires_auth_still_blocked(client) -> None:
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get("/api/conversations/room1/messages")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Pagination cursor — compound (msgtime, id), regression for QA-found bug:
# a msgtime-only cursor with strict "<" silently drops messages when more
# than `limit` rows share the exact same msgtime.
# ---------------------------------------------------------------------------


def _run_messages_query(client, app, all_msgs, before=None, limit=20):
    from app.auth import get_current_user
    from app.db.models import ArchiveMessageRecipient, Contact, MediaFile, MessageRevocation
    from app.db.session import get_db

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
        media_q.all.return_value = []

        revocation_q = MagicMock()
        revocation_q.filter.return_value = revocation_q
        revocation_q.all.return_value = []

        def _query(model):
            if model is ArchiveMessageRecipient:
                return rcpt_q
            if model is Contact:
                return contact_q
            if model is MediaFile:
                return media_q
            if model is MessageRevocation:
                return revocation_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    try:
        url = f"/api/conversations/room1/messages?limit={limit}"
        if before is not None:
            url += f"&before={before}"
        return client.get(url)
    finally:
        app.dependency_overrides.clear()


def test_conversation_messages_pagination_survives_duplicate_msgtime(client) -> None:
    """
    QA regression: 21 messages all share msgtime=T. A msgtime-only cursor
    compared with strict "<" would exclude every row at T once the first
    page (20 of them) is loaded, permanently losing the 21st message. The
    compound (msgtime, id) cursor must not lose it.
    """
    from app.main import app

    T = 5000
    all_msgs = [_msg(i, "staff_a", roomid="room1", msgtime=T) for i in range(21)]

    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    data = resp.json()
    page1_ids = [m["msgid"] for m in data["messages"]]
    assert len(page1_ids) == 20
    assert data["pagination"]["has_older"] is True
    cursor = data["pagination"]["next_before"]
    assert cursor is not None

    resp2 = _run_messages_query(client, app, all_msgs, before=cursor)
    assert resp2.status_code == 200
    data2 = resp2.json()
    page2_ids = [m["msgid"] for m in data2["messages"]]

    # The 21st message must be reachable — not silently dropped.
    assert len(page2_ids) == 1
    assert data2["pagination"]["has_older"] is False
    assert data2["pagination"]["next_before"] is None

    # No skips, no duplicates across pages; combined set == all 21 ids.
    combined = page1_ids + page2_ids
    assert len(combined) == len(set(combined)) == 21
    assert set(combined) == {m.msgid for m in all_msgs}


def test_conversation_messages_pagination_mixed_timestamps_still_works(client) -> None:
    """Sanity check: a mix of unique and duplicate msgtimes across the page
    boundary still yields a full, gap-free, duplicate-free reconstruction."""
    from app.main import app

    # ids 0..9 share msgtime=1000 (duplicate cluster straddling the cursor),
    # ids 10..24 have strictly increasing unique msgtimes.
    all_msgs = [_msg(i, "staff_a", roomid="room1", msgtime=1000) for i in range(10)]
    all_msgs += [_msg(i, "staff_a", roomid="room1", msgtime=1000 + i) for i in range(10, 25)]

    resp = _run_messages_query(client, app, all_msgs)
    assert resp.status_code == 200
    data = resp.json()
    page1_ids = [m["msgid"] for m in data["messages"]]
    assert len(page1_ids) == 20
    assert data["pagination"]["has_older"] is True

    cursor = data["pagination"]["next_before"]
    resp2 = _run_messages_query(client, app, all_msgs, before=cursor)
    assert resp2.status_code == 200
    data2 = resp2.json()
    page2_ids = [m["msgid"] for m in data2["messages"]]
    assert data2["pagination"]["has_older"] is False

    combined = page2_ids + page1_ids  # page2 is older, so it comes first
    assert len(combined) == len(set(combined)) == 25
    assert combined == [m.msgid for m in all_msgs]  # exact ascending reconstruction


# ---------------------------------------------------------------------------
# Seat active/history classification must not be polluted by group-room
# expansion (QA regression)
# ---------------------------------------------------------------------------


def test_latest_own_participation_time_uses_sender_and_recipient_rows_only() -> None:
    from app.routers.conversations import _latest_own_participation_time

    mock = MagicMock()

    sender_max_q = MagicMock()
    sender_max_q.filter.return_value = sender_max_q
    sender_max_q.scalar.return_value = 1000

    recipient_max_q = MagicMock()
    recipient_max_q.join.return_value = recipient_max_q
    recipient_max_q.filter.return_value = recipient_max_q
    recipient_max_q.scalar.return_value = None

    calls = []

    def _query(*entities):
        # Implementation queries sender-max first, then recipient-max.
        calls.append(entities)
        return sender_max_q if len(calls) == 1 else recipient_max_q

    mock.query.side_effect = _query

    result = _latest_own_participation_time(mock, "staff_a", "tenant-a")
    assert result == 1000


def test_latest_own_participation_time_takes_max_of_sender_and_recipient() -> None:
    from app.routers.conversations import _latest_own_participation_time

    mock = MagicMock()

    sender_max_q = MagicMock()
    sender_max_q.filter.return_value = sender_max_q
    sender_max_q.scalar.return_value = 500

    recipient_max_q = MagicMock()
    recipient_max_q.join.return_value = recipient_max_q
    recipient_max_q.filter.return_value = recipient_max_q
    recipient_max_q.scalar.return_value = 900

    calls = []

    def _query(*entities):
        calls.append(entities)
        return sender_max_q if len(calls) == 1 else recipient_max_q

    mock.query.side_effect = _query

    assert _latest_own_participation_time(mock, "staff_a", "tenant-a") == 900


def test_latest_own_participation_time_none_when_no_participation() -> None:
    from app.routers.conversations import _latest_own_participation_time

    mock = MagicMock()

    sender_max_q = MagicMock()
    sender_max_q.filter.return_value = sender_max_q
    sender_max_q.scalar.return_value = None

    recipient_max_q = MagicMock()
    recipient_max_q.join.return_value = recipient_max_q
    recipient_max_q.filter.return_value = recipient_max_q
    recipient_max_q.scalar.return_value = None

    calls = []

    def _query(*entities):
        calls.append(entities)
        return sender_max_q if len(calls) == 1 else recipient_max_q

    mock.query.side_effect = _query

    assert _latest_own_participation_time(mock, "staff_a", "tenant-a") is None


def test_monitored_accounts_active_history_not_polluted_by_group_expansion(
    client, monkeypatch
) -> None:
    """
    QA regression: staff_a participated in group room "room_g" at T1=1000,
    then left/stopped participating. Group room "room_g" later has a
    message at T2=9000 from someone else — staff_a is neither sender nor
    recipient of it. _fetch_messages_for_entity() legitimately expands to
    include that T2 message (for session viewing), but get_monitored_accounts
    must NOT use that expanded set to compute staff_a's latest activity or
    active/history ranking — only _latest_own_participation_time() may be
    used for that.
    """
    import app.routers.conversations as conv
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    T1 = 1000  # staff_a's own last participation in room_g
    T2 = 9000  # room_g's later message — staff_a is NOT sender/recipient
    T_ACTIVE = 2000  # staff_b's own latest activity — should rank as active

    own_participation_times = {"staff_a": T1, "staff_b": T_ACTIVE}

    def fake_latest_own_participation(db, entity_id, tenant_id):
        return own_participation_times.get(entity_id)

    def fake_fetch_messages(db, entity_id, tenant_id):
        if entity_id == "staff_a":
            # Deliberately "polluted" session-view set: includes the later
            # T2 message from someone else in the same room. Legitimate for
            # session viewing; must not leak into seat ranking.
            return [
                _msg(1, "staff_a", roomid="room_g", msgtime=T1),
                _msg(2, "someone_else", roomid="room_g", msgtime=T2),
            ]
        if entity_id == "staff_b":
            return [_msg(3, "staff_b", msgtime=T_ACTIVE)]
        return []

    monkeypatch.setattr(
        conv, "_collect_staff_ids", lambda db, tenant_id: {"staff_a", "staff_b"}
    )
    monkeypatch.setattr(conv, "_latest_own_participation_time", fake_latest_own_participation)
    monkeypatch.setattr(conv, "_fetch_messages_for_entity", fake_fetch_messages)
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(conv, "_load_display_names", lambda db, tenant_id: {})

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get("/api/monitored-accounts")
        assert resp.status_code == 200
        data = resp.json()
        by_id = {d["staff_id"]: d for d in data}

        # staff_b (T_ACTIVE=2000) ranks active — NOT staff_a, even though
        # staff_a's expanded session set contains a later T2=9000 message.
        assert by_id["staff_b"]["seat_status"] == "active"
        assert by_id["staff_b"]["is_active_archive_seat"] is True
        assert by_id["staff_b"]["latest_message_time"] == T_ACTIVE

        assert by_id["staff_a"]["seat_status"] == "history"
        assert by_id["staff_a"]["is_active_archive_seat"] is False
        # Must reflect staff_a's OWN last participation (T1), never the
        # group's later unrelated message (T2).
        assert by_id["staff_a"]["latest_message_time"] == T1
        assert by_id["staff_a"]["latest_message_time"] != T2

        # conversation_count may still legitimately reflect the expanded
        # session-view set — group session viewing is unaffected by this fix.
        assert by_id["staff_a"]["conversation_count"] == 1
    finally:
        app.dependency_overrides.clear()
