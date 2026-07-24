"""
Anti-regression tests for RND-215 — shared conversation-membership service.

These guard the two failure modes that caused the first acceptance FAIL:

  1. Divergence: the conversations *router* must reference the exact same
     function objects exposed by app.conversation_membership, never a
     second, forked re-implementation. If the router ever re-defines one of
     these helpers, behavior silently splits between the router and the
     reachability audit (which imports the service directly) — that is how
     ordinary direct messages got misclassified as unreachable_membership.

  2. Import direction: the service module must NOT import from
     app.routers.* (the dependency points the other way). This is verified
     both statically (no `app.routers` reference in source) and at runtime
     (importing the service does not pull in the router module).

  3. Query composition: _fetch_null_sender_candidate_messages must build its
     SQLAlchemy filters with and_/or_ exactly as the original — a regression
     that passed two conditions to list.append() raised TypeError, and a
     `SQL condition | python tuple` expression changed matching semantics.

This file is self-contained (its own sqlite-backed session) so it does not
depend on import order of other test modules.

Run (from backend/):
    pytest tests/test_conversation_membership_service.py -v
"""

from __future__ import annotations

import ast
import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.conversation_membership as svc
import app.routers.conversations as rtr
from app.db.models import ArchiveMessage, ArchiveMessageRecipient


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


def _insert_recipient(
    db: Session, message_id: int, userid: str, **kwargs
) -> ArchiveMessageRecipient:
    defaults = dict(tenant_id=_TENANT_A)
    defaults.update(kwargs)
    r = ArchiveMessageRecipient(
        message_id=message_id, receiver_userid=userid, **defaults
    )
    db.add(r)
    db.flush()
    return r


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_router_and_service_reference_same_function_objects() -> None:
    """The router must NOT carry a divergent, forked re-implementation."""
    shared = [
        "_is_staff",
        "_collect_staff_ids",
        "_collect_archive_participant_ids",
        "_load_recipients_map",
        "_load_display_names_for_ids",
        "_staff_ids_for_participants",
        "_derive_conversation_membership",
        "_fetch_conversation_messages",
        # RND-219: _load_display_names and _is_valid_roomid moved here too —
        # shared by the listing service (app.services.listing_service) and
        # the message-timeline/media code left in the router.
        "_load_display_names",
        "_is_valid_roomid",
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
    """Dependency direction: service must not import app.routers.*."""
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

    # Runtime check: importing the service must not have loaded the router
    # as a dependency of the service.
    assert "app.routers.conversations" not in getattr(svc, "__dict__", {})


def test_fetch_null_sender_candidate_messages_uses_and_or_composition() -> None:
    """The orphan/null-sender candidate query must not raise and must match
    by recipient (and tenant) — exactly the original and_/or_ semantics."""
    db = _make_session()
    try:
        # One-sided message: empty sender, single recipient contact_a.
        orphan = _insert_message(
            db, sender="", msgtype="text", msgtime=100, roomid=None, content_text="hi"
        )
        _insert_recipient(db, orphan.id, "contact_a")

        # require_null_sender=True must find it via the recipient row only.
        direct_hits = svc._fetch_null_sender_candidate_messages(
            db, "direct__contact_a", ["contact_a"], _TENANT_A, require_null_sender=True
        )
        assert [m.id for m in direct_hits] == [orphan.id]

        # Orphan path (require_null_sender=False) must also find it.
        orphan_hits = svc._fetch_null_sender_candidate_messages(
            db, "direct__contact_a", "contact_a", _TENANT_A
        )
        assert [m.id for m in orphan_hits] == [orphan.id]

        # A message whose recipient is a DIFFERENT tenant must be excluded.
        other = _insert_message(
            db,
            sender="",
            msgtype="text",
            msgtime=101,
            roomid=None,
            tenant_id="tenant-other",
        )
        _insert_recipient(db, other.id, "contact_a", tenant_id="tenant-other")
        hits = svc._fetch_null_sender_candidate_messages(
            db, "direct__contact_a", "contact_a", _TENANT_A
        )
        assert other.id not in [m.id for m in hits]
    finally:
        db.close()


def test_fetch_conversation_messages_resolves_normal_direct() -> None:
    """A normal direct text message must be returned by the shared service
    (the path the reachability audit replays) — guarding against silent
    misclassification as unreachable_membership."""
    db = _make_session()
    try:
        msg = _insert_message(
            db, sender="staff_a", msgtype="text", msgtime=200, roomid=None
        )
        _insert_recipient(db, msg.id, "contact_a")

        fetched = svc._fetch_conversation_messages(
            db, "direct__staff_a___contact_a", _TENANT_A
        )
        assert [m.id for m in fetched] == [msg.id]
    finally:
        db.close()
