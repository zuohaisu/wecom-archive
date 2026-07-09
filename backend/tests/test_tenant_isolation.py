"""
Tests for RND-156 — Multi-tenant / multi-company architecture: core
query-layer isolation.

Validates:
  - Multiple tenants can exist side by side with independent data.
  - Tenant A's admin can see Tenant A's own conversations/messages.
  - Tenant A's admin cannot see Tenant B's conversations/messages, even when
    the conversation_id/roomid string collides across tenants (group and
    direct conversation shapes both covered).
  - ID-based lookups (GET /api/messages/{msgid}, GET
    /api/conversations/{id}/messages) are tenant-protected: a valid ID
    belonging to another tenant returns 404, never that tenant's data.
  - /api/monitored-accounts is tenant-scoped.

Reuses the sqlite-backed schema/fixtures from test_reachability_audit.py so
this exercises the real ORM models and the real tenant-scoped query
functions in app.routers.conversations / app.main, not a parallel
reimplementation.

Run (from backend/):
    pytest tests/test_tenant_isolation.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    _insert_recipient,
    db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _authed(app, db_session, tenant_id):
    """Override get_current_user + get_db to simulate an authenticated
    session for tenant_id against the real sqlite-backed db_session."""
    from app.auth import get_current_user
    from app.db.session import get_db

    def _db_gen():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), tenant_id)
    app.dependency_overrides[get_db] = _db_gen


# ---------------------------------------------------------------------------
# Multiple tenants can coexist, each with independent data
# ---------------------------------------------------------------------------


def test_multiple_tenants_can_exist_with_independent_data(db) -> None:
    from app.routers.conversations import _fetch_conversation_messages

    msg_a = _insert_message(
        db, msgtype="text", sender="staff_a", roomid="room1",
        tenant_id=_TENANT_A, content_text="hello a", msgtime=100,
    )
    _insert_recipient(db, msg_a.id, "contact_a", tenant_id=_TENANT_A)

    msg_b = _insert_message(
        db, msgtype="text", sender="staff_b", roomid="room1",
        tenant_id=_TENANT_B, content_text="hello b", msgtime=100,
    )
    _insert_recipient(db, msg_b.id, "contact_b", tenant_id=_TENANT_B)

    msgs_a = _fetch_conversation_messages(db, "room1", _TENANT_A)
    msgs_b = _fetch_conversation_messages(db, "room1", _TENANT_B)

    assert [m.id for m in msgs_a] == [msg_a.id]
    assert [m.id for m in msgs_b] == [msg_b.id]


def test_direct_conversation_id_collision_across_tenants_isolated(db) -> None:
    """Two tenants whose participant userids happen to collide would derive
    the exact same direct__ conversation_id — isolation must still hold."""
    from app.routers.conversations import _fetch_conversation_messages

    msg_a = _insert_message(
        db, msgtype="text", sender="staff_x", tenant_id=_TENANT_A,
        content_text="a-secret", msgtime=100,
    )
    _insert_recipient(db, msg_a.id, "contact_y", tenant_id=_TENANT_A)

    msg_b = _insert_message(
        db, msgtype="text", sender="staff_x", tenant_id=_TENANT_B,
        content_text="b-secret", msgtime=100,
    )
    _insert_recipient(db, msg_b.id, "contact_y", tenant_id=_TENANT_B)

    conv_id = "direct__contact_y___staff_x"

    msgs_a = _fetch_conversation_messages(db, conv_id, _TENANT_A)
    msgs_b = _fetch_conversation_messages(db, conv_id, _TENANT_B)

    assert [m.content_text for m in msgs_a] == ["a-secret"]
    assert [m.content_text for m in msgs_b] == ["b-secret"]


# ---------------------------------------------------------------------------
# Conversation-messages API — tenant isolation end to end
# ---------------------------------------------------------------------------


def test_tenant_a_admin_sees_own_conversation_messages(client, db) -> None:
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_a", roomid="roomX",
        tenant_id=_TENANT_A, content_text="hi", msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomX/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content_text"] == "hi"


def test_tenant_a_admin_cannot_see_tenant_b_conversation_messages(client, db) -> None:
    """Same roomid string used by both tenants — isolation must hold even
    though the conversation_id collides across tenants."""
    from app.main import app

    msg_b = _insert_message(
        db, msgtype="text", sender="staff_b", roomid="roomX",
        tenant_id=_TENANT_B, content_text="tenant b secret", msgtime=100,
    )
    _insert_recipient(db, msg_b.id, "contact_b", tenant_id=_TENANT_B)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/roomX/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert "tenant b secret" not in resp.text


# ---------------------------------------------------------------------------
# ID-based lookups — GET /api/messages/{msgid}
# ---------------------------------------------------------------------------


def test_id_based_message_lookup_succeeds_for_own_tenant(client, db) -> None:
    from app.main import app

    msg_a = _insert_message(
        db, msgid="msg-a-own", msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="tenant a payload", msgtime=100,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/messages/{msg_a.msgid}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["content_text"] == "tenant a payload"


def test_id_based_message_lookup_is_tenant_protected(client, db) -> None:
    """A syntactically valid msgid belonging to another tenant must 404, not
    leak that tenant's message content."""
    from app.main import app

    msg_b = _insert_message(
        db, msgid="msg-b-secret", msgtype="text", sender="staff_b",
        tenant_id=_TENANT_B, content_text="tenant b payload", msgtime=100,
    )

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/messages/{msg_b.msgid}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert "tenant b payload" not in resp.text


