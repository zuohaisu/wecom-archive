"""Service-level tests for RND-219 — app.services.listing_service.

Companion to tests/test_conversation_membership_service.py, same shape:

  1. Divergence guard: app.routers.conversations must reference the exact
     same function objects app.services.listing_service exposes, never a
     forked re-implementation (see that module's re-export comment).
  2. Import direction: the service must NOT import from app.routers.* (the
     dependency points the other way — the router imports the service).
  3. Direct, fixture-backed (real in-memory sqlite Session) coverage of
     list_monitored_accounts / list_contacts / list_conversations: seat
     active/history ranking + conversation_count, the contact set
     (participants minus staff), conversation-list aggregation including
     group-wins, display-name resolution, and tenant isolation — the exact
     behaviors RND-219's acceptance criteria call out.

Run (from backend/):
    pytest tests/test_listing_service.py -v
"""

from __future__ import annotations

import ast
import os
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.routers.conversations as rtr
import app.services.listing_service as svc


_SCHEMA_SQL = """
CREATE TABLE tenants (
    id TEXT PRIMARY KEY, name TEXT, slug TEXT, is_active INTEGER,
    deletion_locked INTEGER NOT NULL DEFAULT 0,
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
    deleted_at DATETIME, deleted_by_admin_user_id TEXT, delete_reason TEXT, purge_after DATETIME, restored_at DATETIME, restored_by_admin_user_id TEXT, deletion_batch_id TEXT,
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
    avatar_storage_backend TEXT, avatar_storage_ref TEXT,
    avatar_content_type TEXT, avatar_source TEXT, avatar_synced_at TEXT,
    avatar_status TEXT,
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


def _insert_message(db: Session, **kwargs):
    from app.db.models import ArchiveMessage

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


def _insert_recipient(db: Session, message_id: int, userid: str, **kwargs):
    from app.db.models import ArchiveMessageRecipient

    defaults = dict(tenant_id=_TENANT_A)
    defaults.update(kwargs)
    r = ArchiveMessageRecipient(message_id=message_id, receiver_userid=userid, **defaults)
    db.add(r)
    db.flush()
    return r


def _insert_contact(db: Session, wecom_userid: str, name: str, tenant_id: str = _TENANT_A):
    from app.db.models import Contact

    c = Contact(wecom_userid=wecom_userid, name=name, tenant_id=tenant_id)
    db.add(c)
    db.flush()
    return c


# ---------------------------------------------------------------------------
# Divergence / dependency-direction guards (same shape as
# test_conversation_membership_service.py)
# ---------------------------------------------------------------------------


def test_router_and_service_reference_same_function_objects() -> None:
    """The router must NOT carry a divergent, forked re-implementation of
    the six listing helpers it re-exports for backward compatibility."""
    shared = [
        "_build_conversation_list",
        "_compact_entity_messages",
        "_count_entity_conversations",
        "_fetch_compact_messages_for_entity",
        "_latest_own_participation_time",
        "_load_recipients_map_compact",
    ]
    for name in shared:
        router_obj = getattr(rtr, name, None)
        service_obj = getattr(svc, name, None)
        assert router_obj is not None, f"router missing {name}"
        assert service_obj is not None, f"service missing {name}"
        assert router_obj is service_obj, (
            f"{name} diverged: router and service reference different objects"
        )


def test_service_module_does_not_import_router() -> None:
    """Dependency direction: the service must not import app.routers.*."""
    source_path = svc.__file__
    assert source_path and os.path.exists(source_path)
    with open(source_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=source_path)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "app.routers" and not (
                node.module or ""
            ).startswith("app.routers."), (
                f"service imports from app.routers: {node.module}"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app.routers"), (
                    f"service imports app.routers: {alias.name}"
                )

    assert "app.routers.conversations" not in getattr(svc, "__dict__", {})


# ---------------------------------------------------------------------------
# list_monitored_accounts — seat detection, active/history ranking,
# conversation_count, tenant isolation
# ---------------------------------------------------------------------------


def test_list_monitored_accounts_ranks_active_first_and_counts_conversations() -> None:
    db = _make_session()
    try:
        # staff_a: two conversations (one direct, one group), latest at 500.
        m1 = _insert_message(db, sender="staff_a", msgtime=100, roomid=None)
        _insert_recipient(db, m1.id, "contact_x")
        m2 = _insert_message(db, sender="staff_a", msgtime=500, roomid="room_1")
        _insert_recipient(db, m2.id, "contact_y")

        # staff_b: one direct conversation, latest at 200 (older -> history).
        m3 = _insert_message(db, sender="staff_b", msgtime=200, roomid=None)
        _insert_recipient(db, m3.id, "contact_z")

        _insert_contact(db, "staff_a", "Staff A")
        _insert_contact(db, "staff_b", "Staff B")

        result = svc.list_monitored_accounts(db, _TENANT_A)
        by_id = {r.staff_id: r for r in result}

        assert by_id["staff_a"].seat_status == "active"
        assert by_id["staff_a"].is_active_archive_seat is True
        assert by_id["staff_a"].latest_message_time == 500
        assert by_id["staff_a"].conversation_count == 2
        assert by_id["staff_a"].display_name == "Staff A"

        assert by_id["staff_b"].seat_status == "history"
        assert by_id["staff_b"].is_active_archive_seat is False
        assert by_id["staff_b"].latest_message_time == 200
        assert by_id["staff_b"].conversation_count == 1
    finally:
        db.close()


def test_list_monitored_accounts_can_skip_unrendered_conversation_counts() -> None:
    """The console picker must not scan every selected group solely for a
    count it does not display; the complete-count API remains the default."""
    db = _make_session()
    try:
        msg = _insert_message(db, sender="staff_a", msgtime=100, roomid="room_1")
        _insert_recipient(db, msg.id, "contact_x")
        _insert_contact(db, "staff_a", "Staff A")

        with patch.object(
            svc,
            "_batch_count_entity_conversations",
            side_effect=AssertionError("count aggregation must be skipped"),
        ):
            result = svc.list_monitored_accounts(
                db,
                _TENANT_A,
                include_conversation_count=False,
            )

        assert len(result) == 1
        assert result[0].staff_id == "staff_a"
        assert result[0].latest_message_time == 100
        assert result[0].conversation_count is None
    finally:
        db.close()


def test_list_monitored_accounts_tenant_isolated() -> None:
    db = _make_session()
    try:
        m1 = _insert_message(db, sender="staff_a", msgtime=100, roomid=None, tenant_id=_TENANT_A)
        _insert_recipient(db, m1.id, "contact_x", tenant_id=_TENANT_A)

        m2 = _insert_message(db, sender="staff_a", msgtime=999, roomid=None, tenant_id=_TENANT_B)
        _insert_recipient(db, m2.id, "contact_x", tenant_id=_TENANT_B)

        result_a = svc.list_monitored_accounts(db, _TENANT_A)
        assert len(result_a) == 1
        assert result_a[0].latest_message_time == 100

        result_b = svc.list_monitored_accounts(db, _TENANT_B)
        assert len(result_b) == 1
        assert result_b[0].latest_message_time == 999
    finally:
        db.close()


# ---------------------------------------------------------------------------
# list_contacts — participants minus staff, tenant isolation
# ---------------------------------------------------------------------------


def test_list_contacts_excludes_staff_and_sorts_by_id() -> None:
    db = _make_session()
    try:
        m1 = _insert_message(db, sender="staff_a", msgtime=100, roomid=None)
        _insert_recipient(db, m1.id, "contact_zhangsan")
        m2 = _insert_message(db, sender="staff_a", msgtime=200, roomid=None)
        _insert_recipient(db, m2.id, "contact_lisi")

        _insert_contact(db, "contact_zhangsan", "Zhang San")
        _insert_contact(db, "contact_lisi", "Li Si")

        result = svc.list_contacts(db, _TENANT_A)
        ids = [c.contact_id for c in result]

        assert "staff_a" not in ids
        assert ids == sorted(ids)
        assert set(ids) == {"contact_zhangsan", "contact_lisi"}
        by_id = {c.contact_id: c for c in result}
        assert by_id["contact_zhangsan"].display_name == "Zhang San"
    finally:
        db.close()


def test_list_contacts_tenant_isolated() -> None:
    db = _make_session()
    try:
        m1 = _insert_message(db, sender="staff_a", msgtime=100, roomid=None, tenant_id=_TENANT_A)
        _insert_recipient(db, m1.id, "contact_a", tenant_id=_TENANT_A)

        m2 = _insert_message(db, sender="staff_a", msgtime=100, roomid=None, tenant_id=_TENANT_B)
        _insert_recipient(db, m2.id, "contact_b", tenant_id=_TENANT_B)

        result_a = svc.list_contacts(db, _TENANT_A)
        assert [c.contact_id for c in result_a] == ["contact_a"]

        result_b = svc.list_contacts(db, _TENANT_B)
        assert [c.contact_id for c in result_b] == ["contact_b"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# list_conversations — ordering, group-wins, display names, tenant isolation
# ---------------------------------------------------------------------------


def test_compact_conversation_fetch_limits_message_preview_to_response_contract() -> None:
    """A list row exposes at most 200 characters, so the compact query must
    not materialize an arbitrarily long archived message body."""
    db = _make_session()
    try:
        body = "消息" * 150
        msg = _insert_message(db, sender="staff_a", msgtime=100, content_text=body)
        _insert_recipient(db, msg.id, "contact_x")

        messages = svc._fetch_compact_messages_for_entity(db, "staff_a", _TENANT_A)

        assert len(messages) == 1
        assert messages[0].content_text == body[:200]
    finally:
        db.close()


def test_compact_conversation_fetch_expands_groups_without_a_full_id_round_trip() -> None:
    """Group expansion should feed rooms straight into the final projection,
    rather than first transferring every expanded message ID to Python and
    sending the same ID set back in a second SQL ``IN`` query."""
    db = _make_session()
    try:
        first = _insert_message(
            db,
            sender="staff_a",
            roomid="room_1",
            msgtime=100,
            content_text="seed",
        )
        _insert_recipient(db, first.id, "contact_x")
        for offset in range(1, 4):
            _insert_message(
                db,
                sender="contact_x",
                roomid="room_1",
                msgtime=100 + offset,
                content_text=f"expanded-{offset}",
            )

        statements: list[str] = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", capture)
        try:
            messages = svc._fetch_compact_messages_for_entity(db, "staff_a", _TENANT_A)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", capture)

        assert [message.content_text for message in messages] == [
            "seed",
            "expanded-1",
            "expanded-2",
            "expanded-3",
        ]
        # sender seed + recipient seed + seed shape + final room projection.
        # The previous implementation issued a fifth query to load every
        # expanded group message ID before issuing the final projection.
        assert len(statements) == 4
    finally:
        db.close()


def test_compact_staff_list_summarizes_group_history_without_loading_group_recipients() -> None:
    """The initial staff console needs one group card, not every historical
    group message and recipient row.  Direct-title semantics still remain."""
    db = _make_session()
    try:
        direct = _insert_message(
            db, sender="staff_a", msgtime=100, content_text="direct latest"
        )
        _insert_recipient(db, direct.id, "contact_direct")
        group_seed = _insert_message(
            db, sender="staff_a", roomid="room_1", msgtime=200, content_text="group old"
        )
        _insert_recipient(db, group_seed.id, "contact_group")
        for msgtime, text in ((300, "group newer"), (400, "group latest")):
            _insert_message(
                db,
                sender="contact_group",
                roomid="room_1",
                msgtime=msgtime,
                content_text=text,
            )
        _insert_contact(db, "contact_direct", "Direct Contact")

        recipient_ids_requested: list[list[int]] = []
        original_loader = svc._load_recipients_map_compact

        def track_recipients(session, tenant_id, message_ids):
            recipient_ids_requested.append(list(message_ids))
            return original_loader(session, tenant_id, message_ids)

        with patch.object(svc, "_load_recipients_map_compact", side_effect=track_recipients):
            conversations = svc.list_conversations(
                db,
                _TENANT_A,
                "staff_a",
                include_participant_metadata=False,
            )

        by_id = {conversation["conversation_id"]: conversation for conversation in conversations}
        direct_id = "direct__contact_direct___staff_a"
        assert recipient_ids_requested == [[direct.id]]
        assert by_id[direct_id]["display_name"] == "Direct Contact"
        assert by_id[direct_id]["message_count"] == 1
        assert by_id["room_1"]["last_message_text"] == "group latest"
        assert by_id["room_1"]["message_count"] == 3
        assert by_id["room_1"]["contact_ids"] == []
        assert by_id["room_1"]["monitored_account_ids"] == []
    finally:
        db.close()


def test_compact_staff_list_preserves_group_wins_collision_aggregation() -> None:
    """A rare direct ID matching a real room ID remains one group card with
    the combined count and deterministic latest preview."""
    db = _make_session()
    try:
        roomid = "direct__contact_x___staff_a"
        direct = _insert_message(db, sender="staff_a", msgtime=100, content_text="direct")
        _insert_recipient(db, direct.id, "contact_x")
        group = _insert_message(
            db,
            sender="staff_a",
            roomid=roomid,
            msgtime=200,
            content_text="group latest",
        )
        _insert_recipient(db, group.id, "contact_x")

        conversations = svc.list_conversations(
            db,
            _TENANT_A,
            "staff_a",
            include_participant_metadata=False,
        )

        assert len(conversations) == 1
        conversation = conversations[0]
        assert conversation["conversation_id"] == roomid
        assert conversation["conversation_type"] == "group"
        assert conversation["message_count"] == 2
        assert conversation["last_message_text"] == "group latest"
    finally:
        db.close()


def test_list_conversations_sorted_by_last_activity_desc() -> None:
    db = _make_session()
    try:
        m1 = _insert_message(db, sender="staff_a", msgtime=100, roomid=None)
        _insert_recipient(db, m1.id, "contact_x")
        m2 = _insert_message(db, sender="staff_a", msgtime=500, roomid="room_1")
        _insert_recipient(db, m2.id, "contact_y")

        result = svc.list_conversations(db, _TENANT_A, "staff_a")
        times = [c["last_message_time"] for c in result]
        assert times == sorted(times, reverse=True)
        assert times == [500, 100]
    finally:
        db.close()


def test_list_conversations_group_wins_collision() -> None:
    """A bucket that collects both a group-shaped and direct-shaped message
    (same canonical key) must end up conversation_type='group' regardless
    of which message was aggregated first — the group-wins contract
    _build_conversation_list documents."""
    db = _make_session()
    try:
        # Direct-shaped message first (msgtime 100), group-shaped message
        # for the *same* room second (msgtime 200) -- both must resolve to
        # the same conversation_id bucket via roomid "room_1".
        m1 = _insert_message(db, sender="staff_a", msgtime=100, roomid="room_1")
        _insert_recipient(db, m1.id, "contact_x")
        m2 = _insert_message(db, sender="contact_x", msgtime=200, roomid="room_1")
        _insert_recipient(db, m2.id, "staff_a")

        result = svc.list_conversations(db, _TENANT_A, "staff_a")
        assert len(result) == 1
        assert result[0]["conversation_type"] == "group"
        assert result[0]["avatar_url"] is None
        assert result[0]["avatar_status"] == "missing"
        assert result[0]["last_message_time"] == 200
    finally:
        db.close()


def test_list_conversations_resolves_display_names_and_controlled_direct_avatar() -> None:
    db = _make_session()
    try:
        m1 = _insert_message(db, sender="staff_a", msgtime=100, roomid=None)
        _insert_recipient(db, m1.id, "contact_zhangsan")
        contact = _insert_contact(db, "contact_zhangsan", "Zhang San")
        contact.avatar_storage_backend = "local"
        contact.avatar_storage_ref = "tenants/tenant-a/avatars/internal-contact_zhangsan.jpg"
        contact.avatar_content_type = "image/jpeg"
        contact.avatar_status = "ready"
        db.flush()

        result = svc.list_conversations(db, _TENANT_A, "staff_a")
        assert len(result) == 1
        assert result[0]["display_name"] == "Zhang San"
        assert result[0]["avatar_url"] == f"/api/admin/avatars/internal/{contact.id}"
        assert result[0]["avatar_status"] == "ready"
    finally:
        db.close()


def test_list_conversations_prefers_tenant_scoped_group_metadata_name() -> None:
    from app.db.models import GroupChatMetadata

    db = _make_session()
    try:
        message = _insert_message(db, sender="staff_a", msgtime=100, roomid="room-named")
        _insert_recipient(db, message.id, "contact_a")
        db.add(
            GroupChatMetadata(
                tenant_id=_TENANT_A,
                roomid="room-named",
                display_name="Support Team",
                source="wecom_external_groupchat",
                sync_status="resolved",
            )
        )
        db.commit()

        result = svc.list_conversations(db, _TENANT_A, "staff_a")
        assert result[0]["display_name"] == "Support Team"
        assert result[0]["room_display_name"] == "Support Team"
        assert result[0]["room_raw_id"] == "room-named"
    finally:
        db.close()


def test_list_conversations_tenant_isolated() -> None:
    db = _make_session()
    try:
        m1 = _insert_message(db, sender="staff_a", msgtime=100, roomid=None, tenant_id=_TENANT_A)
        _insert_recipient(db, m1.id, "contact_x", tenant_id=_TENANT_A)

        m2 = _insert_message(db, sender="staff_a", msgtime=999, roomid=None, tenant_id=_TENANT_B)
        _insert_recipient(db, m2.id, "contact_z", tenant_id=_TENANT_B)

        result_a = svc.list_conversations(db, _TENANT_A, "staff_a")
        assert len(result_a) == 1
        assert result_a[0]["last_message_time"] == 100

        result_b = svc.list_conversations(db, _TENANT_B, "staff_a")
        assert len(result_b) == 1
        assert result_b[0]["last_message_time"] == 999
    finally:
        db.close()
