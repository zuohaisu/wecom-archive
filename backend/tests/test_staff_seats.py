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

RND-158 — Page load speed optimisation:
  - get_monitored_accounts avoids full ArchiveMessage ORM object
    materialisation for conversation_count.
  - _count_entity_conversations fetches a compact projection (id, sender,
    roomid + recipients) and derives canonical conversation keys using
    the exact same _derive_conversation_membership function as the
    authoritative _build_conversation_list, preserving exact count
    semantics.
  - Real SQLite-backed equivalence tests confirm the compact projection
    path produces the same conversation_count as the authoritative full
    fetch for all canonical conversation identity cases.

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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import ArchiveMessage, ArchiveMessageRecipient


# ---------------------------------------------------------------------------
# sqlite-backed test schema for equivalence tests
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE tenants (
    id TEXT PRIMARY KEY, name TEXT, slug TEXT, is_active INTEGER,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE archive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid TEXT NOT NULL,
    seq INTEGER NOT NULL,
    publickey_ver INTEGER NOT NULL,
    raw_encrypted_payload TEXT,
    encrypt_random_key TEXT NOT NULL,
    encrypt_chat_msg TEXT NOT NULL,
    decrypt_status TEXT NOT NULL DEFAULT 'pending',
    decrypted_payload TEXT,
    structured_content TEXT,
    content_text TEXT,
    msgtype TEXT,
    sender TEXT,
    roomid TEXT,
    msgtime INTEGER,
    tolist TEXT,
    sdkfileid TEXT,
    is_revoked INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT,
    tenant_id TEXT,
    created_at TEXT
);
CREATE TABLE archive_message_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    receiver_userid TEXT NOT NULL,
    receiver_type TEXT,
    tenant_id TEXT,
    created_at TEXT
);
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wecom_userid TEXT NOT NULL,
    name TEXT,
    tenant_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE admin_users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    wecom_user_id TEXT NOT NULL,
    name TEXT,
    avatar_url TEXT,
    last_login_at TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""

_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"


def _make_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    return Session(engine)


@pytest.fixture()
def db():
    session = _make_session()
    yield session
    session.close()


def _insert_message(db: Session, **kwargs) -> ArchiveMessage:
    defaults = dict(
        seq=1,
        publickey_ver=1,
        encrypt_random_key="x",
        encrypt_chat_msg="y",
        decrypt_status="success",
        tenant_id=_TENANT_A,
    )
    defaults.update(kwargs)
    defaults.setdefault("msgid", f"msg-{defaults['seq']}-{id(defaults)}")
    msg = ArchiveMessage(**defaults)
    db.add(msg)
    db.flush()
    return msg


def _insert_recipient(db: Session, message_id: int, userid: str, **kwargs) -> ArchiveMessageRecipient:
    defaults = dict(tenant_id=_TENANT_A)
    defaults.update(kwargs)
    r = ArchiveMessageRecipient(message_id=message_id, receiver_userid=userid, **defaults)
    db.add(r)
    db.flush()
    return r


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
# Pagination cursor — compound (msgtime, id), regression for QA-found bug
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
    assert len(page2_ids) == 1
    assert data2["pagination"]["has_older"] is False
    assert data2["pagination"]["next_before"] is None
    combined = page1_ids + page2_ids
    assert len(combined) == len(set(combined)) == 21
    assert set(combined) == {m.msgid for m in all_msgs}


def test_conversation_messages_pagination_mixed_timestamps_still_works(client) -> None:
    from app.main import app

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
    combined = page2_ids + page1_ids
    assert len(combined) == len(set(combined)) == 25
    assert combined == [m.msgid for m in all_msgs]


# ---------------------------------------------------------------------------
# Seat active/history classification must not be polluted by group expansion
# ---------------------------------------------------------------------------


