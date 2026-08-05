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

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.routers.conversations as rtr
import app.services.listing_service as svc


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
        assert result[0]["last_message_time"] == 200
    finally:
        db.close()


def test_list_conversations_resolves_display_names() -> None:
    db = _make_session()
    try:
        m1 = _insert_message(db, sender="staff_a", msgtime=100, roomid=None)
        _insert_recipient(db, m1.id, "contact_zhangsan")
        _insert_contact(db, "contact_zhangsan", "Zhang San")

        result = svc.list_conversations(db, _TENANT_A, "staff_a")
        assert len(result) == 1
        assert result[0]["display_name"] == "Zhang San"
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
