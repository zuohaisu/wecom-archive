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

from app.db.models import ArchiveMessage, ArchiveMessageRecipient, MediaFile


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
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'admin',
    status TEXT NOT NULL DEFAULT 'active',
    email TEXT,
    phone TEXT,
    department TEXT,
    last_active_at TEXT,
    invite_token TEXT,
    invited_by TEXT,
    invite_status TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sdkfileid TEXT,
    archive_message_id INTEGER,
    tenant_id TEXT,
    file_type TEXT,
    local_path TEXT,
    oss_key TEXT,
    storage_backend TEXT,
    storage_ref TEXT,
    file_size INTEGER,
    download_status TEXT,
    download_attempts INTEGER NOT NULL DEFAULT 0,
    migration_status TEXT,
    migration_attempted_at TEXT,
    migration_error TEXT,
    bucket TEXT,
    mime_type TEXT,
    checksum_sha256 TEXT,
    thumbnail_ref TEXT,
    image_width INTEGER,
    image_height INTEGER,
    thumbnail_status TEXT,
    thumbnail_attempted_at TEXT,
    thumbnail_error TEXT,
    playback_ref TEXT,
    playback_status TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE message_revocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT,
    revoke_event_message_id INTEGER,
    revoke_event_msgid TEXT,
    revoke_event_msgtime INTEGER,
    target_msgid TEXT,
    original_message_id INTEGER,
    status TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE group_chat_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    roomid TEXT NOT NULL,
    display_name TEXT,
    source TEXT NOT NULL DEFAULT 'wecom_external_groupchat',
    sync_status TEXT NOT NULL DEFAULT 'unresolved',
    last_checked_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(tenant_id, roomid)
);
"""
# RND-158 Phase 2 QA round 7 (recovery note): media_files and
# message_revocations added so the real GET /api/conversations/{id}/messages
# endpoint (which unconditionally queries both) can be exercised through
# TestClient against this same in-memory SQLite session.

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
    import app.services.listing_service as listing_service

    seen_tenant_ids = []

    def fake_collect_staff_ids(db, tenant_id):
        seen_tenant_ids.append(tenant_id)
        return {"real_wecom_user_001", "staff_old_account"}

    own_participation_times = {"real_wecom_user_001": 5000, "staff_old_account": 1000}

    def fake_fetch_messages(db, entity_id, tenant_id):
        if entity_id == "real_wecom_user_001":
            return [_msg(1, "real_wecom_user_001", msgtime=5000)]
        if entity_id == "staff_old_account":
            return [_msg(2, "staff_old_account", msgtime=1000)]
        return []

    # RND-219: get_monitored_accounts' aggregation now runs inside
    # app.services.listing_service, so the functions it actually calls
    # must be patched there. _fetch_messages_for_entity/_load_recipients_map
    # are not called by this endpoint (never were) — left patched on `conv`
    # as an inert no-op guard against a real DB call sneaking through.
    # RND-191: latest_message_time is now resolved via the batched
    # _batch_latest_own_participation_time (one call for every seat) rather
    # than the singular per-seat _latest_own_participation_time.
    monkeypatch.setattr(listing_service, "_collect_staff_ids", fake_collect_staff_ids)
    monkeypatch.setattr(
        listing_service,
        "_batch_latest_own_participation_time",
        lambda db, tenant_id, entity_ids: {
            eid: own_participation_times[eid] for eid in entity_ids if eid in own_participation_times
        },
    )
    monkeypatch.setattr(
        listing_service, "_batch_count_entity_conversations", lambda db, tenant_id, entity_ids, staff_ids=None: {eid: 1 for eid in entity_ids}
    )
    monkeypatch.setattr(conv, "_fetch_messages_for_entity", fake_fetch_messages)
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(listing_service, "_load_display_names_for_ids", lambda db, tenant_id, ids: {})

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
    import app.services.listing_service as listing_service
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    monkeypatch.setattr(listing_service, "_collect_staff_ids", lambda db, tenant_id: set())

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
    import app.services.listing_service as listing_service
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    monkeypatch.setattr(
        listing_service, "_collect_staff_ids", lambda db, tenant_id: {"real_wecom_user_001"}
    )
    monkeypatch.setattr(
        listing_service,
        "_batch_latest_own_participation_time",
        lambda db, tenant_id, entity_ids: {eid: 100 for eid in entity_ids},
    )
    monkeypatch.setattr(
        listing_service,
        "_batch_count_entity_conversations",
        lambda db, tenant_id, entity_ids, staff_ids=None: {eid: 1 for eid in entity_ids},
    )
    monkeypatch.setattr(
        conv,
        "_fetch_messages_for_entity",
        lambda db, entity_id, tenant_id: [_msg(1, entity_id, msgtime=100)],
    )
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(listing_service, "_load_display_names_for_ids", lambda db, tenant_id, ids: {})

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
    import app.services.listing_service as listing_service
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    messages = [
        _msg(1, "real_wecom_user_001", msgtime=100, content_text="hi"),
        _msg(2, "real_wecom_user_001", roomid="room_1", msgtime=300, content_text="group hi"),
    ]
    recipients_map = {1: ["contact_zhangsan"], 2: ["contact_lisi"]}

    # get_conversations() uses the compact-projection path
    # (_fetch_compact_messages_for_entity et al.), not the legacy
    # _fetch_messages_for_entity -- see that route's docstring. RND-219:
    # this aggregation now runs inside app.services.listing_service.
    monkeypatch.setattr(
        listing_service, "_fetch_compact_messages_for_entity", lambda db, entity_id, tenant_id: messages
    )
    monkeypatch.setattr(
        listing_service, "_load_recipients_map_compact", lambda db, tenant_id, ids: recipients_map
    )
    monkeypatch.setattr(listing_service, "_load_display_names_for_ids", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(
        listing_service, "_staff_ids_for_participants", lambda db, tenant_id, ids: {"real_wecom_user_001"}
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
    import app.services.listing_service as listing_service
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    messages = [
        _msg(1, "real_wecom_user_001", roomid="room_a", msgtime=100),
        _msg(2, "real_wecom_user_001", roomid="room_b", msgtime=500),
        _msg(3, "real_wecom_user_001", roomid="room_c", msgtime=300),
    ]

    monkeypatch.setattr(
        listing_service, "_fetch_compact_messages_for_entity", lambda db, entity_id, tenant_id: messages
    )
    monkeypatch.setattr(listing_service, "_load_recipients_map_compact", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(listing_service, "_load_display_names_for_ids", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(
        listing_service, "_staff_ids_for_participants", lambda db, tenant_id, ids: {"real_wecom_user_001"}
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


def test_staff_sessions_compact_flag_uses_group_summary_response(client, db: Session) -> None:
    """The console's initial staff request opts into the group-summary path;
    its cards do not need inferred group participant arrays."""
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    seed = _insert_message(
        db,
        msgid="compact-group-seed",
        sender="staff_speed",
        roomid="room_compact",
        msgtime=100,
        content_text="old",
    )
    _insert_recipient(db, seed.id, "contact_group")
    _insert_message(
        db,
        msgid="compact-group-latest",
        sender="contact_group",
        roomid="room_compact",
        msgtime=200,
        content_text="latest",
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), _TENANT_A)
    app.dependency_overrides[get_db] = override_db
    try:
        response = client.get(
            "/api/conversations?mode=staff&staff_id=staff_speed"
            "&include_participant_metadata=false"
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["conversation_type"] == "group"
        assert body[0]["last_message_text"] == "latest"
        assert body[0]["contact_ids"] == []
        assert body[0]["monitored_account_ids"] == []
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /api/conversations/{id}/messages — default latest-20 ascending + before cursor
# ---------------------------------------------------------------------------


def test_conversation_messages_default_returns_latest_20_ascending(client, db: Session) -> None:
    # RND-191: real SQLite-backed session, not a hand-rolled MagicMock db.
    # resolve_timeline_page now resolves membership via a compact
    # (id/sender/roomid/msgtime) projection and only hydrates the winning
    # page to full ArchiveMessage rows (two distinct `db.query(...)` shapes
    # instead of one) -- a mock keyed to a single canned `.all()` result
    # regardless of which columns/ids were actually queried can no longer
    # tell the two queries apart. A real DB does the filtering for real, so
    # this is both simpler and a more faithful regression check.
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    for i in range(25):
        _insert_message(
            db,
            id=i,
            msgid=f"m-{i}",
            sender="staff_a" if i % 2 == 0 else "contact_b",
            roomid="room1",
            msgtime=1000 + i,
            msgtype="text",
            content_text="",
            seq=i,
            tenant_id=_TENANT_A,
        )

    def _override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), _TENANT_A)
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


def _run_messages_query(client, app, db, all_msgs, before=None, limit=20):
    # RND-191: real SQLite-backed session -- see the docstring note on
    # test_conversation_messages_default_returns_latest_20_ascending for
    # why a MagicMock keyed to one canned `.all()` result can no longer
    # stand in for resolve_timeline_page's two distinct query shapes
    # (compact resolution, then page-only hydration). Idempotent seeding:
    # this helper is called twice per test against the same `all_msgs`/`db`
    # (once per pagination page), so already-inserted ids are skipped
    # rather than re-inserted.
    from app.auth import get_current_user
    from app.db.session import get_db

    existing_ids = {row[0] for row in db.query(ArchiveMessage.id).all()}
    for m in all_msgs:
        if m.id not in existing_ids:
            _insert_message(
                db,
                id=m.id,
                msgid=m.msgid,
                sender=m.sender,
                roomid=m.roomid,
                msgtime=m.msgtime,
                msgtype=m.msgtype,
                content_text=m.content_text,
                seq=m.id,
                tenant_id=_TENANT_A,
            )

    def _override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), _TENANT_A)
    app.dependency_overrides[get_db] = _override_db
    try:
        url = f"/api/conversations/room1/messages?limit={limit}"
        if before is not None:
            url += f"&before={before}"
        return client.get(url)
    finally:
        app.dependency_overrides.clear()


def test_conversation_messages_pagination_survives_duplicate_msgtime(client, db: Session) -> None:
    from app.main import app

    T = 5000
    all_msgs = [_msg(i, "staff_a", roomid="room1", msgtime=T) for i in range(21)]

    resp = _run_messages_query(client, app, db, all_msgs)
    assert resp.status_code == 200
    data = resp.json()
    page1_ids = [m["msgid"] for m in data["messages"]]
    assert len(page1_ids) == 20
    assert data["pagination"]["has_older"] is True
    cursor = data["pagination"]["next_before"]
    assert cursor is not None

    resp2 = _run_messages_query(client, app, db, all_msgs, before=cursor)
    assert resp2.status_code == 200
    data2 = resp2.json()
    page2_ids = [m["msgid"] for m in data2["messages"]]
    assert len(page2_ids) == 1
    assert data2["pagination"]["has_older"] is False
    assert data2["pagination"]["next_before"] is None
    combined = page1_ids + page2_ids
    assert len(combined) == len(set(combined)) == 21
    assert set(combined) == {m.msgid for m in all_msgs}


def test_conversation_messages_pagination_mixed_timestamps_still_works(client, db: Session) -> None:
    from app.main import app

    all_msgs = [_msg(i, "staff_a", roomid="room1", msgtime=1000) for i in range(10)]
    all_msgs += [_msg(i, "staff_a", roomid="room1", msgtime=1000 + i) for i in range(10, 25)]

    resp = _run_messages_query(client, app, db, all_msgs)
    assert resp.status_code == 200
    data = resp.json()
    page1_ids = [m["msgid"] for m in data["messages"]]
    assert len(page1_ids) == 20
    assert data["pagination"]["has_older"] is True
    cursor = data["pagination"]["next_before"]
    resp2 = _run_messages_query(client, app, db, all_msgs, before=cursor)
    assert resp2.status_code == 200
    data2 = resp2.json()
    page2_ids = [m["msgid"] for m in data2["messages"]]
    assert data2["pagination"]["has_older"] is False
    combined = page2_ids + page1_ids
    assert len(combined) == len(set(combined)) == 25
    assert combined == [m.msgid for m in all_msgs]


def test_timeline_uses_sql_cursor_pagination_for_normal_group_conversations(
    client, db: Session, monkeypatch
) -> None:
    """Issue #21: a normal room must not load every message before paging."""
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app
    from app.services import timeline_service

    for index in range(45):
        _insert_message(
            db,
            msgid=f"issue-21-{index}",
            sender="staff-speed",
            roomid="issue-21-room",
            msgtime=1_000 + index,
            content_text=f"message {index}",
            seq=10_000 + index,
        )
    db.commit()

    compact_calls: list[object] = []

    def unexpected_full_conversation_load(*args, **kwargs):
        compact_calls.append((args, kwargs))
        raise AssertionError("normal group timeline must page in SQL")

    monkeypatch.setattr(
        timeline_service,
        "_fetch_conversation_messages_compact",
        unexpected_full_conversation_load,
    )

    def override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), _TENANT_A)
    app.dependency_overrides[get_db] = override_db
    try:
        first = client.get(
            "/api/conversations/issue-21-room/messages?limit=20&conversation_type=group"
        )
        assert first.status_code == 200
        first_body = first.json()
        assert [item["msgid"] for item in first_body["messages"]] == [
            f"issue-21-{index}" for index in range(25, 45)
        ]
        assert first_body["pagination"]["has_older"] is True

        second = client.get(
            "/api/conversations/issue-21-room/messages?limit=20&conversation_type=group"
            f"&before={first_body['pagination']['next_before']}"
        )
        assert second.status_code == 200
        second_body = second.json()
        assert [item["msgid"] for item in second_body["messages"]] == [
            f"issue-21-{index}" for index in range(5, 25)
        ]
        assert second_body["pagination"]["has_older"] is True
    finally:
        app.dependency_overrides.clear()

    assert compact_calls == []