def test_monitored_accounts_avoids_full_orm_materialization_for_conversation_count(
    client, monkeypatch
) -> None:
    """
    RND-158: /api/monitored-accounts must NOT call _fetch_messages_for_entity
    for conversation-count computation. Instead it should call
    _count_entity_conversations, which fetches a compact projection and
    derives canonical conversation keys using the exact same
    _derive_conversation_membership logic as the authoritative builder.
    """
    import app.routers.conversations as conv
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    old_fetch_called = []
    count_called_with = []

    def fake_old_fetch(db, entity_id, tenant_id):
        old_fetch_called.append(entity_id)
        return []

    def fake_count(db, entity_id, tenant_id, staff_ids=None):
        count_called_with.append((entity_id, tenant_id))
        return 5

    monkeypatch.setattr(
        conv, "_collect_staff_ids", lambda db, tenant_id: {"staff_a", "staff_b"}
    )
    monkeypatch.setattr(
        conv, "_latest_own_participation_time", lambda db, entity_id, tenant_id: 100
    )
    monkeypatch.setattr(conv, "_fetch_messages_for_entity", fake_old_fetch)
    monkeypatch.setattr(conv, "_count_entity_conversations", fake_count)
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(conv, "_load_display_names", lambda db, tenant_id: {})

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get("/api/monitored-accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(count_called_with) == 2
        used_ids = {c[0] for c in count_called_with}
        assert used_ids == {"staff_a", "staff_b"}
        assert len(old_fetch_called) == 0
        by_id = {d["staff_id"]: d for d in data}
        assert by_id["staff_a"]["conversation_count"] == 5
        assert by_id["staff_b"]["conversation_count"] == 5
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# RND-158: Behavioral equivalence tests — SQLite-backed
#
# Compares the optimized compact-projection count against the authoritative
# full-fetch builder for every canonical conversation identity case.
# ---------------------------------------------------------------------------


def _authoritative_count(db: Session, entity_id: str, tenant_id: str) -> int:
    """Reference: old full-fetch path via _fetch_messages_for_entity + _build_conversation_list."""
    from app.routers.conversations import _fetch_messages_for_entity, _load_recipients_map, _build_conversation_list
    messages = _fetch_messages_for_entity(db, entity_id, tenant_id)
    if not messages:
        return 0
    recipients_map = _load_recipients_map(db, tenant_id, [m.id for m in messages])
    return len(_build_conversation_list(messages, recipients_map, {}, None))


def _optimized_count(db: Session, entity_id: str, tenant_id: str, staff_ids=None) -> int:
    """Optimized path: compact projection + canonical key derivation."""
    from app.routers.conversations import _count_entity_conversations
    return _count_entity_conversations(db, entity_id, tenant_id, staff_ids)


def _assert_equivalence(db: Session, entity_id: str, tenant_id: str, msg: str, staff_ids=None) -> None:
    auth = _authoritative_count(db, entity_id, tenant_id)
    opt = _optimized_count(db, entity_id, tenant_id, staff_ids)
    assert auth == opt, (
        f"Equivalence failure for '{msg}': authoritative={auth}, optimized={opt} (staff_ids={staff_ids})"
    )


# -- Fixture cases --

def test_equivalence_one_direct_conversation(db: Session) -> None:
    """Single direct message: staff_a -> contact_zhangsan."""
    msg = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "one direct conversation")


def test_equivalence_repeated_same_partner(db: Session) -> None:
    """Two messages to the same direct partner — counts as 1 conversation."""
    msg1 = _insert_message(db, sender="staff_a", msgtime=100, tenant_id=_TENANT_A)
    _insert_recipient(db, msg1.id, "contact_zhangsan", tenant_id=_TENANT_A)
    msg2 = _insert_message(db, sender="staff_a", msgtime=200, tenant_id=_TENANT_A)
    _insert_recipient(db, msg2.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "repeated same partner")


def test_equivalence_sender_side(db: Session) -> None:
    """Entity is the sender of the direct message."""
    msg = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "sender side")


def test_equivalence_recipient_side(db: Session) -> None:
    """Entity is the recipient of the direct message."""
    msg = _insert_message(db, sender="contact_zhangsan", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "staff_a", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "recipient side")