# ---------------------------------------------------------------------------
# /api/monitored-accounts — tenant-scoped seat detection
# ---------------------------------------------------------------------------


def test_monitored_accounts_scoped_to_tenant(client, db) -> None:
    from app.main import app

    msg_a = _insert_message(
        db, msgtype="text", sender="staff_only_a", tenant_id=_TENANT_A,
        content_text="a", msgtime=100,
    )
    _insert_recipient(db, msg_a.id, "contact_a", tenant_id=_TENANT_A)

    msg_b = _insert_message(
        db, msgtype="text", sender="staff_only_b", tenant_id=_TENANT_B,
        content_text="b", msgtime=100,
    )
    _insert_recipient(db, msg_b.id, "contact_b", tenant_id=_TENANT_B)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/monitored-accounts")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    ids = {a["staff_id"] for a in resp.json()}
    assert ids == {"staff_only_a"}
    assert "staff_only_b" not in ids


# ---------------------------------------------------------------------------
# Negative tests — malformed/mistagged cross-tenant recipient rows
#
# message_id (archive_message_recipients.message_id) is a global primary
# key referencing archive_messages.id, not a tenant-scoped value. A
# recipient row's own tenant_id is therefore the only thing that can ever
# authorize exposing it — parent-message tenant ownership alone is not
# sufficient. These tests construct the data-integrity anomaly directly
# (a recipient row whose tenant_id disagrees with either its parent
# message's tenant or the requesting admin's tenant) and prove it can never
# leak through _load_recipients_map or _fetch_conversation_messages'
# direct-conversation join.
# ---------------------------------------------------------------------------


def test_stray_cross_tenant_recipient_row_not_exposed_in_recipients_map(db) -> None:
    from app.routers.conversations import _load_recipients_map

    msg_a = _insert_message(
        db, msgtype="text", sender="staff_a", tenant_id=_TENANT_A,
        content_text="hi", msgtime=100,
    )
    # Malformed row: shares msg_a.id but is tagged tenant B.
    _insert_recipient(db, msg_a.id, "contact_leaked", tenant_id=_TENANT_B)

    result = _load_recipients_map(db, _TENANT_A, [msg_a.id])

    assert result.get(msg_a.id, []) == []
    all_recipients = [r for recips in result.values() for r in recips]
    assert "contact_leaked" not in all_recipients


def test_stray_cross_tenant_recipient_does_not_create_false_direct_membership(db) -> None:
    """A Tenant A message with no legitimate Tenant A recipient row, but a
    stray Tenant B recipient row sharing its message_id and matching
    receiver_userid, must not be picked up as direct-conversation
    membership when Tenant A queries that conversation_id."""
    from app.routers.conversations import _fetch_conversation_messages

    msg_a = _insert_message(
        db, msgtype="text", sender="staff_x", tenant_id=_TENANT_A,
        content_text="a-only", msgtime=100,
    )
    # No legitimate tenant-A recipient row for contact_y — only a
    # mistagged tenant-B row pointing at this tenant-A message.
    _insert_recipient(db, msg_a.id, "contact_y", tenant_id=_TENANT_B)

    conv_id = "direct__contact_y___staff_x"
    msgs = _fetch_conversation_messages(db, conv_id, _TENANT_A)

    assert msgs == []


def test_id_based_message_recipients_excludes_stray_cross_tenant_row(client, db) -> None:
    """GET /api/messages/{msgid} must never surface a recipient row whose
    tenant_id disagrees with the authenticated tenant, even for a message
    the admin is otherwise authorized to view."""
    from app.main import app

    msg_a = _insert_message(
        db, msgid="msg-a-recip", msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hi", msgtime=100,
    )
    _insert_recipient(db, msg_a.id, "contact_leaked", tenant_id=_TENANT_B)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/messages/{msg_a.msgid}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["recipients"] == []
    assert "contact_leaked" not in resp.text