def test_timeline_uses_sql_cursor_pagination_for_normal_direct_conversations(
    client, db: Session, monkeypatch
) -> None:
    """Issue #21: ordinary direct pairs use the same bounded page query."""
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app
    from app.services import timeline_service

    for index in range(45):
        message = _insert_message(
            db,
            msgid=f"issue-21-direct-{index}",
            sender="staff-speed",
            msgtime=2_000 + index,
            content_text=f"direct message {index}",
            seq=20_000 + index,
        )
        _insert_recipient(db, message.id, "contact-speed")
    db.commit()

    compact_calls: list[object] = []

    def unexpected_full_conversation_load(*args, **kwargs):
        compact_calls.append((args, kwargs))
        raise AssertionError("normal direct timeline must page in SQL")

    monkeypatch.setattr(
        timeline_service,
        "_fetch_conversation_messages_compact",
        unexpected_full_conversation_load,
    )

    def override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), _TENANT_A)
    app.dependency_overrides[get_db] = override_db
    try:
        first = client.get(
            "/api/conversations/direct__contact-speed___staff-speed/messages?limit=20"
            "&conversation_type=direct"
        )
        assert first.status_code == 200
        first_body = first.json()
        assert [item["msgid"] for item in first_body["messages"]] == [
            f"issue-21-direct-{index}" for index in range(25, 45)
        ]
        assert first_body["pagination"]["has_older"] is True
    finally:
        app.dependency_overrides.clear()

    assert compact_calls == []


# ---------------------------------------------------------------------------
# Seat active/history classification must not be polluted by group expansion
# ---------------------------------------------------------------------------


def test_monitored_accounts_avoids_full_orm_materialization_for_conversation_count(
    client, monkeypatch
) -> None:
    """
    RND-158: /api/monitored-accounts must NOT call _fetch_messages_for_entity
    for conversation-count computation.

    RND-191: conversation-count computation was further batched across all
    seats in a single call (_batch_count_entity_conversations) instead of
    one _count_entity_conversations call per seat, to eliminate the ~7*S
    per-tenant query multiplier for S seats (RND-158 profiling identified
    _latest_own_participation_time + _count_entity_conversations together
    as ~7 queries per seat). This test now asserts the batched call happens
    exactly once, covering every seat, rather than once per seat.
    """
    import app.routers.conversations as conv
    import app.services.listing_service as listing_service
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    old_fetch_called = []
    count_called_with = []

    def fake_old_fetch(db, entity_id, tenant_id):
        old_fetch_called.append(entity_id)
        return []

    def fake_batch_count(db, tenant_id, entity_ids, staff_ids=None):
        count_called_with.append((frozenset(entity_ids), tenant_id))
        return {eid: 5 for eid in entity_ids}

    monkeypatch.setattr(
        listing_service, "_collect_staff_ids", lambda db, tenant_id: {"staff_a", "staff_b"}
    )
    monkeypatch.setattr(
        listing_service,
        "_batch_latest_own_participation_time",
        lambda db, tenant_id, entity_ids: {eid: 100 for eid in entity_ids},
    )
    monkeypatch.setattr(conv, "_fetch_messages_for_entity", fake_old_fetch)
    monkeypatch.setattr(listing_service, "_batch_count_entity_conversations", fake_batch_count)
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(listing_service, "_load_display_names_for_ids", lambda db, tenant_id, ids: {})

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db_empty
    try:
        resp = client.get("/api/monitored-accounts")
        assert resp.status_code == 200
        data = resp.json()
        # Batched into a single call covering every seat, not one call per seat.
        assert len(count_called_with) == 1
        used_ids = count_called_with[0][0]
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


def _batch_optimized_count(db: Session, entity_id: str, tenant_id: str, staff_ids=None) -> int:
    """RND-191 batch path: same canonical count via
    _batch_count_entity_conversations, called with a single-entity set so
    every existing equivalence fixture below also exercises the batched
    code path, not just the original per-entity one."""
    from app.services.listing_service import _batch_count_entity_conversations
    return _batch_count_entity_conversations(db, tenant_id, {entity_id}, staff_ids).get(entity_id, 0)


def _assert_equivalence(db: Session, entity_id: str, tenant_id: str, msg: str, staff_ids=None) -> None:
    auth = _authoritative_count(db, entity_id, tenant_id)
    opt = _optimized_count(db, entity_id, tenant_id, staff_ids)
    batch = _batch_optimized_count(db, entity_id, tenant_id, staff_ids)
    assert auth == opt == batch, (
        f"Equivalence failure for '{msg}': authoritative={auth}, optimized={opt}, "
        f"batch={batch} (staff_ids={staff_ids})"
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
# RND-191: _batch_count_entity_conversations / _batch_latest_own_
# participation_time — multi-entity behavior the single-entity equivalence
# fixtures above (each calling _batch_optimized_count with a set of exactly
# one id) don't exercise: a group room's expansion is computed ONCE and
# shared across every entity that touches it (see that function's
# docstring) rather than once per entity, so these confirm sharing the
# expansion doesn't leak one entity's membership into another's count.
# ---------------------------------------------------------------------------


def test_batch_count_entity_conversations_two_seats_share_group_room(db: Session) -> None:
    """Two seats in the SAME group room -- expanded once by the batch call,
    not once per seat -- must each still get their own correct count.
    Only staff_a additionally has a private direct conversation."""
    from app.services.listing_service import _batch_count_entity_conversations

    msg1 = _insert_message(db, sender="staff_a", roomid="room_g1", tenant_id=_TENANT_A)
    _insert_recipient(db, msg1.id, "staff_b", tenant_id=_TENANT_A)
    msg2 = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg2.id, "contact_zhangsan", tenant_id=_TENANT_A)

    result = _batch_count_entity_conversations(
        db, _TENANT_A, {"staff_a", "staff_b"}, {"staff_a", "staff_b"}
    )
    assert result["staff_a"] == 2  # room_g1 + direct with contact_zhangsan
    assert result["staff_b"] == 1  # room_g1 only

    # Cross-check against the original per-entity path for the same data.
    from app.routers.conversations import _count_entity_conversations
    assert result["staff_a"] == _count_entity_conversations(
        db, "staff_a", _TENANT_A, {"staff_a", "staff_b"}
    )
    assert result["staff_b"] == _count_entity_conversations(
        db, "staff_b", _TENANT_A, {"staff_a", "staff_b"}
    )


def test_batch_count_entity_conversations_handles_entity_with_no_participation(db: Session) -> None:
    """An id in the batch request with zero archived participation must
    resolve to 0, not be dropped or raise, even though other ids in the
    same batch call do have data."""
    from app.services.listing_service import _batch_count_entity_conversations

    msg = _insert_message(db, sender="staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)

    result = _batch_count_entity_conversations(
        db, _TENANT_A, {"staff_a", "staff_ghost"}, {"staff_a"}
    )
    assert result["staff_a"] == 1
    assert result["staff_ghost"] == 0


def test_batch_count_entity_conversations_empty_entity_ids() -> None:
    from app.services.listing_service import _batch_count_entity_conversations

    assert _batch_count_entity_conversations(MagicMock(), _TENANT_A, set()) == {}


def test_batch_latest_own_participation_time_matches_singular(db: Session) -> None:
    """The batched max(sender, recipient) time for several entities in one
    call must match calling the original singular function once per
    entity, and an entity with no rows at all is simply absent."""
    from app.services.listing_service import (
        _batch_latest_own_participation_time,
        _latest_own_participation_time,
    )

    msg1 = _insert_message(db, sender="staff_a", msgtime=100, tenant_id=_TENANT_A)
    msg2 = _insert_message(db, sender="staff_b", msgtime=50, tenant_id=_TENANT_A)
    _insert_recipient(db, msg2.id, "staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, msg1.id, "staff_c", tenant_id=_TENANT_A)

    batch = _batch_latest_own_participation_time(
        db, _TENANT_A, {"staff_a", "staff_b", "staff_c", "staff_ghost"}
    )
    assert batch.get("staff_a") == _latest_own_participation_time(db, "staff_a", _TENANT_A) == 100
    assert batch.get("staff_b") == _latest_own_participation_time(db, "staff_b", _TENANT_A) == 50
    assert batch.get("staff_c") == _latest_own_participation_time(db, "staff_c", _TENANT_A) == 100
    assert "staff_ghost" not in batch


def test_batch_latest_own_participation_time_empty_entity_ids() -> None:
    from app.services.listing_service import _batch_latest_own_participation_time

    assert _batch_latest_own_participation_time(MagicMock(), _TENANT_A, set()) == {}


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
    import app.services.listing_service as listing_service
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    T1 = 1000
    T2 = 9000
    T_ACTIVE = 2000

    own_participation_times = {"staff_a": T1, "staff_b": T_ACTIVE}

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
        listing_service, "_collect_staff_ids", lambda db, tenant_id: {"staff_a", "staff_b"}
    )
    monkeypatch.setattr(
        listing_service,
        "_batch_latest_own_participation_time",
        lambda db, tenant_id, entity_ids: {
            eid: own_participation_times[eid] for eid in entity_ids if eid in own_participation_times
        },
    )
    monkeypatch.setattr(
        listing_service,
        "_batch_count_entity_conversations",
        lambda db, tenant_id, entity_ids, staff_ids=None: {eid: 1 for eid in entity_ids},
    )
    monkeypatch.setattr(conv, "_fetch_messages_for_entity", fake_fetch_messages)
    monkeypatch.setattr(conv, "_load_recipients_map", lambda db, tenant_id, ids: {})
    monkeypatch.setattr(listing_service, "_load_display_names_for_ids", lambda db, tenant_id, ids: {})

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

# ---------------------------------------------------------------------------
# RND-158 Phase 2 QA round 7 -- recovery note:
#
# This module's working tree previously carried ~1,910 additional lines of
# uncommitted test coverage from 6 prior QA rounds (RND-158 Phase 2
# collision-navigation / prefix-null-sender / API-contract rounds), built on
# top of the committed baseline above. That uncommitted content was
# destroyed during this round by an incorrect use of the file-write tool
# (a full-file overwrite instead of an in-place append) and could not be
# fully recovered from git history or local editor history -- see this
# round's final report for the incident writeup and the list of test names
# that were lost. The two helpers below (_optimized_conversations,
# _messages_via_endpoint, _list_row_for) are reconstructed verbatim from
# this same conversation's own earlier tool-call history (which read them
# from disk before the overwrite), since this round's new tests depend on
# them; they are not a full restoration of everything that was lost.
# ---------------------------------------------------------------------------


def _optimized_conversations(db: Session, entity_id: str, tenant_id: str) -> list[dict]:
    """New path: exactly what get_conversations() does internally."""
    from app.routers.conversations import (
        _fetch_compact_messages_for_entity,
        _load_recipients_map_compact,
        _load_display_names_for_ids,
        _staff_ids_for_participants,
        _build_conversation_list,
    )

    messages = _fetch_compact_messages_for_entity(db, entity_id, tenant_id)
    if not messages:
        return []
    recipients_map = _load_recipients_map_compact(db, tenant_id, [m.id for m in messages])
    participant_ids: set[str] = {m.sender for m in messages if m.sender}
    for ids in recipients_map.values():
        participant_ids.update(ids)
    display_names = _load_display_names_for_ids(db, tenant_id, participant_ids)
    staff_ids = _staff_ids_for_participants(db, tenant_id, participant_ids)
    return _build_conversation_list(messages, recipients_map, display_names, staff_ids)


def _messages_via_endpoint(
    client,
    app,
    db,
    conversation_id: str,
    tenant_id: str = _TENANT_A,
    limit: int = 20,
    mode: str | None = None,
    staff_id: str | None = None,
    contact_id: str | None = None,
    conversation_type: str | None = None,
):
    """Exercise the REAL /api/conversations/{id}/messages endpoint (not
    just the internal helper) against the given real SQLite session, per
    the ticket's "actual frontend/API navigation path" requirement.

    mode/staff_id/contact_id/conversation_type (RND-158 Phase 2
    API-contract round): optional entity context, forwarded verbatim as
    query params exactly as the real frontend now does -- see
    timelineEntityQueryParams() in app/main.py. Omitted by default so
    every pre-existing call site continues to exercise the legacy,
    entity-blind ID-only resolution path unchanged.
    """
    from app.auth import get_current_user
    from app.db.session import get_db

    def _override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), tenant_id)
    app.dependency_overrides[get_db] = _override_db
    params = {"limit": limit}
    if mode is not None:
        params["mode"] = mode
    if staff_id is not None:
        params["staff_id"] = staff_id
    if contact_id is not None:
        params["contact_id"] = contact_id
    if conversation_type is not None:
        params["conversation_type"] = conversation_type
    try:
        resp = client.get(
            f"/api/conversations/{conversation_id}/messages", params=params
        )
    finally:
        app.dependency_overrides.clear()
    return resp


def _list_row_for(db: Session, entity_id: str, tenant_id: str, conversation_id: str) -> dict:
    rows = _optimized_conversations(db, entity_id, tenant_id)
    match = next((r for r in rows if r["conversation_id"] == conversation_id), None)
    assert match is not None, f"conversation_id {conversation_id} not found in list: {rows}"
    return match
# ===========================================================================
# RND-158 Phase 2 QA round 7 -- three regression areas:
#   1. Three-participant (and more) null-sender messages: the timeline
#      re-verification classifier (_fetch_null_sender_candidate_messages)
#      must use the SAME real staff/contact classification the list uses,
#      not a stub that always says "not staff" -- see that function's
#      docstring for the full root-cause explanation.
#   2. Query narrowing: the null-sender-multi-recipient-gap-fix candidate
#      scan must not run an unrestricted, unselective query against every
#      ordinary direct conversation with no null-sender messages at all.
#   3. Media context propagation: a timeline resolved with entity context
#      must generate media_url/media_access_url values that carry the same
#      context, and the media routes must accept and honor it, including
#      not leaking one side of a genuine collision to an entity that only
#      participates in the other side.
# ===========================================================================