def test_equivalence_multiple_direct_partners(db: Session) -> None:
    """Two different direct partners — counts as 2 conversations."""
    msg1 = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg1.id, "contact_zhangsan", tenant_id=_TENANT_A)
    msg2 = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg2.id, "contact_lisi", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "multiple direct partners")


def test_equivalence_one_group(db: Session) -> None:
    """One group room."""
    msg = _insert_message(db, sender="staff_a", roomid="room_g1", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "one group")


def test_equivalence_repeated_group(db: Session) -> None:
    """Multiple messages in the same group room — counts as 1 conversation."""
    msg1 = _insert_message(db, sender="staff_a", roomid="room_g1", msgtime=100, tenant_id=_TENANT_A)
    _insert_recipient(db, msg1.id, "contact_zhangsan", tenant_id=_TENANT_A)
    msg2 = _insert_message(db, sender="staff_a", roomid="room_g1", msgtime=200, tenant_id=_TENANT_A)
    _insert_recipient(db, msg2.id, "contact_lisi", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "repeated group")


def test_equivalence_multiple_groups(db: Session) -> None:
    """Two different group rooms — counts as 2 conversations."""
    msg1 = _insert_message(db, sender="staff_a", roomid="room_g1", tenant_id=_TENANT_A)
    _insert_recipient(db, msg1.id, "contact_zhangsan", tenant_id=_TENANT_A)
    msg2 = _insert_message(db, sender="staff_a", roomid="room_g2", tenant_id=_TENANT_A)
    _insert_recipient(db, msg2.id, "contact_lisi", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "multiple groups")


def test_equivalence_mixed_direct_and_group(db: Session) -> None:
    """One direct + one group = 2 conversations."""
    msg1 = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg1.id, "contact_zhangsan", tenant_id=_TENANT_A)
    msg2 = _insert_message(db, sender="staff_a", roomid="room_g1", tenant_id=_TENANT_A)
    _insert_recipient(db, msg2.id, "contact_lisi", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "mixed direct and group")


def test_equivalence_staff_to_staff(db: Session) -> None:
    """Direct message between two staff members — both with and without
    explicit staff_ids."""
    msg = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "staff_b", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "staff to staff")
    _assert_equivalence(
        db, "staff_a", _TENANT_A, "staff to staff with staff_ids",
        staff_ids={"staff_a", "staff_b"},
    )


def test_equivalence_self_message(db: Session) -> None:
    """A user sends a message to themselves."""
    msg = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "staff_a", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "self message")


def test_equivalence_empty_participation(db: Session) -> None:
    """Entity has no messages — count is 0."""
    # Staff_a exists in another tenant's data
    msg = _insert_message(db, sender="staff_a", tenant_id=_TENANT_B)
    _insert_recipient(db, msg.id, "contact_x", tenant_id=_TENANT_B)
    _assert_equivalence(db, "staff_a", _TENANT_A, "empty participation")


def test_equivalence_cross_tenant_data(db: Session) -> None:
    """Messages in tenant B should not be counted for tenant A."""
    msg_a = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg_a.id, "contact_a", tenant_id=_TENANT_A)
    msg_b = _insert_message(db, sender="staff_a", tenant_id=_TENANT_B)
    _insert_recipient(db, msg_b.id, "contact_b", tenant_id=_TENANT_B)
    _assert_equivalence(db, "staff_a", _TENANT_A, "cross-tenant isolation")
    assert _optimized_count(db, "staff_a", _TENANT_A) == 1
    assert _optimized_count(db, "staff_a", _TENANT_B) == 1


def test_equivalence_null_roomid(db: Session) -> None:
    """Null roomid is treated as direct message."""
    msg = _insert_message(db, sender="staff_a", roomid=None, tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "null roomid")


def test_equivalence_empty_roomid(db: Session) -> None:
    """Empty string roomid is treated as direct message."""
    msg = _insert_message(db, sender="staff_a", roomid="", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "empty roomid")


def test_equivalence_whitespace_roomid(db: Session) -> None:
    """Whitespace-only roomid is truthy — treated as a group conversation
    key by the authoritative builder (``if roomid:`` after ``roomid or ""``)."""
    msg = _insert_message(db, sender="staff_a", roomid="   ", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "whitespace roomid")