def _resolved_conv_id(db: Session, tenant_id: str, msg_id: int) -> str:
    """Recompute the REAL canonical conversation_id for one already-inserted
    message via the exact same authoritative path _build_conversation_list
    uses (real, tenant-scoped staff classification) -- used by the
    three-party fixtures below to derive the expected conversation_id
    without hand-duplicating _derive_conversation_membership's logic."""
    from app.routers.conversations import (
        _derive_conversation_membership,
        _collect_staff_ids,
    )

    msg = db.query(ArchiveMessage).filter(ArchiveMessage.id == msg_id).one()
    recipients = [
        r.receiver_userid
        for r in db.query(ArchiveMessageRecipient)
        .filter(ArchiveMessageRecipient.message_id == msg_id)
        .all()
    ]
    staff_ids = _collect_staff_ids(db, tenant_id)
    conv_id, _t, _s, _c = _derive_conversation_membership(
        msg.sender, msg.roomid, recipients, lambda uid: uid in staff_ids
    )
    return conv_id


# --- 1. Three(+)-participant null-sender messages ----------------------------


@pytest.mark.parametrize(
    "recipients,selected_entity,mode",
    [
        (["staff_a", "contact_zhangsan", "contact_lisi"], "staff_a", "staff"),
        (["staff_a", "contact_zhangsan", "contact_lisi"], "contact_zhangsan", "contact"),
        (["staff_a", "contact_zhangsan", "contact_lisi"], "contact_lisi", "contact"),
        (["staff_a", "staff_b", "contact_zhangsan"], "staff_a", "staff"),
        (["staff_a", "staff_b", "contact_zhangsan"], "staff_b", "staff"),
        (["staff_a", "staff_b", "contact_zhangsan"], "contact_zhangsan", "contact"),
        (["contact_zhangsan", "contact_lisi", "contact_wangwu"], "contact_zhangsan", "contact"),
        (["contact_zhangsan", "contact_lisi", "contact_wangwu"], "contact_wangwu", "contact"),
        (["staff_a", "staff_b", "staff_c"], "staff_a", "staff"),
        (["staff_a", "staff_b", "staff_c"], "staff_c", "staff"),
    ],
    ids=[
        "1staff2contact-first",
        "1staff2contact-middle",
        "1staff2contact-last",
        "2staff1contact-first",
        "2staff1contact-middle",
        "2staff1contact-last",
        "3contact-first",
        "3contact-last",
        "3staff-first",
        "3staff-last",
    ],
)
def test_three_party_null_sender_list_timeline_consistency(
    client, db: Session, recipients: list[str], selected_entity: str, mode: str
) -> None:
    """Ticket section 1: a null-sender message with 3 recipients spanning
    every staff/contact split (1s2c, 2s1c, 3c, 3s) must be navigable from
    EVERY visible list row, for whichever recipient is selected -- not just
    the two participants _direct_conv_id's canonical id string keeps. This
    is the exact scenario blocker 1 root-caused: the timeline
    re-verification classifier used to be a stub that always said
    "not staff", producing a DIFFERENT recomputed id than the list's real
    classifier for any 2+-participant null-sender message, silently
    dropping such messages from the timeline (and returning an unrelated
    real direct pair instead, if one happened to share the canonical id).
    """
    from app.main import app as _app

    m = _insert_message(
        db, sender=None, msgtime=10, content_text="three-party-null", tenant_id=_TENANT_A
    )
    for uid in recipients:
        _insert_recipient(db, m.id, uid, tenant_id=_TENANT_A)

    conv_id = _resolved_conv_id(db, _TENANT_A, m.id)

    # Sanity: conv_id is 2-party-shaped ("direct__a___b") for a 3-participant
    # message, per _derive_conversation_membership's contract -- proves the
    # test fixture actually exercises the multi-participant re-verification
    # path, not the <=1-participant one-sided fallback.
    assert conv_id.startswith("direct__") and "___" in conv_id[len("direct__"):]

    row = _list_row_for(db, selected_entity, _TENANT_A, conv_id)
    assert row["message_count"] == 1
    assert row["last_message_text"] == "three-party-null"

    kwargs = {"mode": mode}
    if mode == "staff":
        kwargs["staff_id"] = selected_entity
    else:
        kwargs["contact_id"] = selected_entity
    resp = _messages_via_endpoint(client, _app, db, conv_id, **kwargs)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]
    assert data["messages"][0]["content_text"] == "three-party-null"
    assert row["last_message_text"] in {mm["content_text"] for mm in data["messages"]}

    # No unrelated direct rows / duplicates: an unrelated ordinary direct
    # pair sharing NO participant with this message must not appear here,
    # and this message must not appear twice.
    texts = [mm["content_text"] for mm in data["messages"]]
    assert texts.count("three-party-null") == 1