def test_equivalence_direct_group_collision(db: Session) -> None:
    """A direct conversation and a group conversation whose canonical keys
    collide (both produce the same identifier) — the builder merges them
    into a single key, so the count is 1, not 2.

    Creates:
      - A direct conversation 'staff_a ↔ contact_zhangsan' → key "direct__contact_zhangsan___staff_a"
      - A group message in roomid="direct__contact_zhangsan___staff_a" → key is that same roomid
    Both produce different conv_type but the SAME canonical conv_id."""
    # Direct message: staff_a → contact_zhangsan
    msg_direct = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    # Group message whose roomid happens to COLLIDE with the direct conv_id
    from app.routers.conversations import _direct_conv_id
    collision_roomid = _direct_conv_id("contact_zhangsan", "staff_a")
    msg_group = _insert_message(db, sender="staff_a", roomid=collision_roomid, tenant_id=_TENANT_A)
    _insert_recipient(db, msg_group.id, "contact_lisi", tenant_id=_TENANT_A)
    # Both messages map to the same canonical key → count = 1
    _assert_equivalence(db, "staff_a", _TENANT_A, "direct/group ID collision")
    _assert_equivalence(
        db, "staff_a", _TENANT_A, "direct/group ID collision with staff_ids",
        staff_ids={"staff_a"},
    )


def test_equivalence_multi_recipient_direct_message(db: Session) -> None:
    """Direct message with two recipients — counts as 1 conversation using
    the canonical key derived from sorted(all_parties)."""
    msg = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_lisi", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "multi-recipient direct message")
    _assert_equivalence(
        db, "staff_a", _TENANT_A, "multi-recipient direct with staff_ids",
        staff_ids={"staff_a"},
    )


def test_equivalence_mixed_staff_contact_multi_recipient(db: Session) -> None:
    """Direct message with mixed staff/contact recipients — the staff/contact
    rule picks one pair from sorted(staff) + sorted(contact)."""
    msg = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "staff_b", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_lisi", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "mixed staff/contact multi-recipient")
    _assert_equivalence(
        db, "staff_a", _TENANT_A, "mixed staff/contact multi-recipient with staff_ids",
        staff_ids={"staff_a", "staff_b"},
    )


def test_equivalence_null_sender_with_recipients(db: Session) -> None:
    """A message with null sender but valid recipients — the empty sender
    becomes "" and recipients produce a valid key."""
    msg = _insert_message(db, sender=None, tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "staff_a", tenant_id=_TENANT_A)
    _assert_equivalence(db, "staff_a", _TENANT_A, "recipient-side with null sender")
    _assert_equivalence(
        db, "staff_a", _TENANT_A, "recipient-side null sender with staff_ids",
        staff_ids={"staff_a"},
    )


# ---------------------------------------------------------------------------
# _latest_own_participation_time — unit tests (mock-based)
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
    import app.routers.conversations as conv
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    T1 = 1000
    T2 = 9000
    T_ACTIVE = 2000

    own_participation_times = {"staff_a": T1, "staff_b": T_ACTIVE}

    def fake_latest_own_participation(db, entity_id, tenant_id):
        return own_participation_times.get(entity_id)

    def fake_fetch_messages(db, entity_id, tenant_id):
        if entity_id == "staff_a":
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
    monkeypatch.setattr(conv, "_count_entity_conversations", lambda db, eid, tid, staff_ids=None: 1)
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

        assert by_id["staff_b"]["seat_status"] == "active"
        assert by_id["staff_b"]["is_active_archive_seat"] is True
        assert by_id["staff_b"]["latest_message_time"] == T_ACTIVE

        assert by_id["staff_a"]["seat_status"] == "history"
        assert by_id["staff_a"]["is_active_archive_seat"] is False
        assert by_id["staff_a"]["latest_message_time"] == T1
        assert by_id["staff_a"]["latest_message_time"] != T2

        # Mocked count returns 1.
        assert by_id["staff_a"]["conversation_count"] == 1
    finally:
        app.dependency_overrides.clear()