def test_three_party_null_sender_order_independent_insertion(client, db: Session) -> None:
    """Recipient insertion order must not affect the resolved
    conversation_id or timeline result -- _derive_conversation_membership
    sorts internally, but this proves the DB-row-order independence
    end-to-end through the real endpoint."""
    from app.main import app as _app

    m1 = _insert_message(db, sender=None, msgtime=10, content_text="order-1", tenant_id=_TENANT_A)
    for uid in ["contact_lisi", "staff_a", "contact_zhangsan"]:
        _insert_recipient(db, m1.id, uid, tenant_id=_TENANT_A)

    m2 = _insert_message(db, sender=None, msgtime=20, content_text="order-2", tenant_id=_TENANT_A)
    for uid in ["staff_a", "contact_zhangsan", "contact_lisi"]:
        _insert_recipient(db, m2.id, uid, tenant_id=_TENANT_A)

    conv_id_1 = _resolved_conv_id(db, _TENANT_A, m1.id)
    conv_id_2 = _resolved_conv_id(db, _TENANT_A, m2.id)
    assert conv_id_1 == conv_id_2

    row = _list_row_for(db, "staff_a", _TENANT_A, conv_id_1)
    assert row["message_count"] == 2

    resp = _messages_via_endpoint(client, _app, db, conv_id_1, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 2 == row["message_count"]
    assert {mm["content_text"] for mm in data["messages"]} == {"order-1", "order-2"}


def test_three_party_null_sender_repeated_messages_equal_timestamps(client, db: Session) -> None:
    """Repeated three-party null-sender messages sharing the exact same
    msgtime -- list count and timeline count must still agree, no message
    dropped or duplicated at the tie boundary."""
    from app.main import app as _app

    ids = set()
    for i in range(4):
        m = _insert_message(
            db, sender=None, msgtime=50, content_text=f"tri-{i}", tenant_id=_TENANT_A
        )
        for uid in ["staff_a", "contact_zhangsan", "contact_lisi"]:
            _insert_recipient(db, m.id, uid, tenant_id=_TENANT_A)
        ids.add(m.id)

    conv_id = _resolved_conv_id(db, _TENANT_A, next(iter(ids)))
    row = _list_row_for(db, "contact_lisi", _TENANT_A, conv_id)
    assert row["message_count"] == 4

    resp = _messages_via_endpoint(client, _app, db, conv_id, mode="contact", contact_id="contact_lisi")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 4 == row["message_count"]
    assert {mm["content_text"] for mm in data["messages"]} == {f"tri-{i}" for i in range(4)}


def test_three_party_null_sender_does_not_leak_unrelated_direct_pair(client, db: Session) -> None:
    """The exact scenario blocker 1 root-caused: a genuinely unrelated
    staff_a<->contact_zhangsan direct message exists (found normally via
    _fetch_direct_pair_messages) alongside a 3-participant null-sender
    message that ALSO canonicalizes to the same 2-party id
    (direct__contact_zhangsan___staff_a). The timeline for that id must
    return BOTH (they are both genuinely part of this bucket) -- the bug
    was that the null-sender message was silently dropped while the
    unrelated real pair message was kept, making the null-sender message
    permanently unreachable. Confirm the fix returns exactly both, with no
    third phantom message and no loss.
    """
    from app.routers.conversations import _direct_conv_id
    from app.main import app as _app

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")

    m_real = _insert_message(
        db, sender="staff_a", msgtime=10, content_text="real-pair-message", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_real.id, "contact_zhangsan", tenant_id=_TENANT_A)

    m_null = _insert_message(
        db, sender=None, msgtime=20, content_text="three-party-null-message", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_null.id, "staff_a", tenant_id=_TENANT_A)
    _insert_recipient(db, m_null.id, "contact_zhangsan", tenant_id=_TENANT_A)
    # A third contact that sorts AFTER "contact_zhangsan" -- so
    # sorted(contact_set)[0] in _derive_conversation_membership's two-party
    # branch still picks "contact_zhangsan", keeping this message's
    # canonical id identical to the unrelated real pair's id above. (A
    # third contact sorting BEFORE "contact_zhangsan", e.g.
    # "contact_lisi", would instead canonicalize to a DIFFERENT id --
    # that is correct behavior, not this test's scenario.)
    _insert_recipient(db, m_null.id, "contact_zzz_extra", tenant_id=_TENANT_A)

    assert _resolved_conv_id(db, _TENANT_A, m_null.id) == conv_id

    row = _list_row_for(db, "staff_a", _TENANT_A, conv_id)
    assert row["message_count"] == 2

    resp = _messages_via_endpoint(client, _app, db, conv_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    texts = {mm["content_text"] for mm in data["messages"]}
    assert texts == {"real-pair-message", "three-party-null-message"}
    assert len(data["messages"]) == 2 == row["message_count"]


# --- 2. Query narrowing -------------------------------------------------------


def test_null_sender_gap_fix_query_narrowing_normal_direct_conversation(db: Session) -> None:
    """Ticket section 6 "Query narrowing": for a normal direct conversation
    with NO null-sender messages at all, the gap-fix candidate query must
    (a) actually carry a sender-null/empty restriction (proven by
    inspecting the executed SQL text) and (b) return zero candidate rows,
    and the overall statement count for resolving the conversation must
    stay small and bounded, not scale with an unrestricted scan.
    """
    from sqlalchemy import event
    from app.routers.conversations import _direct_conv_id, _fetch_conversation_messages

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
    for i in range(10):
        m = _insert_message(db, sender="staff_a", msgtime=i, content_text=f"d{i}", tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    engine = db.get_bind()
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        fetched = _fetch_conversation_messages(db, conv_id, _TENANT_A)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert len(fetched) == 10

    null_predicate_statements = [
        s for s in statements if "archive_messages" in s and "sender" in s and "NULL" in s.upper()
    ]
    assert null_predicate_statements, (
        "expected at least one statement restricting sender IS NULL/'' for the "
        f"null-sender gap-fix candidate query; got statements={statements}"
    )

    # Bounded statement count: group lookup + direct pair lookup + ONE merged
    # null-sender candidate query (not two, per-token) + recipients map for
    # that candidate query's own (empty) result set. Comfortably under a
    # tenant-wide-scan-sized count.
    assert len(statements) <= 6, f"expected a small, bounded statement count; got {statements}"


def test_null_sender_gap_fix_merges_two_token_queries_into_one(db: Session) -> None:
    """The null-sender-multi-recipient-gap-fix call site issues ONE merged
    candidate query covering both uid_a and uid_b tokens, not two separate
    round trips -- proven by counting how many statements reference the
    archive_message_recipients join during a request where a genuine
    null-sender candidate exists (so the query cannot short-circuit to
    zero rows before running)."""
    from sqlalchemy import event
    from app.routers.conversations import _direct_conv_id, _fetch_conversation_messages

    conv_id = _direct_conv_id("contact_zhangsan", "contact_lisi")
    m = _insert_message(db, sender=None, msgtime=10, content_text="pair-null", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_lisi", tenant_id=_TENANT_A)

    engine = db.get_bind()
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        fetched = _fetch_conversation_messages(db, conv_id, _TENANT_A)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert {mm.id for mm in fetched} == {m.id}

    # The merged null-sender candidate query is distinguishable from
    # _fetch_direct_pair_messages's own two (unrelated, single-token)
    # recipient joins by its shape: it matches recipients via
    # "receiver_userid IN (...)" against BOTH tokens in one statement,
    # rather than a single "receiver_userid = ?" per statement. Expect
    # exactly one such IN-shaped statement, not two separate per-token
    # "=" statements.
    merged_candidate_statements = [
        s for s in statements
        if "archive_message_recipients" in s and "receiver_userid IN" in s
    ]
    assert len(merged_candidate_statements) == 1, (
        f"expected exactly one merged candidate query joining recipients via IN, got "
        f"{len(merged_candidate_statements)}: {statements}"
    )
    single_token_recipient_statements = [
        s for s in statements
        if "archive_message_recipients" in s and "receiver_userid =" in s
    ]
    # _fetch_direct_pair_messages's own two single-token joins (a->b, b->a) --
    # unaffected by the gap-fix merge, still present.
    assert len(single_token_recipient_statements) == 2


# --- 3. Media context propagation --------------------------------------------


def _insert_media_file(
    db: Session,
    archive_message_id: int,
    tenant_id: str,
    sdkfileid: str,
    local_path: str,
) -> MediaFile:
    mf = MediaFile(
        tenant_id=tenant_id,
        sdkfileid=sdkfileid,
        archive_message_id=archive_message_id,
        download_status="downloaded",
        local_path=local_path,
        file_type="image", file_size=1,
    )
    db.add(mf)
    db.flush()
    return mf


def _authed_media(app, db_session, tenant_id):
    from app.auth import get_current_user
    from app.db.session import get_db

    def _db_gen():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), tenant_id)
    app.dependency_overrides[get_db] = _db_gen


def test_contextual_timeline_media_url_carries_entity_context(client, db: Session, monkeypatch, tmp_path) -> None:
    """Ticket blocker 3, item 1: when the timeline request itself carried
    entity context, the generated media_url/media_access_url must carry
    the same mode/staff_id/conversation_type as query params, properly
    encoded."""
    from urllib.parse import urlparse, parse_qs
    from app.main import app as _app

    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "photo.jpg").write_bytes(b"bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    msg = _insert_message(
        db, msgtype="image", sender="staff_a", msgtime=10, sdkfileid="sdk-ctx-1", tenant_id=_TENANT_A
    )
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-ctx-1", str(media_root / "photo.jpg"))

    from app.routers.conversations import _direct_conv_id

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")

    resp = _messages_via_endpoint(
        client, _app, db, conv_id, mode="staff", staff_id="staff_a", conversation_type="direct"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1
    media_url = data["messages"][0]["media_url"]
    media_access_url = data["messages"][0]["media_access_url"]
    assert media_url is not None and media_access_url is not None

    for url in (media_url, media_access_url):
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        assert qs.get("mode") == ["staff"]
        assert qs.get("staff_id") == ["staff_a"]
        assert qs.get("conversation_type") == ["direct"]

    # Without entity context on the request, generated URLs carry no
    # context query string at all (unchanged, byte-identical to previous
    # rounds).
    resp_legacy = _messages_via_endpoint(client, _app, db, conv_id)
    assert resp_legacy.status_code == 200
    legacy_url = resp_legacy.json()["messages"][0]["media_url"]
    assert "?" not in legacy_url


def test_media_endpoint_accepts_and_resolves_with_entity_context(client, db: Session, monkeypatch, tmp_path) -> None:
    """Ticket blocker 3, item 2: the media endpoint itself accepts the same
    mode/staff_id/contact_id/conversation_type params and resolves
    correctly with them."""
    from app.main import app as _app

    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "photo.jpg").write_bytes(b"real-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    msg = _insert_message(
        db, msgtype="image", sender="staff_a", msgtime=10, sdkfileid="sdk-ctx-2", tenant_id=_TENANT_A
    )
    _insert_recipient(db, msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _insert_media_file(db, msg.id, _TENANT_A, "sdk-ctx-2", str(media_root / "photo.jpg"))

    from app.routers.conversations import _direct_conv_id

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")

    _authed_media(_app, db, _TENANT_A)
    try:
        resp = client.get(
            f"/api/conversations/{conv_id}/messages/{msg.msgid}/media",
            params={"mode": "staff", "staff_id": "staff_a", "conversation_type": "direct"},
        )
    finally:
        _app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.content == b"real-bytes"


def test_asymmetric_collision_media_does_not_leak_opposite_side(client, db: Session, monkeypatch, tmp_path) -> None:
    """Ticket blocker 3, item 3: message A (direct side only) has media,
    message B (group side only) has media, same collision conversation_id.
    An entity that only participates in the direct side must 404 for
    message B's media (and vice versa), even knowing the exact msgid."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "direct.jpg").write_bytes(b"direct-bytes")
    (media_root / "group.jpg").write_bytes(b"group-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    collision_id = _direct_conv_id("contact_lisi", "staff_a")

    m_direct = _insert_message(
        db, msgtype="image", sender="staff_a", msgtime=10, sdkfileid="sdk-direct",
        tenant_id=_TENANT_A,
    )
    _insert_recipient(db, m_direct.id, "contact_lisi", tenant_id=_TENANT_A)
    _insert_media_file(db, m_direct.id, _TENANT_A, "sdk-direct", str(media_root / "direct.jpg"))

    m_group = _insert_message(
        db, msgtype="image", sender="staff_a", roomid=collision_id, msgtime=20,
        sdkfileid="sdk-group", tenant_id=_TENANT_A,
    )
    _insert_recipient(db, m_group.id, "contact_wangwu", tenant_id=_TENANT_A)
    _insert_media_file(db, m_group.id, _TENANT_A, "sdk-group", str(media_root / "group.jpg"))

    _authed_media(_app, db, _TENANT_A)
    try:
        # contact_lisi (direct-side only) requesting the GROUP-side
        # message's media -> 404, even though it's a real, downloaded row.
        resp_leak_1 = client.get(
            f"/api/conversations/{collision_id}/messages/{m_group.msgid}/media",
            params={"mode": "contact", "contact_id": "contact_lisi"},
        )
        # contact_wangwu (group-side only) requesting the DIRECT-side
        # message's media -> 404.
        resp_leak_2 = client.get(
            f"/api/conversations/{collision_id}/messages/{m_direct.msgid}/media",
            params={"mode": "contact", "contact_id": "contact_wangwu"},
        )
        # Correct-side requests succeed.
        resp_ok_1 = client.get(
            f"/api/conversations/{collision_id}/messages/{m_direct.msgid}/media",
            params={"mode": "contact", "contact_id": "contact_lisi"},
        )
        resp_ok_2 = client.get(
            f"/api/conversations/{collision_id}/messages/{m_group.msgid}/media",
            params={"mode": "contact", "contact_id": "contact_wangwu"},
        )
        # staff_a participates in both sides -> both accessible.
        resp_both_1 = client.get(
            f"/api/conversations/{collision_id}/messages/{m_direct.msgid}/media",
            params={"mode": "staff", "staff_id": "staff_a"},
        )
        resp_both_2 = client.get(
            f"/api/conversations/{collision_id}/messages/{m_group.msgid}/media",
            params={"mode": "staff", "staff_id": "staff_a"},
        )
    finally:
        _app.dependency_overrides.clear()

    assert resp_leak_1.status_code == 404
    assert resp_leak_2.status_code == 404
    assert resp_ok_1.status_code == 200 and resp_ok_1.content == b"direct-bytes"
    assert resp_ok_2.status_code == 200 and resp_ok_2.content == b"group-bytes"
    assert resp_both_1.status_code == 200 and resp_both_1.content == b"direct-bytes"
    assert resp_both_2.status_code == 200 and resp_both_2.content == b"group-bytes"


def test_legacy_ambiguous_media_request_returns_400(client, db: Session, monkeypatch, tmp_path) -> None:
    """Ticket blocker 3, item 4: a legacy ID-only media request on a
    genuine direct/group collision raises the same 400
    _fetch_conversation_messages already raises for the ID-only timeline
    case -- not swallowed into a 404 or any other status."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "direct.jpg").write_bytes(b"direct-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    collision_id = _direct_conv_id("contact_lisi", "staff_a")

    m_direct = _insert_message(
        db, msgtype="image", sender="staff_a", msgtime=10, sdkfileid="sdk-direct-legacy",
        tenant_id=_TENANT_A,
    )
    _insert_recipient(db, m_direct.id, "contact_lisi", tenant_id=_TENANT_A)
    _insert_media_file(db, m_direct.id, _TENANT_A, "sdk-direct-legacy", str(media_root / "direct.jpg"))

    m_group = _insert_message(
        db, msgtype="image", sender="staff_a", roomid=collision_id, msgtime=20,
        sdkfileid="sdk-group-legacy", tenant_id=_TENANT_A,
    )
    _insert_recipient(db, m_group.id, "contact_wangwu", tenant_id=_TENANT_A)

    _authed_media(_app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/{collision_id}/messages/{m_direct.msgid}/media")
    finally:
        _app.dependency_overrides.clear()

    assert resp.status_code == 400


def test_media_context_tenant_isolation(client, db: Session, monkeypatch, tmp_path) -> None:
    """Ticket blocker 3, item 5 / section 6: overlapping conversation_id
    and msgid strings across two tenants, with entity context supplied --
    tenant A must never receive tenant B's media, or vice versa."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "a.jpg").write_bytes(b"tenant-a-bytes")
    (media_root / "b.jpg").write_bytes(b"tenant-b-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")

    msg_a = _insert_message(
        db, msgid="shared-msgid", msgtype="image", sender="staff_a", msgtime=10,
        sdkfileid="sdk-a", tenant_id=_TENANT_A,
    )
    _insert_recipient(db, msg_a.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _insert_media_file(db, msg_a.id, _TENANT_A, "sdk-a", str(media_root / "a.jpg"))

    msg_b = _insert_message(
        db, msgid="shared-msgid", msgtype="image", sender="staff_a", msgtime=10,
        sdkfileid="sdk-b", tenant_id=_TENANT_B,
    )
    _insert_recipient(db, msg_b.id, "contact_zhangsan", tenant_id=_TENANT_B)
    _insert_media_file(db, msg_b.id, _TENANT_B, "sdk-b", str(media_root / "b.jpg"))

    _authed_media(_app, db, _TENANT_A)
    try:
        resp_a = client.get(
            f"/api/conversations/{conv_id}/messages/shared-msgid/media",
            params={"mode": "staff", "staff_id": "staff_a"},
        )
    finally:
        _app.dependency_overrides.clear()

    _authed_media(_app, db, _TENANT_B)
    try:
        resp_b = client.get(
            f"/api/conversations/{conv_id}/messages/shared-msgid/media",
            params={"mode": "staff", "staff_id": "staff_a"},
        )
    finally:
        _app.dependency_overrides.clear()

    assert resp_a.status_code == 200 and resp_a.content == b"tenant-a-bytes"
    assert resp_b.status_code == 200 and resp_b.content == b"tenant-b-bytes"


# --- 4. conversation_type policy ----------------------------------------------


def test_conversation_type_valid_value_passes(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
    m = _insert_message(db, sender="staff_a", msgtime=10, content_text="hi", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    resp = _messages_via_endpoint(client, _app, db, conv_id, conversation_type="direct")
    assert resp.status_code == 200

    room_msg = _insert_message(
        db, sender="staff_a", roomid="room1", msgtime=10, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, room_msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    resp_group = _messages_via_endpoint(client, _app, db, "room1", conversation_type="group")
    assert resp_group.status_code == 200


def test_conversation_type_garbage_value_400(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
    m = _insert_message(db, sender="staff_a", msgtime=10, content_text="hi", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    resp = _messages_via_endpoint(client, _app, db, conv_id, conversation_type="bogus")
    assert resp.status_code == 400


def test_conversation_type_mismatch_returns_400(client, db: Session) -> None:
    """Caller supplies conversation_type=group for an id that resolves to
    a pure direct conversation (no roomid on any resolved message) -- 400,
    not silently served."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
    m = _insert_message(db, sender="staff_a", msgtime=10, content_text="hi", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    resp = _messages_via_endpoint(client, _app, db, conv_id, conversation_type="group")
    assert resp.status_code == 400

    room_msg = _insert_message(
        db, sender="staff_a", roomid="room2", msgtime=10, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, room_msg.id, "contact_zhangsan", tenant_id=_TENANT_A)
    resp2 = _messages_via_endpoint(client, _app, db, "room2", conversation_type="direct")
    assert resp2.status_code == 400


# ===========================================================================
# RND-158 Phase 2 data-loss recovery -- rounds 2-6 regression coverage,
# recreated against the CURRENT (round-7-fixed) implementation from the
# round-by-round QA report descriptions. See the recovery task's report for
# the full incident writeup; the helpers/tests below exercise the same
# scenarios/invariants the original (destroyed) tests covered, written
# fresh against this file's existing fixture-building conventions.
# ===========================================================================


def _conversations_via_endpoint(client, app, db, tenant_id: str = _TENANT_A, **params):
    """Exercise the REAL GET /api/conversations endpoint against a real
    SQLite session -- the list-side counterpart to _messages_via_endpoint."""
    from app.auth import get_current_user
    from app.db.session import get_db

    def _override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), tenant_id)
    app.dependency_overrides[get_db] = _override_db
    try:
        resp = client.get("/api/conversations", params=params)
    finally:
        app.dependency_overrides.clear()
    return resp


def _authoritative_conversations(db: Session, entity_id: str, tenant_id: str) -> list[dict]:
    """Reference: old full-fetch path via _fetch_messages_for_entity +
    _build_conversation_list -- the full-list counterpart to
    _authoritative_count above."""
    from app.routers.conversations import (
        _fetch_messages_for_entity,
        _load_recipients_map,
        _build_conversation_list,
    )

    messages = _fetch_messages_for_entity(db, entity_id, tenant_id)
    if not messages:
        return []
    recipients_map = _load_recipients_map(db, tenant_id, [m.id for m in messages])
    return _build_conversation_list(messages, recipients_map, {}, None)


def _assert_full_equivalence(db: Session, entity_id: str, tenant_id: str, msg: str) -> None:
    auth_rows = _authoritative_conversations(db, entity_id, tenant_id)
    opt_rows = _optimized_conversations(db, entity_id, tenant_id)
    assert auth_rows == opt_rows, (
        f"Full equivalence failure for '{msg}': authoritative={auth_rows}, optimized={opt_rows}"
    )


# ---------------------------------------------------------------------------
# Round 2: deterministic tie-break coverage (recreated after round 7 data
# loss) -- (msgtime, id) for latest-message selection within a conversation
# bucket, (last_message_time, conversation_id) for overall conversation
# ordering. See _build_conversation_list's two sort-key docstrings.
# ---------------------------------------------------------------------------


def test_tiebreak_equal_timestamp_direct_conversation_higher_id_wins(db: Session) -> None:
    T = 1000
    msg1 = _insert_message(db, sender="staff_a", msgtime=T, content_text="first", tenant_id=_TENANT_A)
    _insert_recipient(db, msg1.id, "contact_zhangsan", tenant_id=_TENANT_A)
    msg2 = _insert_message(db, sender="staff_a", msgtime=T, content_text="second", tenant_id=_TENANT_A)
    _insert_recipient(db, msg2.id, "contact_zhangsan", tenant_id=_TENANT_A)
    assert msg2.id > msg1.id

    auth_rows = _authoritative_conversations(db, "staff_a", _TENANT_A)
    opt_rows = _optimized_conversations(db, "staff_a", _TENANT_A)
    assert len(auth_rows) == len(opt_rows) == 1
    assert auth_rows[0] == opt_rows[0]
    assert auth_rows[0]["last_message_text"] == "second"


def test_tiebreak_equal_timestamp_group_conversation_higher_id_wins(db: Session) -> None:
    T = 2000
    msg1 = _insert_message(
        db, sender="staff_a", roomid="room_x", msgtime=T, content_text="g-first", tenant_id=_TENANT_A
    )
    _insert_recipient(db, msg1.id, "contact_zhangsan", tenant_id=_TENANT_A)
    msg2 = _insert_message(
        db, sender="staff_a", roomid="room_x", msgtime=T, content_text="g-second", tenant_id=_TENANT_A
    )
    _insert_recipient(db, msg2.id, "contact_lisi", tenant_id=_TENANT_A)
    assert msg2.id > msg1.id

    auth_rows = _authoritative_conversations(db, "staff_a", _TENANT_A)
    opt_rows = _optimized_conversations(db, "staff_a", _TENANT_A)
    assert len(auth_rows) == len(opt_rows) == 1
    assert auth_rows[0] == opt_rows[0]
    assert auth_rows[0]["last_message_text"] == "g-second"


def test_tiebreak_conversation_ordering_equal_last_message_time_conversation_id_desc(
    db: Session,
) -> None:
    """3+ separate conversations sharing the exact same last_message_time --
    final ordering falls back to conversation_id descending."""
    T = 5000
    for roomid in ("room_aaa", "room_zzz", "room_mmm"):
        m = _insert_message(db, sender="staff_a", roomid=roomid, msgtime=T, content_text=roomid, tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    rows = _optimized_conversations(db, "staff_a", _TENANT_A)
    assert len(rows) == 3
    assert all(r["last_message_time"] == T for r in rows)
    conv_ids = [r["conversation_id"] for r in rows]
    assert conv_ids == sorted(conv_ids, reverse=True)

    auth_rows = _authoritative_conversations(db, "staff_a", _TENANT_A)
    assert [r["conversation_id"] for r in auth_rows] == conv_ids


def test_tiebreak_mixed_direct_and_group_equal_timestamps_deterministic_order(db: Session) -> None:
    T = 7000
    m1 = _insert_message(db, sender="staff_a", msgtime=T, content_text="direct-msg", tenant_id=_TENANT_A)
    _insert_recipient(db, m1.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m2 = _insert_message(
        db, sender="staff_a", roomid="room_y", msgtime=T, content_text="group-msg", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m2.id, "contact_lisi", tenant_id=_TENANT_A)

    expected = [r["conversation_id"] for r in _optimized_conversations(db, "staff_a", _TENANT_A)]
    for _ in range(3):
        rows = _optimized_conversations(db, "staff_a", _TENANT_A)
        assert [r["conversation_id"] for r in rows] == expected


def test_tiebreak_repeated_execution_stable(db: Session) -> None:
    """Repeated execution of the same conversation-building path against
    unchanged data returns an identical conversation_id sequence every
    time, for both the optimized and authoritative paths."""
    for i in range(5):
        m = _insert_message(
            db, sender="staff_a", roomid=f"room_{i}", msgtime=1000 + i, content_text=f"m{i}", tenant_id=_TENANT_A
        )
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    sequences = []
    for _ in range(5):
        rows = _optimized_conversations(db, "staff_a", _TENANT_A)
        sequences.append(tuple(r["conversation_id"] for r in rows))
    assert len(set(sequences)) == 1

    auth_sequences = []
    for _ in range(5):
        rows = _authoritative_conversations(db, "staff_a", _TENANT_A)
        auth_sequences.append(tuple(r["conversation_id"] for r in rows))
    assert len(set(auth_sequences)) == 1
    assert sequences[0] == auth_sequences[0]


def test_tiebreak_mode_staff_end_to_end_deterministic(client, db: Session) -> None:
    from app.main import app as _app

    T = 3000
    for roomid in ("room_e1", "room_e2", "room_e3"):
        m = _insert_message(db, sender="staff_a", roomid=roomid, msgtime=T, content_text=roomid, tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    orders = []
    for _ in range(3):
        resp = _conversations_via_endpoint(client, _app, db, mode="staff", staff_id="staff_a")
        assert resp.status_code == 200
        orders.append(tuple(c["conversation_id"] for c in resp.json()))
    assert len(set(orders)) == 1


def test_tiebreak_mode_contact_end_to_end_deterministic(client, db: Session) -> None:
    from app.main import app as _app

    T = 4000
    for sender in ("staff_a", "staff_b", "staff_c"):
        m = _insert_message(db, sender=sender, msgtime=T, content_text=sender, tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    orders = []
    for _ in range(3):
        resp = _conversations_via_endpoint(client, _app, db, mode="contact", contact_id="contact_zhangsan")
        assert resp.status_code == 200
        orders.append(tuple(c["conversation_id"] for c in resp.json()))
    assert len(set(orders)) == 1


# ---------------------------------------------------------------------------
# Round 3: SQL ordering (ORDER BY msgtime ASC, id ASC on both fetch paths)
# + collision determinism (group beats direct, order-independent).
# ---------------------------------------------------------------------------


def test_full_equiv_direct_group_key_collision(db: Session) -> None:
    from app.routers.conversations import _direct_conv_id

    collision_roomid = _direct_conv_id("contact_zhangsan", "staff_a")
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_roomid, msgtime=20, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)

    _assert_full_equivalence(db, "staff_a", _TENANT_A, "direct/group collision")
    rows = _optimized_conversations(db, "staff_a", _TENANT_A)
    assert len(rows) == 1
    assert rows[0]["conversation_type"] == "group"
    assert rows[0]["message_count"] == 2


def test_full_equiv_direct_group_key_collision_group_first(db: Session) -> None:
    """Same logical fixture as above, but the group message is inserted
    (and so has a LOWER id, i.e. would be processed first in an id-ordered
    scan) before the direct message -- result must be structurally
    identical: type=group, message_count=2, latest text driven by msgtime
    (20 > 10) regardless of insertion order."""
    from app.routers.conversations import _direct_conv_id

    collision_roomid = _direct_conv_id("contact_zhangsan", "staff_a")
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_roomid, msgtime=20, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)

    _assert_full_equivalence(db, "staff_a", _TENANT_A, "direct/group collision group-first")
    rows = _optimized_conversations(db, "staff_a", _TENANT_A)
    assert len(rows) == 1
    assert rows[0]["conversation_type"] == "group"
    assert rows[0]["message_count"] == 2
    assert rows[0]["last_message_text"] == "g"


def test_collision_type_order_independence_identical_result() -> None:
    """Pure-Python-level test bypassing the DB layer entirely: calling
    _build_conversation_list with the message list in one order, then
    again with the list reversed, must produce byte-identical output."""
    from app.routers.conversations import _build_conversation_list, _direct_conv_id

    collision_roomid = _direct_conv_id("contact_zhangsan", "staff_a")
    msg_direct = _msg(1, "staff_a", roomid=None, msgtime=10, content_text="d")
    msg_group = _msg(2, "staff_a", roomid=collision_roomid, msgtime=20, content_text="g")
    recipients_map = {1: ["contact_zhangsan"], 2: ["contact_lisi"]}

    forward = _build_conversation_list([msg_direct, msg_group], recipients_map, {}, {"staff_a"})
    backward = _build_conversation_list([msg_group, msg_direct], recipients_map, {}, {"staff_a"})
    assert forward == backward
    assert len(forward) == 1
    assert forward[0]["conversation_type"] == "group"
    assert forward[0]["message_count"] == 2


def test_sql_order_contract_out_of_order_insertion_authoritative_path(db: Session) -> None:
    """Insert ArchiveMessage rows with deliberately scrambled msgtime
    values (not in insertion order), then call _fetch_messages_for_entity
    directly and assert the ACTUALLY RETURNED row sequence is
    (msgtime, id) ascending."""
    from app.routers.conversations import _fetch_messages_for_entity

    scrambled = [500, 100, 300, 200, 400]
    for t in scrambled:
        m = _insert_message(db, sender="staff_a", msgtime=t, content_text=str(t), tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    fetched = _fetch_messages_for_entity(db, "staff_a", _TENANT_A)
    assert len(fetched) == 5
    pairs = [(m.msgtime, m.id) for m in fetched]
    assert pairs == sorted(pairs)


def test_sql_order_contract_out_of_order_insertion_compact_path(db: Session) -> None:
    from app.routers.conversations import _fetch_compact_messages_for_entity

    scrambled = [500, 100, 300, 200, 400]
    for t in scrambled:
        m = _insert_message(db, sender="staff_a", msgtime=t, content_text=str(t), tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    fetched = _fetch_compact_messages_for_entity(db, "staff_a", _TENANT_A)
    assert len(fetched) == 5
    pairs = [(m.msgtime, m.id) for m in fetched]
    assert pairs == sorted(pairs)


def test_sql_order_contract_out_of_order_insertion_group(db: Session) -> None:
    """Same scrambled-insertion ordering contract, but for messages inside
    a shared group room, checked via both the full and compact fetch
    paths."""
    from app.routers.conversations import _fetch_messages_for_entity, _fetch_compact_messages_for_entity

    scrambled = [500, 100, 300, 200, 400]
    for t in scrambled:
        m = _insert_message(
            db, sender="staff_a", roomid="room_scrambled", msgtime=t, content_text=str(t), tenant_id=_TENANT_A
        )
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    full_fetched = _fetch_messages_for_entity(db, "staff_a", _TENANT_A)
    compact_fetched = _fetch_compact_messages_for_entity(db, "staff_a", _TENANT_A)
    assert len(full_fetched) == len(compact_fetched) == 5
    full_pairs = [(m.msgtime, m.id) for m in full_fetched]
    compact_pairs = [(m.msgtime, m.id) for m in compact_fetched]
    assert full_pairs == sorted(full_pairs)
    assert compact_pairs == sorted(compact_pairs)
    assert full_pairs == compact_pairs


def test_cross_tenant_equal_timestamp_collision_isolation(db: Session) -> None:
    """Tenant A has only a direct-shaped message using participant strings
    that collide with tenant B's canonical key; tenant B has BOTH a direct
    and a group-shaped message at the same colliding roomid/participant
    strings. Tenant A must stay 'direct' (unaffected by B's group
    message); tenant B must resolve to 'group'. Zero cross-tenant leakage
    of collision-type resolution."""
    from app.routers.conversations import _direct_conv_id

    collision_roomid = _direct_conv_id("contact_zhangsan", "staff_a")

    m_a = _insert_message(db, sender="staff_a", msgtime=10, content_text="a-direct", tenant_id=_TENANT_A)
    _insert_recipient(db, m_a.id, "contact_zhangsan", tenant_id=_TENANT_A)

    m_b_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="b-direct", tenant_id=_TENANT_B)
    _insert_recipient(db, m_b_direct.id, "contact_zhangsan", tenant_id=_TENANT_B)
    m_b_group = _insert_message(
        db, sender="staff_a", roomid=collision_roomid, msgtime=20, content_text="b-group", tenant_id=_TENANT_B
    )
    _insert_recipient(db, m_b_group.id, "contact_lisi", tenant_id=_TENANT_B)

    rows_a = _optimized_conversations(db, "staff_a", _TENANT_A)
    rows_b = _optimized_conversations(db, "staff_a", _TENANT_B)

    assert len(rows_a) == 1
    assert rows_a[0]["conversation_type"] == "direct"
    assert rows_a[0]["message_count"] == 1

    assert len(rows_b) == 1
    assert rows_b[0]["conversation_type"] == "group"
    assert rows_b[0]["message_count"] == 2


# ---------------------------------------------------------------------------
# Round 4: collision-timeline membership merging -- the list's "group
# wins" bucket must be openable via the timeline, not silently routed to
# direct-only. See _fetch_conversation_messages's docstring.
# ---------------------------------------------------------------------------


def test_collision_timeline_membership_merge_basic(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, collision_id)
    assert row["conversation_type"] == "group"
    assert row["message_count"] == 2

    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 2 == row["message_count"]
    assert {mm["content_text"] for mm in data["messages"]} == {"d", "g"}


def test_collision_timeline_direct_has_later_msgtime(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=10, content_text="g-early", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)
    m_direct = _insert_message(db, sender="staff_a", msgtime=20, content_text="d-late", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, collision_id)
    assert row["last_message_text"] == "d-late"
    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 2 == row["message_count"]
    texts = {mm["content_text"] for mm in data["messages"]}
    assert texts == {"g-early", "d-late"}
    assert row["last_message_text"] in texts


def test_collision_timeline_group_has_later_msgtime(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d-early", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, content_text="g-late", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, collision_id)
    assert row["last_message_text"] == "g-late"
    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 2 == row["message_count"]
    texts = {mm["content_text"] for mm in data["messages"]}
    assert texts == {"d-early", "g-late"}
    assert row["last_message_text"] in texts


def test_collision_timeline_equal_timestamp(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    T = 50
    m_direct = _insert_message(db, sender="staff_a", msgtime=T, content_text="d-eq", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=T, content_text="g-eq", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, collision_id)
    # (msgtime, id) tie-break: m_group has the higher id (inserted last).
    assert row["last_message_text"] == "g-eq"

    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 2 == row["message_count"]
    assert row["last_message_text"] in {mm["content_text"] for mm in data["messages"]}


def test_collision_timeline_direct_first_insertion_order(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, collision_id)
    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    assert row["conversation_type"] == "group"
    assert row["message_count"] == 2 == len(resp.json()["messages"])


def test_collision_timeline_group_first_insertion_order(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, collision_id)
    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    assert row["conversation_type"] == "group"
    assert row["message_count"] == 2 == len(resp.json()["messages"])


def test_normal_direct_timeline_no_collision_unchanged(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
    m = _insert_message(db, sender="staff_a", msgtime=10, content_text="hi", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, conv_id)
    assert row["conversation_type"] == "direct"
    resp = _messages_via_endpoint(client, _app, db, conv_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]
    assert data["messages"][0]["content_text"] == "hi"


def test_normal_group_timeline_no_collision_unchanged(client, db: Session) -> None:
    from app.main import app as _app

    m = _insert_message(
        db, sender="staff_a", roomid="plain_room", msgtime=10, content_text="hi-group", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, "plain_room")
    assert row["conversation_type"] == "group"
    resp = _messages_via_endpoint(client, _app, db, "plain_room", mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]
    assert data["messages"][0]["content_text"] == "hi-group"


def test_prefix_shaped_room_without_direct_collision_no_false_merge(client, db: Session) -> None:
    """A group roomid literally named like a direct-shaped id
    ('direct__something___else') with NO actual direct pair matching that
    string -- must open as pure group with only its own messages, no
    false-positive merge."""
    from app.main import app as _app

    roomid = "direct__something___else"
    m = _insert_message(db, sender="staff_a", roomid=roomid, msgtime=10, content_text="only-group", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, roomid)
    assert row["conversation_type"] == "group"
    assert row["message_count"] == 1

    resp = _messages_via_endpoint(client, _app, db, roomid, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]
    assert data["messages"][0]["content_text"] == "only-group"


def test_direct_conversation_without_group_collision_no_false_merge(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
    m = _insert_message(db, sender="staff_a", msgtime=10, content_text="plain-direct", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, conv_id)
    assert row["conversation_type"] == "direct"
    resp = _messages_via_endpoint(client, _app, db, conv_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]


@pytest.mark.parametrize(
    "fixture_name",
    [
        "normal_direct",
        "normal_group",
        "collision",
        "repeated_messages",
        "equal_timestamps",
        "multi_recipient",
        "null_sender",
    ],
)
def test_message_count_consistency_sweep(client, db: Session, fixture_name: str) -> None:
    """For a variety of conversation shapes, list_row.message_count must
    equal the timeline's returned message count, and the list's
    latest-message text must be present among the timeline's messages."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    entity = "staff_a"
    mode_kwargs = {"mode": "staff", "staff_id": entity}

    if fixture_name == "normal_direct":
        conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
        m = _insert_message(db, sender="staff_a", msgtime=10, content_text="t", tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)
    elif fixture_name == "normal_group":
        conv_id = "sweep_room"
        m = _insert_message(db, sender="staff_a", roomid=conv_id, msgtime=10, content_text="t", tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)
    elif fixture_name == "collision":
        conv_id = _direct_conv_id("contact_zhangsan", "staff_a")
        m1 = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
        _insert_recipient(db, m1.id, "contact_zhangsan", tenant_id=_TENANT_A)
        m2 = _insert_message(db, sender="staff_a", roomid=conv_id, msgtime=20, content_text="g", tenant_id=_TENANT_A)
        _insert_recipient(db, m2.id, "contact_lisi", tenant_id=_TENANT_A)
    elif fixture_name == "repeated_messages":
        conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
        for i in range(5):
            m = _insert_message(db, sender="staff_a", msgtime=10 + i, content_text=f"r{i}", tenant_id=_TENANT_A)
            _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)
    elif fixture_name == "equal_timestamps":
        conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
        for i in range(3):
            m = _insert_message(db, sender="staff_a", msgtime=99, content_text=f"eq{i}", tenant_id=_TENANT_A)
            _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)
    elif fixture_name == "multi_recipient":
        m = _insert_message(db, sender="staff_a", msgtime=10, content_text="multi", tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_lisi", tenant_id=_TENANT_A)
        conv_id = _resolved_conv_id(db, _TENANT_A, m.id)
    else:  # null_sender
        m = _insert_message(db, sender=None, msgtime=10, content_text="ns", tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "staff_a", tenant_id=_TENANT_A)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)
        conv_id = _resolved_conv_id(db, _TENANT_A, m.id)

    row = _list_row_for(db, entity, _TENANT_A, conv_id)
    resp = _messages_via_endpoint(client, _app, db, conv_id, **mode_kwargs)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert row["message_count"] == len(data["messages"])
    timeline_texts = {mm["content_text"] for mm in data["messages"]}
    assert row["last_message_text"] in timeline_texts


def test_collision_merge_tenant_isolation_ids(client, db: Session) -> None:
    """Two tenants with deliberately overlapping conversation ids/room
    ids/staff/contact ids/timestamps -- collision-merge logic must never
    leak across the tenant boundary."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")

    m_a_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="a-d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_a_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_a_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, content_text="a-g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_a_group.id, "contact_lisi", tenant_id=_TENANT_A)

    m_b_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="b-d", tenant_id=_TENANT_B)
    _insert_recipient(db, m_b_direct.id, "contact_zhangsan", tenant_id=_TENANT_B)

    row_a = _list_row_for(db, "staff_a", _TENANT_A, collision_id)
    row_b = _list_row_for(db, "staff_a", _TENANT_B, collision_id)
    assert row_a["conversation_type"] == "group"
    assert row_a["message_count"] == 2
    assert row_b["conversation_type"] == "direct"
    assert row_b["message_count"] == 1

    resp_a = _messages_via_endpoint(client, _app, db, collision_id, tenant_id=_TENANT_A, mode="staff", staff_id="staff_a")
    resp_b = _messages_via_endpoint(client, _app, db, collision_id, tenant_id=_TENANT_B, mode="staff", staff_id="staff_a")
    assert resp_a.status_code == 200 and resp_b.status_code == 200
    texts_a = {mm["content_text"] for mm in resp_a.json()["messages"]}
    texts_b = {mm["content_text"] for mm in resp_b.json()["messages"]}
    assert texts_a == {"a-d", "a-g"}
    assert texts_b == {"b-d"}
    assert texts_a.isdisjoint(texts_b)


def test_collision_merge_tenant_isolation_staff_contact_overlap(client, db: Session) -> None:
    """Same staff_id/contact_id strings reused across two tenants, both
    with a genuine collision -- each tenant's timeline must only ever
    return its own messages."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")

    for tenant, tag in ((_TENANT_A, "A"), (_TENANT_B, "B")):
        m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text=f"{tag}-d", tenant_id=tenant)
        _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=tenant)
        m_group = _insert_message(
            db, sender="staff_a", roomid=collision_id, msgtime=20, content_text=f"{tag}-g", tenant_id=tenant
        )
        _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=tenant)

    resp_a = _messages_via_endpoint(client, _app, db, collision_id, tenant_id=_TENANT_A, mode="staff", staff_id="staff_a")
    resp_b = _messages_via_endpoint(client, _app, db, collision_id, tenant_id=_TENANT_B, mode="staff", staff_id="staff_a")
    assert resp_a.status_code == 200 and resp_b.status_code == 200
    texts_a = {mm["content_text"] for mm in resp_a.json()["messages"]}
    texts_b = {mm["content_text"] for mm in resp_b.json()["messages"]}
    assert texts_a == {"A-d", "A-g"}
    assert texts_b == {"B-d", "B-g"}


# ---------------------------------------------------------------------------
# Round 5: prefix-shaped group navigation order + null-sender (one-sided)
# navigation contract. See _fetch_conversation_messages's numbered
# resolution-order docstring.
# ---------------------------------------------------------------------------


def test_prefix_shaped_room_no_separator_opens_as_group(client, db: Session) -> None:
    """A real group room literally named 'direct__room_only_group' (no
    '___' in the remainder) must resolve as group, not 400 on a malformed
    direct-parse attempt."""
    from app.main import app as _app

    roomid = "direct__room_only_group"
    m = _insert_message(db, sender="staff_a", roomid=roomid, msgtime=10, content_text="only", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, roomid)
    assert row["conversation_type"] == "group"
    assert row["message_count"] == 1

    resp = _messages_via_endpoint(client, _app, db, roomid, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]


def test_prefix_shaped_room_fake_two_part_shape_opens_as_group(client, db: Session) -> None:
    """A real group room whose remainder happens to parse into a 2-part
    shape but has no actual direct pair -- exact room membership wins
    (pure group, not misrouted through direct parsing)."""
    from app.main import app as _app

    roomid = "direct__fake___shape"
    m = _insert_message(
        db, sender="staff_a", roomid=roomid, msgtime=10, content_text="fake-shape", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, roomid)
    assert row["conversation_type"] == "group"

    resp = _messages_via_endpoint(client, _app, db, roomid, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]
    assert data["messages"][0]["content_text"] == "fake-shape"


def test_standard_direct_id_no_room_collision_unchanged(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
    m = _insert_message(db, sender="staff_a", msgtime=10, content_text="plain", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=_TENANT_A)

    resp = _messages_via_endpoint(client, _app, db, conv_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 1


def test_valid_direct_group_collision_still_resolves_after_round5(client, db: Session) -> None:
    """Re-verify round 4's collision-merge fix still works after round 5's
    resolution-order restructuring -- the highest-risk regression surface
    between these two rounds."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, collision_id)
    assert row["conversation_type"] == "group"
    assert row["message_count"] == 2

    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 2


def test_null_sender_one_sided_orphan_navigable(client, db: Session) -> None:
    """A one-sided null-sender list row (e.g. 'direct__contact_orphan')
    must be openable via the timeline: 200, count match, latest present,
    and no unrelated messages included."""
    from app.main import app as _app

    m = _insert_message(db, sender=None, msgtime=10, content_text="orphan-msg", tenant_id=_TENANT_A)
    _insert_recipient(db, m.id, "contact_orphan", tenant_id=_TENANT_A)

    conv_id = _resolved_conv_id(db, _TENANT_A, m.id)
    assert conv_id == "direct__contact_orphan"

    m_other = _insert_message(db, sender="staff_a", msgtime=15, content_text="unrelated", tenant_id=_TENANT_A)
    _insert_recipient(db, m_other.id, "contact_zhangsan", tenant_id=_TENANT_A)

    row = _list_row_for(db, "contact_orphan", _TENANT_A, conv_id)
    assert row["message_count"] == 1

    resp = _messages_via_endpoint(client, _app, db, conv_id, mode="contact", contact_id="contact_orphan")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]
    assert data["messages"][0]["content_text"] == "orphan-msg"
    assert "unrelated" not in {mm["content_text"] for mm in data["messages"]}


def test_prefix_shaped_room_cross_tenant_isolation(client, db: Session) -> None:
    from app.main import app as _app

    roomid = "direct__cross_tenant_room"
    m_a = _insert_message(db, sender="staff_a", roomid=roomid, msgtime=10, content_text="a-only", tenant_id=_TENANT_A)
    _insert_recipient(db, m_a.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_b = _insert_message(db, sender="staff_a", roomid=roomid, msgtime=10, content_text="b-only", tenant_id=_TENANT_B)
    _insert_recipient(db, m_b.id, "contact_zhangsan", tenant_id=_TENANT_B)

    resp_a = _messages_via_endpoint(client, _app, db, roomid, tenant_id=_TENANT_A, mode="staff", staff_id="staff_a")
    assert resp_a.status_code == 200
    texts_a = {mm["content_text"] for mm in resp_a.json()["messages"]}
    assert texts_a == {"a-only"}


def test_null_sender_cross_tenant_identity_isolation(client, db: Session) -> None:
    from app.main import app as _app

    m_a = _insert_message(db, sender=None, msgtime=10, content_text="a-orphan", tenant_id=_TENANT_A)
    _insert_recipient(db, m_a.id, "contact_shared_orphan", tenant_id=_TENANT_A)
    m_b = _insert_message(db, sender=None, msgtime=10, content_text="b-orphan", tenant_id=_TENANT_B)
    _insert_recipient(db, m_b.id, "contact_shared_orphan", tenant_id=_TENANT_B)

    conv_id = _resolved_conv_id(db, _TENANT_A, m_a.id)
    assert conv_id == _resolved_conv_id(db, _TENANT_B, m_b.id)

    resp_a = _messages_via_endpoint(
        client, _app, db, conv_id, tenant_id=_TENANT_A, mode="contact", contact_id="contact_shared_orphan"
    )
    assert resp_a.status_code == 200
    texts_a = {mm["content_text"] for mm in resp_a.json()["messages"]}
    assert texts_a == {"a-orphan"}


def test_prefix_and_null_sender_repeated_execution_stable(client, db: Session) -> None:
    """Run the prefix-shaped-group, prefix-with-fake-shape, and
    null-sender cases each 20x -- identical HTTP status/message-ids/
    ordering/count/latest-identity every run."""
    from app.main import app as _app

    roomid1 = "direct__stable_room_only"
    m1 = _insert_message(db, sender="staff_a", roomid=roomid1, msgtime=10, content_text="s1", tenant_id=_TENANT_A)
    _insert_recipient(db, m1.id, "contact_zhangsan", tenant_id=_TENANT_A)

    roomid2 = "direct__stable___fakeshape"
    m2 = _insert_message(db, sender="staff_a", roomid=roomid2, msgtime=10, content_text="s2", tenant_id=_TENANT_A)
    _insert_recipient(db, m2.id, "contact_zhangsan", tenant_id=_TENANT_A)

    m3 = _insert_message(db, sender=None, msgtime=10, content_text="s3", tenant_id=_TENANT_A)
    _insert_recipient(db, m3.id, "contact_stable_orphan", tenant_id=_TENANT_A)
    conv_id3 = _resolved_conv_id(db, _TENANT_A, m3.id)

    for _ in range(20):
        r1 = _messages_via_endpoint(client, _app, db, roomid1, mode="staff", staff_id="staff_a")
        r2 = _messages_via_endpoint(client, _app, db, roomid2, mode="staff", staff_id="staff_a")
        r3 = _messages_via_endpoint(client, _app, db, conv_id3, mode="contact", contact_id="contact_stable_orphan")
        assert r1.status_code == r2.status_code == r3.status_code == 200
        assert [mm["msgid"] for mm in r1.json()["messages"]] == [m1.msgid]
        assert [mm["msgid"] for mm in r2.json()["messages"]] == [m2.msgid]
        assert [mm["msgid"] for mm in r3.json()["messages"]] == [m3.msgid]


# ---------------------------------------------------------------------------
# Round 6: entity-scoped API contract (mode/staff_id/contact_id/
# conversation_type on the timeline endpoint) + multi-recipient (2-party)
# null-sender fix.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "recipients,label",
    [
        (["staff_a", "contact_zhangsan"], "staff_contact"),
        (["contact_zhangsan", "contact_lisi"], "two_contacts"),
        (["staff_a", "staff_b"], "two_staff"),
    ],
)
def test_multi_recipient_null_sender_2party_resolves(client, db: Session, recipients, label) -> None:
    """A null-sender message with exactly two recipients (one staff + one
    contact, two contacts, or two staff) forms a valid 2-part canonical id
    -- must resolve end-to-end via the standard 2-party _direct_conv_id
    path, not the null-sender one-sided fallback."""
    from app.main import app as _app

    m = _insert_message(db, sender=None, msgtime=10, content_text=f"2p-{label}", tenant_id=_TENANT_A)
    for uid in recipients:
        _insert_recipient(db, m.id, uid, tenant_id=_TENANT_A)

    conv_id = _resolved_conv_id(db, _TENANT_A, m.id)
    assert conv_id.startswith("direct__") and "___" in conv_id[len("direct__"):]

    selected = recipients[0]
    mode = "staff" if selected.startswith("staff_") else "contact"
    kwargs = {"mode": mode}
    if mode == "staff":
        kwargs["staff_id"] = selected
    else:
        kwargs["contact_id"] = selected

    row = _list_row_for(db, selected, _TENANT_A, conv_id)
    assert row["message_count"] == 1

    resp = _messages_via_endpoint(client, _app, db, conv_id, **kwargs)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]
    assert data["messages"][0]["content_text"] == f"2p-{label}"


def test_entity_asymmetric_collision_contact_direct_side_only(client, db: Session) -> None:
    """Contact participates in the direct side ONLY (not the group side).
    For that contact, list count includes only the direct side, and the
    contextual timeline (mode=contact&contact_id=X) includes only the
    direct side -- no group-side leakage."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_wangwu", tenant_id=_TENANT_A)

    row = _list_row_for(db, "contact_zhangsan", _TENANT_A, collision_id)
    assert row["message_count"] == 1
    assert row["last_message_text"] == "d"

    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="contact", contact_id="contact_zhangsan")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]
    assert data["messages"][0]["content_text"] == "d"


def test_entity_asymmetric_collision_staff_group_side_only(client, db: Session) -> None:
    """Staff participates in the group side ONLY."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_group = _insert_message(
        db, sender="staff_b", roomid=collision_id, msgtime=20, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_wangwu", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_b", _TENANT_A, collision_id)
    assert row["message_count"] == 1
    assert row["last_message_text"] == "g"

    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="staff", staff_id="staff_b")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1 == row["message_count"]
    assert data["messages"][0]["content_text"] == "g"


def test_entity_symmetric_collision_both_sides_merged(client, db: Session) -> None:
    """Selected entity participates in BOTH sides -- list and timeline both
    include both, deduplicated, count equality."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, content_text="g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=_TENANT_A)

    row = _list_row_for(db, "staff_a", _TENANT_A, collision_id)
    assert row["message_count"] == 2

    resp = _messages_via_endpoint(client, _app, db, collision_id, mode="staff", staff_id="staff_a")
    assert resp.status_code == 200
    data = resp.json()
    ids = [mm["msgid"] for mm in data["messages"]]
    assert len(ids) == len(set(ids)) == 2 == row["message_count"]


def test_every_row_navigability_staff_and_contact_modes(client, db: Session) -> None:
    """For a dataset covering normal direct, normal group, and symmetric
    collision, open EVERY list row via the exact contextual request in
    BOTH staff mode and contact mode: 200, message_count == timeline
    count, latest present, no duplicate ids, no cross-entity/cross-tenant
    leakage."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    m1 = _insert_message(db, sender="staff_a", msgtime=10, content_text="nd", tenant_id=_TENANT_A)
    _insert_recipient(db, m1.id, "contact_zhangsan", tenant_id=_TENANT_A)

    m2 = _insert_message(
        db, sender="staff_a", roomid="every_row_room", msgtime=10, content_text="ng", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m2.id, "contact_zhangsan", tenant_id=_TENANT_A)

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    m3 = _insert_message(db, sender="staff_a", msgtime=30, content_text="cd", tenant_id=_TENANT_A)
    _insert_recipient(db, m3.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m4 = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=40, content_text="cg", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m4.id, "contact_zhangsan", tenant_id=_TENANT_A)

    for entity, mode in (("staff_a", "staff"), ("contact_zhangsan", "contact")):
        rows = _optimized_conversations(db, entity, _TENANT_A)
        assert rows, "expected at least one visible row"
        seen_conv_ids = set()
        for row in rows:
            kwargs = {"mode": mode}
            if mode == "staff":
                kwargs["staff_id"] = entity
            else:
                kwargs["contact_id"] = entity
            resp = _messages_via_endpoint(client, _app, db, row["conversation_id"], **kwargs)
            assert resp.status_code == 200, (row, resp.text)
            data = resp.json()
            assert row["message_count"] == len(data["messages"])
            ids = [mm["msgid"] for mm in data["messages"]]
            assert len(ids) == len(set(ids))
            texts = {mm["content_text"] for mm in data["messages"]}
            assert row["last_message_text"] in texts
            seen_conv_ids.add(row["conversation_id"])
        assert len(seen_conv_ids) == len(rows)


def test_legacy_id_only_collision_returns_400_tenant_scoped(client, db: Session) -> None:
    """Legacy ID-only behavior on a genuine collision (no entity context)
    now returns 400 -- and the SAME id string is tenant-scoped: ambiguous
    in tenant A but unambiguous (pure direct, no colliding group room) in
    tenant B."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")

    m_a_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="a-d", tenant_id=_TENANT_A)
    _insert_recipient(db, m_a_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    m_a_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, content_text="a-g", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_a_group.id, "contact_lisi", tenant_id=_TENANT_A)

    m_b_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text="b-d", tenant_id=_TENANT_B)
    _insert_recipient(db, m_b_direct.id, "contact_zhangsan", tenant_id=_TENANT_B)

    resp_a = _messages_via_endpoint(client, _app, db, collision_id, tenant_id=_TENANT_A)
    resp_b = _messages_via_endpoint(client, _app, db, collision_id, tenant_id=_TENANT_B)
    assert resp_a.status_code == 400
    assert resp_b.status_code == 200
    assert len(resp_b.json()["messages"]) == 1


def test_entity_scoped_collision_cross_tenant_isolation(client, db: Session) -> None:
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_zhangsan", "staff_a")
    for tenant, tag in ((_TENANT_A, "A"), (_TENANT_B, "B")):
        m_direct = _insert_message(db, sender="staff_a", msgtime=10, content_text=f"{tag}-d", tenant_id=tenant)
        _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=tenant)
        m_group = _insert_message(
            db, sender="staff_a", roomid=collision_id, msgtime=20, content_text=f"{tag}-g", tenant_id=tenant
        )
        _insert_recipient(db, m_group.id, "contact_lisi", tenant_id=tenant)

    resp_a = _messages_via_endpoint(client, _app, db, collision_id, tenant_id=_TENANT_A, mode="staff", staff_id="staff_a")
    resp_b = _messages_via_endpoint(client, _app, db, collision_id, tenant_id=_TENANT_B, mode="staff", staff_id="staff_a")
    assert resp_a.status_code == 200 and resp_b.status_code == 200
    texts_a = {mm["content_text"] for mm in resp_a.json()["messages"]}
    texts_b = {mm["content_text"] for mm in resp_b.json()["messages"]}
    assert texts_a == {"A-d", "A-g"}
    assert texts_b == {"B-d", "B-g"}


def test_2party_null_sender_cross_tenant_isolation(client, db: Session) -> None:
    from app.main import app as _app

    msg_ids = {}
    for tenant, tag in ((_TENANT_A, "A"), (_TENANT_B, "B")):
        m = _insert_message(db, sender=None, msgtime=10, content_text=f"{tag}-2p", tenant_id=tenant)
        _insert_recipient(db, m.id, "staff_a", tenant_id=tenant)
        _insert_recipient(db, m.id, "contact_zhangsan", tenant_id=tenant)
        msg_ids[tenant] = m.id

    conv_id = _resolved_conv_id(db, _TENANT_A, msg_ids[_TENANT_A])
    assert conv_id == _resolved_conv_id(db, _TENANT_B, msg_ids[_TENANT_B])

    resp_a = _messages_via_endpoint(client, _app, db, conv_id, tenant_id=_TENANT_A, mode="staff", staff_id="staff_a")
    resp_b = _messages_via_endpoint(client, _app, db, conv_id, tenant_id=_TENANT_B, mode="staff", staff_id="staff_a")
    assert resp_a.status_code == 200 and resp_b.status_code == 200
    texts_a = {mm["content_text"] for mm in resp_a.json()["messages"]}
    texts_b = {mm["content_text"] for mm in resp_b.json()["messages"]}
    assert texts_a == {"A-2p"}
    assert texts_b == {"B-2p"}


# ===========================================================================
# RND-158 Phase 2 QA round 8 -- two blockers:
#   1. Entity scoping was only applied inside the genuine direct/group
#      collision branch, never for a pure direct-shaped canonical bucket
#      (step 4 of _fetch_conversation_messages) -- a message that merely
#      canonicalizes to the same "direct__a___b" id as the requested
#      bucket (e.g. an unrelated ordinary direct message sharing that
#      exact pair) was always included regardless of whether the specific
#      entity_id asking actually sent or received it. Fixed by filtering
#      direct_messages through the same _entity_seed_ids(...) membership
#      test the collision branch already used, when entity context is
#      supplied. The SAME any()-gate-then-return-whole-list flaw existed,
#      latent, in the collision branch itself (blocker 1's own "also
#      verify" section) -- fixed there too with a precise per-message
#      filter instead of an any() gate.
#   2. The two media routes (get_message_media, get_message_media_access)
#      validated conversation_type's shape but never cross-checked it
#      against the actually-resolved type of the specific message being
#      served -- fixed once, inside the shared _resolve_authorized_media,
#      so both routes get the same consistency check the timeline route
#      already has.
# ===========================================================================


def _three_party_with_competing_direct_fixture(
    db: Session,
    *,
    null_recipients: list[str],
    competing_sender: str,
    competing_recipient: str,
    tenant_id: str = _TENANT_A,
    null_msgtime: int = 20,
    competing_msgtime: int = 10,
    null_text: str = "null-sender-msg",
    competing_text: str = "ordinary-direct-msg",
):
    """Ticket fixture A/B shape: a null-sender message whose recipients
    span 3 distinct participants, PLUS a genuinely unrelated ordinary
    direct message between exactly the two participants the null-sender
    message's canonical id collapses to (per _derive_conversation_membership's
    lossy two-party canonicalization -- see that function's docstring).
    Both messages provably canonicalize to the identical conversation_id;
    the third null-sender-only participant is not part of the competing
    ordinary message at all.
    """
    m_null = _insert_message(
        db, sender=None, msgtime=null_msgtime, content_text=null_text, tenant_id=tenant_id
    )
    for uid in null_recipients:
        _insert_recipient(db, m_null.id, uid, tenant_id=tenant_id)

    m_direct = _insert_message(
        db, sender=competing_sender, msgtime=competing_msgtime, content_text=competing_text,
        tenant_id=tenant_id,
    )
    _insert_recipient(db, m_direct.id, competing_recipient, tenant_id=tenant_id)

    conv_id = _resolved_conv_id(db, tenant_id, m_null.id)
    assert conv_id == _resolved_conv_id(db, tenant_id, m_direct.id), (
        "fixture invariant: both messages must canonicalize to the exact "
        "same conversation_id for this test to exercise the collision "
        "gap this round fixes"
    )
    return conv_id, m_null, m_direct


def test_selected_third_contact_excludes_competing_direct(client, db: Session) -> None:
    """Ticket fixture A exactly: null sender + recipients {staff_a,
    contact_first, contact_second}; competing ordinary direct
    staff_a->contact_first shares the same canonical id. Selected
    mode=contact&contact_id=contact_second (the participant the canonical
    id text silently drops) must see ONLY the null-sender message: list
    count=1, timeline count=1, null-sender message present, ordinary
    direct message ABSENT, latest matches, no duplicates.
    """
    from app.main import app as _app

    conv_id, m_null, m_direct = _three_party_with_competing_direct_fixture(
        db,
        null_recipients=["staff_a", "contact_first", "contact_second"],
        competing_sender="staff_a",
        competing_recipient="contact_first",
    )

    row = _list_row_for(db, "contact_second", _TENANT_A, conv_id)
    assert row["message_count"] == 1
    assert row["last_message_text"] == "null-sender-msg"

    resp = _messages_via_endpoint(
        client, _app, db, conv_id, mode="contact", contact_id="contact_second"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    ids = [mm["msgid"] for mm in data["messages"]]
    texts = {mm["content_text"] for mm in data["messages"]}
    assert len(data["messages"]) == 1 == row["message_count"]
    assert texts == {"null-sender-msg"}
    assert "ordinary-direct-msg" not in texts
    assert len(ids) == len(set(ids))
    assert row["last_message_text"] in texts


def test_selected_third_staff_excludes_competing_direct(client, db: Session) -> None:
    """Ticket fixture B, staff-side mirror: null sender + recipients
    {contact_a, staff_first, staff_second}; competing ordinary direct
    staff_first->contact_a shares the same canonical id. Selected
    mode=staff&staff_id=staff_second must see ONLY the null-sender
    message -- same assertions as the contact-side fixture."""
    from app.main import app as _app

    conv_id, m_null, m_direct = _three_party_with_competing_direct_fixture(
        db,
        null_recipients=["contact_a", "staff_first", "staff_second"],
        competing_sender="staff_first",
        competing_recipient="contact_a",
    )

    row = _list_row_for(db, "staff_second", _TENANT_A, conv_id)
    assert row["message_count"] == 1
    assert row["last_message_text"] == "null-sender-msg"

    resp = _messages_via_endpoint(
        client, _app, db, conv_id, mode="staff", staff_id="staff_second"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    ids = [mm["msgid"] for mm in data["messages"]]
    texts = {mm["content_text"] for mm in data["messages"]}
    assert len(data["messages"]) == 1 == row["message_count"]
    assert texts == {"null-sender-msg"}
    assert "ordinary-direct-msg" not in texts
    assert len(ids) == len(set(ids))
    assert row["last_message_text"] in texts


def test_canonical_pair_participants_still_see_ordinary_direct_message(
    client, db: Session
) -> None:
    """Critical "don't over-filter" check (ticket regression #3): for the
    SAME fixture as the selected-third-contact test, the two participants
    who ARE the canonical pair (contact_first, staff_a) must still
    legitimately see BOTH messages -- the ordinary direct message is a
    real message they sent/received, and must not be dropped by the new
    filter. Counts must match each entity's own list bucket count."""
    from app.main import app as _app

    conv_id, m_null, m_direct = _three_party_with_competing_direct_fixture(
        db,
        null_recipients=["staff_a", "contact_first", "contact_second"],
        competing_sender="staff_a",
        competing_recipient="contact_first",
    )

    for entity, mode in (("contact_first", "contact"), ("staff_a", "staff")):
        row = _list_row_for(db, entity, _TENANT_A, conv_id)
        assert row["message_count"] == 2, (entity, row)

        kwargs = {"mode": mode}
        if mode == "staff":
            kwargs["staff_id"] = entity
        else:
            kwargs["contact_id"] = entity
        resp = _messages_via_endpoint(client, _app, db, conv_id, **kwargs)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        texts = {mm["content_text"] for mm in data["messages"]}
        ids = [mm["msgid"] for mm in data["messages"]]
        assert len(data["messages"]) == 2 == row["message_count"], (entity, data)
        assert texts == {"null-sender-msg", "ordinary-direct-msg"}
        assert len(ids) == len(set(ids))


def test_three_party_null_sender_without_competing_direct_still_correct(
    client, db: Session
) -> None:
    """Regression guard against round 6/7's existing 3-party null-sender
    coverage: the SAME 3-party null-sender fixture WITHOUT any competing
    ordinary direct message must remain correct after this round's new
    entity-scoped filter at step 4 -- the filter must not accidentally
    drop the null-sender message itself when there is nothing else in the
    bucket to filter out."""
    from app.main import app as _app

    m_null = _insert_message(
        db, sender=None, msgtime=15, content_text="solo-null", tenant_id=_TENANT_A
    )
    for uid in ["staff_a", "contact_first", "contact_second"]:
        _insert_recipient(db, m_null.id, uid, tenant_id=_TENANT_A)

    conv_id = _resolved_conv_id(db, _TENANT_A, m_null.id)

    for entity, mode in (
        ("staff_a", "staff"),
        ("contact_first", "contact"),
        ("contact_second", "contact"),
    ):
        row = _list_row_for(db, entity, _TENANT_A, conv_id)
        assert row["message_count"] == 1

        kwargs = {"mode": mode}
        if mode == "staff":
            kwargs["staff_id"] = entity
        else:
            kwargs["contact_id"] = entity
        resp = _messages_via_endpoint(client, _app, db, conv_id, **kwargs)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["messages"]) == 1 == row["message_count"]
        assert data["messages"][0]["content_text"] == "solo-null"


def test_selected_third_contact_sees_none_of_multiple_competing_directs(
    client, db: Session
) -> None:
    """Multiple competing direct messages for the canonical pair (several
    staff_a->contact_first messages at different times) -- the selected
    third participant (contact_second) must see NONE of them, only their
    own null-sender message(s)."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    conv_id = _direct_conv_id("staff_a", "contact_first")

    m_null = _insert_message(
        db, sender=None, msgtime=100, content_text="the-null-one", tenant_id=_TENANT_A
    )
    for uid in ["staff_a", "contact_first", "contact_second"]:
        _insert_recipient(db, m_null.id, uid, tenant_id=_TENANT_A)

    for i, t in enumerate([10, 20, 30, 40]):
        m = _insert_message(
            db, sender="staff_a", msgtime=t, content_text=f"competing-{i}", tenant_id=_TENANT_A
        )
        _insert_recipient(db, m.id, "contact_first", tenant_id=_TENANT_A)

    assert _resolved_conv_id(db, _TENANT_A, m_null.id) == conv_id

    row = _list_row_for(db, "contact_second", _TENANT_A, conv_id)
    assert row["message_count"] == 1

    resp = _messages_via_endpoint(
        client, _app, db, conv_id, mode="contact", contact_id="contact_second"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    texts = {mm["content_text"] for mm in data["messages"]}
    assert len(data["messages"]) == 1 == row["message_count"]
    assert texts == {"the-null-one"}
    for i in range(4):
        assert f"competing-{i}" not in texts


def test_canonical_pair_view_deterministic_ordering_with_equal_timestamps(
    client, db: Session
) -> None:
    """Ensure deterministic (msgtime, id) ordering is preserved when the
    entity-filtered set still has 2+ messages sharing the exact same
    msgtime -- exercised via contact_first's view, which legitimately
    includes both the null-sender and ordinary direct message at the same
    msgtime."""
    from app.main import app as _app

    conv_id, m_null, m_direct = _three_party_with_competing_direct_fixture(
        db,
        null_recipients=["staff_a", "contact_first", "contact_second"],
        competing_sender="staff_a",
        competing_recipient="contact_first",
        null_msgtime=50,
        competing_msgtime=50,
    )

    resp = _messages_via_endpoint(
        client, _app, db, conv_id, mode="contact", contact_id="contact_first"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["messages"]) == 2
    # Deterministic (msgtime, id) tie-break: the fixture helper inserts
    # m_null then m_direct, so m_null.id < m_direct.id -- with equal
    # msgtime, ascending id order means m_null sorts before m_direct.
    # Derive the expected order directly from the real inserted ids
    # rather than assuming insertion order, so this test can't silently
    # pass for the wrong reason if the fixture helper ever changes.
    ids_in_order = [mm["msgid"] for mm in data["messages"]]
    by_msgid_id = {m_null.msgid: m_null.id, m_direct.msgid: m_direct.id}
    expected_order = sorted([m_direct.msgid, m_null.msgid], key=lambda mid: by_msgid_id[mid])
    assert ids_in_order == expected_order

    # Repeat execution must be stable.
    resp2 = _messages_via_endpoint(
        client, _app, db, conv_id, mode="contact", contact_id="contact_first"
    )
    assert [mm["msgid"] for mm in resp2.json()["messages"]] == ids_in_order


def test_entity_scoped_step4_tenant_isolation(client, db: Session) -> None:
    """The same participant-id strings / canonical-id collision
    reproduced in a second tenant -- confirm strict isolation: tenant A's
    entity-scoped resolution never returns tenant B's messages, and each
    tenant's third-participant filtering is independently correct."""
    from app.main import app as _app

    conv_id_a, m_null_a, m_direct_a = _three_party_with_competing_direct_fixture(
        db,
        null_recipients=["staff_a", "contact_first", "contact_second"],
        competing_sender="staff_a",
        competing_recipient="contact_first",
        tenant_id=_TENANT_A,
        null_text="A-null",
        competing_text="A-direct",
    )
    conv_id_b, m_null_b, m_direct_b = _three_party_with_competing_direct_fixture(
        db,
        null_recipients=["staff_a", "contact_first", "contact_second"],
        competing_sender="staff_a",
        competing_recipient="contact_first",
        tenant_id=_TENANT_B,
        null_text="B-null",
        competing_text="B-direct",
    )
    assert conv_id_a == conv_id_b

    resp_a = _messages_via_endpoint(
        client, _app, db, conv_id_a, tenant_id=_TENANT_A, mode="contact", contact_id="contact_second"
    )
    resp_b = _messages_via_endpoint(
        client, _app, db, conv_id_b, tenant_id=_TENANT_B, mode="contact", contact_id="contact_second"
    )
    assert resp_a.status_code == 200 and resp_b.status_code == 200
    texts_a = {mm["content_text"] for mm in resp_a.json()["messages"]}
    texts_b = {mm["content_text"] for mm in resp_b.json()["messages"]}
    assert texts_a == {"A-null"}
    assert texts_b == {"B-null"}


def test_collision_branch_also_applies_per_message_entity_filter(
    client, db: Session
) -> None:
    """The identical latent flaw the ticket asks us to check for: inside
    the genuine direct/group collision branch (step 2), direct_messages
    can ITSELF be a mix of an ordinary direct message and an unrelated
    null-sender message under the same canonical id (exactly like step
    4), and a real group room additionally collides with that same id.
    A third participant who only appears in the null-sender message (not
    the ordinary direct message, not the group side) must see ONLY the
    null-sender message when resolving this id -- not the unrelated
    ordinary direct message merely because it shares the direct side's
    bucket."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    collision_id = _direct_conv_id("contact_first", "staff_a")

    # Ordinary ordinary direct message for the canonical pair.
    m_direct = _insert_message(
        db, sender="staff_a", msgtime=10, content_text="ordinary-direct", tenant_id=_TENANT_A
    )
    _insert_recipient(db, m_direct.id, "contact_first", tenant_id=_TENANT_A)

    # Null-sender message sharing the same canonical id but with a third
    # participant not present on the ordinary direct message.
    m_null = _insert_message(
        db, sender=None, msgtime=20, content_text="collision-null", tenant_id=_TENANT_A
    )
    for uid in ["staff_a", "contact_first", "contact_second"]:
        _insert_recipient(db, m_null.id, uid, tenant_id=_TENANT_A)

    # A real group room whose roomid literally equals the collision id.
    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=30, content_text="group-side",
        tenant_id=_TENANT_A,
    )
    _insert_recipient(db, m_group.id, "contact_wangwu", tenant_id=_TENANT_A)

    assert _resolved_conv_id(db, _TENANT_A, m_null.id) == collision_id

    resp = _messages_via_endpoint(
        client, _app, db, collision_id, mode="contact", contact_id="contact_second"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    texts = {mm["content_text"] for mm in data["messages"]}
    assert texts == {"collision-null"}
    assert "ordinary-direct" not in texts
    assert "group-side" not in texts


# ---------------------------------------------------------------------------
# Round 8 blocker 2: media conversation_type consistency check, applied
# once inside _resolve_authorized_media so both media routes share it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("media_route_suffix", ["media", "media/access"])
def test_media_conversation_type_matrix(
    client, db: Session, monkeypatch, tmp_path, media_route_suffix: str
) -> None:
    """Ticket section 8 required behavior matrix, exercised against both
    get_message_media and get_message_media_access:
      - valid direct+direct -> 200
      - valid group+group -> 200
      - invalid enum -> 400 (regression guard, already worked)
      - direct+group conflict -> 400 (this round's fix)
      - group+direct conflict -> 400 (this round's fix)
      - unambiguous legacy request with no conversation_type at all -> 200
        (unchanged)
    """
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "d.jpg").write_bytes(b"direct-bytes")
    (media_root / "g.jpg").write_bytes(b"group-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    direct_conv_id = _direct_conv_id("staff_a", "contact_zhangsan")
    m_direct = _insert_message(
        db, msgtype="image", sender="staff_a", msgtime=10, sdkfileid="sdk-matrix-d",
        tenant_id=_TENANT_A,
    )
    _insert_recipient(db, m_direct.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _insert_media_file(db, m_direct.id, _TENANT_A, "sdk-matrix-d", str(media_root / "d.jpg"))

    group_id = "matrix_group_room"
    m_group = _insert_message(
        db, msgtype="image", sender="staff_a", roomid=group_id, msgtime=10,
        sdkfileid="sdk-matrix-g", tenant_id=_TENANT_A,
    )
    _insert_recipient(db, m_group.id, "contact_zhangsan", tenant_id=_TENANT_A)
    _insert_media_file(db, m_group.id, _TENANT_A, "sdk-matrix-g", str(media_root / "g.jpg"))

    def _get(conv_id, msgid, **params):
        _authed_media(_app, db, _TENANT_A)
        try:
            return client.get(
                f"/api/conversations/{conv_id}/messages/{msgid}/{media_route_suffix}",
                params=params,
            )
        finally:
            _app.dependency_overrides.clear()

    # valid direct+direct -> 200
    resp = _get(direct_conv_id, m_direct.msgid, conversation_type="direct")
    assert resp.status_code == 200, resp.text

    # valid group+group -> 200
    resp = _get(group_id, m_group.msgid, conversation_type="group")
    assert resp.status_code == 200, resp.text

    # invalid enum -> 400
    resp = _get(direct_conv_id, m_direct.msgid, conversation_type="bogus")
    assert resp.status_code == 400

    # direct+group conflict -> 400 (this round's fix)
    resp = _get(direct_conv_id, m_direct.msgid, conversation_type="group")
    assert resp.status_code == 400, resp.text

    # group+direct conflict -> 400 (this round's fix)
    resp = _get(group_id, m_group.msgid, conversation_type="direct")
    assert resp.status_code == 400, resp.text

    # unambiguous legacy request, no conversation_type at all -> 200,
    # unchanged.
    resp = _get(direct_conv_id, m_direct.msgid)
    assert resp.status_code == 200, resp.text
    resp = _get(group_id, m_group.msgid)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("media_route_suffix", ["media", "media/access"])
def test_media_conversation_type_legacy_ambiguous_collision_still_400(
    client, db: Session, monkeypatch, tmp_path, media_route_suffix: str
) -> None:
    """Regression guard: a legacy ambiguous collision media request with
    no entity context and no conversation_type still 400s via
    _fetch_conversation_messages's existing ambiguity check -- unaffected
    by this round's conversation_type consistency addition."""
    from app.main import app as _app
    from app.routers.conversations import _direct_conv_id

    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "d.jpg").write_bytes(b"direct-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    collision_id = _direct_conv_id("contact_lisi", "staff_a")
    m_direct = _insert_message(
        db, msgtype="image", sender="staff_a", msgtime=10, sdkfileid="sdk-legacy-matrix",
        tenant_id=_TENANT_A,
    )
    _insert_recipient(db, m_direct.id, "contact_lisi", tenant_id=_TENANT_A)
    _insert_media_file(db, m_direct.id, _TENANT_A, "sdk-legacy-matrix", str(media_root / "d.jpg"))

    m_group = _insert_message(
        db, sender="staff_a", roomid=collision_id, msgtime=20, tenant_id=_TENANT_A,
    )
    _insert_recipient(db, m_group.id, "contact_wangwu", tenant_id=_TENANT_A)

    _authed_media(_app, db, _TENANT_A)
    try:
        resp = client.get(
            f"/api/conversations/{collision_id}/messages/{m_direct.msgid}/{media_route_suffix}"
        )
    finally:
        _app.dependency_overrides.clear()

    assert resp.status_code == 400
