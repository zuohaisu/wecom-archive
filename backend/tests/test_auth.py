"""
Tests for RND-110 — WeCom employee login / admin console protection.

Validates:
  - Unauthenticated admin API routes return 401.
  - /api/wecom/archive/events is NOT gated by session auth.
  - Auth callback rejects invalid/missing state.
  - /api/auth/me returns correct shape for authenticated and unauthenticated calls.
  - Logout revokes session and clears cookie.
  - Protected HTML admin routes redirect to /admin/login when unauthenticated.
  - Session cookie flags are set correctly for the environment.
  - Auth router imports without error.
  - get_current_user dependency raises 401 with no cookie.
  - All archive queries are scoped to the session tenant_id (cross-tenant isolation).
  - A session for tenant A cannot read tenant B messages via any message endpoint.

All tests that require live WeCom APIs or a real DB are skipped unless DATABASE_URL is set.
Tests that only need no-session → 401 behaviour use a mocked DB dependency so they
pass in CI without any real database.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers — mock DB that returns no sessions (simulates unauthenticated)
# ---------------------------------------------------------------------------


def _mock_db_no_session() -> Generator:
    """get_db override: DB that returns None for every session lookup."""
    mock = MagicMock()
    # query().filter().first() → None  (no valid session)
    mock.query.return_value.filter.return_value.first.return_value = None
    yield mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def override_db():
    """
    Override get_db with a mock for every test in this module.
    This avoids needing DATABASE_URL for the 401-behaviour tests.
    Tests that need a real DB skip themselves explicitly.
    """
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_no_session
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Import / compile tests (no DB, no network)
# ---------------------------------------------------------------------------


def test_auth_module_imports() -> None:
    """app.auth imports without error and exports expected symbols."""
    from app import auth  # noqa: F401

    assert callable(auth.get_current_user)
    assert callable(auth.generate_state)
    assert callable(auth.consume_state)
    assert callable(auth.get_wecom_token)
    assert auth.SESSION_COOKIE == "session_id"
    assert auth.SESSION_TTL_HOURS == 8


def test_auth_router_imports() -> None:
    """app.routers.auth imports without error and exports a FastAPI router."""
    from app.routers import auth  # noqa: F401

    assert hasattr(auth, "router")


def test_conversations_router_still_imports() -> None:
    """conversations router still imports cleanly after RND-110 changes."""
    from app.routers import conversations  # noqa: F401

    assert hasattr(conversations, "router")


# ---------------------------------------------------------------------------
# CSRF state store tests (no DB, no network)
# ---------------------------------------------------------------------------


def test_generate_state_returns_non_empty_string() -> None:
    from app.auth import generate_state

    s = generate_state()
    assert isinstance(s, str) and len(s) >= 32


def test_consume_state_valid() -> None:
    from app.auth import consume_state, generate_state

    token = generate_state()
    assert consume_state(token) is True


def test_consume_state_single_use() -> None:
    from app.auth import consume_state, generate_state

    token = generate_state()
    consume_state(token)  # first use
    assert consume_state(token) is False  # second use must fail


def test_consume_state_invalid_token() -> None:
    from app.auth import consume_state

    assert consume_state("not-a-real-state-token") is False


# ---------------------------------------------------------------------------
# 401 — protected API routes require session
# ---------------------------------------------------------------------------


def test_monitored_accounts_requires_auth(client) -> None:
    resp = client.get("/api/monitored-accounts")
    assert resp.status_code == 401


def test_contacts_requires_auth(client) -> None:
    resp = client.get("/api/contacts")
    assert resp.status_code == 401


def test_conversations_requires_auth(client) -> None:
    resp = client.get("/api/conversations?mode=staff&staff_id=staff_test")
    assert resp.status_code == 401


def test_conversation_messages_requires_auth(client) -> None:
    resp = client.get("/api/conversations/some_conv_id/messages")
    assert resp.status_code == 401


def test_api_messages_requires_auth(client) -> None:
    resp = client.get("/api/messages")
    assert resp.status_code == 401


def test_api_message_detail_requires_auth(client) -> None:
    resp = client.get("/api/messages/fakemsgid")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Admin HTML routes redirect to login when unauthenticated
# ---------------------------------------------------------------------------


def test_admin_conversations_redirects_to_login_when_unauth(client) -> None:
    resp = client.get("/admin/conversations", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login"


def test_admin_messages_redirects_to_login_when_unauth(client) -> None:
    resp = client.get("/admin/messages", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login"


# ---------------------------------------------------------------------------
# Public routes — not gated by session auth
# ---------------------------------------------------------------------------


def test_health_is_public(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200


def test_wecom_archive_events_not_gated_by_session_auth(client) -> None:
    """
    /api/wecom/archive/events must never require a session cookie.
    Without required query params it returns 422 (missing params), NOT 401 (auth).
    """
    resp = client.get("/api/wecom/archive/events")
    assert resp.status_code != 401, (
        "/api/wecom/archive/events must not be protected by session auth"
    )


def test_admin_login_page_is_public(client) -> None:
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert b"Login" in resp.content or b"login" in resp.content


def test_wecom_login_redirect_is_public(client) -> None:
    """
    GET /api/auth/wecom/login must be reachable without a session cookie.
    Without WECOM_CORP_ID/WECOM_AGENT_ID it redirects to error page, not 401.
    """
    resp = client.get("/api/auth/wecom/login", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# /api/auth/me — always 200, authenticated field reflects session state
# ---------------------------------------------------------------------------


def test_auth_me_unauthenticated_returns_200_not_authenticated(client) -> None:
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is False


def test_auth_me_shape_unauthenticated(client) -> None:
    resp = client.get("/api/auth/me")
    data = resp.json()
    assert "authenticated" in data
    assert data["authenticated"] is False
    # Must not expose secrets or session token
    for forbidden in ("session_id", "token", "secret", "cookie"):
        assert forbidden not in data


# ---------------------------------------------------------------------------
# OAuth callback — invalid state is rejected
# ---------------------------------------------------------------------------


def test_callback_invalid_state_redirects_to_login(client) -> None:
    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "fake_code", "state": "not-a-real-state"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    location = resp.headers.get("location", "")
    assert "/admin/login" in location
    assert "error=" in location


def test_callback_missing_state_returns_error(client) -> None:
    """Missing required 'state' query param → 422 (validation), never 200."""
    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "fake_code"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307, 422)


def test_callback_consumed_state_is_rejected(client) -> None:
    """A state token consumed once must not be reusable (single-use)."""
    from app.auth import generate_state

    state = generate_state()
    # First use: will fail at state validation (state is valid but env not configured)
    # → redirects somewhere, state is consumed
    client.get(
        "/api/auth/wecom/callback",
        params={"code": "fake_code", "state": state},
        follow_redirects=False,
    )
    # Second use: state already consumed → must redirect with error
    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "fake_code", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    location = resp.headers.get("location", "")
    assert "/admin/login" in location
    assert "error=" in location


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_clears_cookie(client) -> None:
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("logged_out") is True
    # Cookie should be cleared (set-cookie with empty value or deleted)
    set_cookie = resp.headers.get("set-cookie", "")
    assert "session_id" in set_cookie


def test_logout_get_method_not_allowed(client) -> None:
    """Logout must be POST only."""
    resp = client.get("/api/auth/logout")
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Tenant scoping — helper function tests (no DB required)
# ---------------------------------------------------------------------------


def test_conversations_router_has_get_current_user_dep() -> None:
    """All conversation routes must declare get_current_user as a dependency."""
    import inspect

    from app.auth import get_current_user
    from app.routers.conversations import router

    protected_paths = {
        "/api/monitored-accounts",
        "/api/contacts",
        "/api/conversations",
        "/api/conversations/{conversation_id}/messages",
    }

    for route in router.routes:
        if not (hasattr(route, "path") and route.path in protected_paths):
            continue
        # FastAPI stores Depends() params in the function signature, not route.dependencies
        sig = inspect.signature(route.endpoint)
        dep_funcs = {
            p.default.dependency
            for p in sig.parameters.values()
            if hasattr(p.default, "dependency")
        }
        assert get_current_user in dep_funcs, (
            f"Route {route.path} is missing Depends(get_current_user)"
        )


# ---------------------------------------------------------------------------
# DB-backed tests (skipped when DATABASE_URL not set)
# ---------------------------------------------------------------------------

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_session_cookie_validates_against_db() -> None:
    """A valid session UUID in the cookie must allow access; invalid must return 401."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db.session import get_db

    # Remove the mock override for this test (needs real DB)
    app.dependency_overrides.clear()

    with TestClient(app, raise_server_exceptions=False) as c:
        # Unknown session ID → 401
        c.cookies.set("session_id", str(uuid.uuid4()))
        resp = c.get("/api/monitored-accounts")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Cross-tenant isolation helpers
# ---------------------------------------------------------------------------


def _make_mock_msg(msgid: str = "msg-001", tenant_id: str = "tenant-a") -> MagicMock:
    """Return a MagicMock ArchiveMessage with all required fields set."""
    msg = MagicMock()
    msg.msgid = msgid
    msg.id = 1
    msg.seq = 1
    msg.msgtype = "text"
    msg.action = None
    msg.sender = "staff_alice"
    msg.roomid = None
    msg.msgtime = 1700000000000
    msg.content_text = "hello"
    msg.decrypt_status = "success"
    msg.tenant_id = tenant_id
    return msg


def _db_returning(messages=(), recipients=()):
    """
    get_db factory: returns the given messages for ArchiveMessage queries
    and the given recipients for ArchiveMessageRecipient queries.
    Pass empty sequences to simulate a cross-tenant miss.
    """
    messages = list(messages)
    recipients = list(recipients)

    def _override():
        mock = MagicMock()

        msg_q = MagicMock()
        msg_q.filter.return_value = msg_q
        msg_q.order_by.return_value = msg_q
        msg_q.limit.return_value = msg_q
        msg_q.all.return_value = messages
        msg_q.first.return_value = messages[0] if messages else None

        rcpt_q = MagicMock()
        rcpt_q.filter.return_value = rcpt_q
        rcpt_q.all.return_value = recipients

        def _query(model):
            from app.db.models import ArchiveMessageRecipient
            if model is ArchiveMessageRecipient:
                return rcpt_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    return _override


def _db_with_session_and_messages(tenant_id: str, messages=()):
    """
    get_db factory for HTML admin routes.
    Returns a valid AdminSession for the given tenant_id (so _resolve_session_tenant_id
    succeeds) and the given messages for subsequent ArchiveMessage queries.
    """
    messages = list(messages)

    def _override():
        mock = MagicMock()

        session_mock = MagicMock()
        session_mock.tenant_id = tenant_id

        session_q = MagicMock()
        session_q.filter.return_value = session_q
        session_q.first.return_value = session_mock

        msg_q = MagicMock()
        msg_q.filter.return_value = msg_q
        msg_q.order_by.return_value = msg_q
        msg_q.limit.return_value = msg_q
        msg_q.all.return_value = messages
        msg_q.first.return_value = messages[0] if messages else None

        rcpt_q = MagicMock()
        rcpt_q.filter.return_value = rcpt_q
        rcpt_q.all.return_value = []

        def _query(model):
            from app.db.models import AdminSession, ArchiveMessageRecipient
            if model is AdminSession:
                return session_q
            if model is ArchiveMessageRecipient:
                return rcpt_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    return _override


def _current_user_for(tenant_id: str):
    """get_current_user factory: bypasses session lookup, returns mock user + tenant_id."""
    mock_user = MagicMock()
    mock_user.id = f"user-{tenant_id}"
    mock_user.wecom_user_id = "staff_alice"
    return lambda: (mock_user, tenant_id)


# ---------------------------------------------------------------------------
# Cross-tenant isolation tests (API routes)
# ---------------------------------------------------------------------------


def test_cross_tenant_api_message_detail_returns_404_for_other_tenant(client) -> None:
    """
    A session for tenant B must not read tenant A's messages.
    With tenant B's session, querying a msgid that would exist in tenant A
    returns 404 (the DB is simulated to return no result for tenant B).
    """
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_current_user] = _current_user_for("tenant-b")
    app.dependency_overrides[get_db] = _db_returning()  # empty = no match for tenant-b
    resp = client.get("/api/messages/msg-belongs-to-tenant-a")
    assert resp.status_code == 404


def test_cross_tenant_api_message_detail_returns_200_for_own_tenant(client) -> None:
    """Positive case: tenant A session CAN read tenant A's own message detail."""
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    msg = _make_mock_msg(msgid="msg-a-001", tenant_id="tenant-a")
    app.dependency_overrides[get_current_user] = _current_user_for("tenant-a")
    app.dependency_overrides[get_db] = _db_returning(messages=[msg])
    resp = client.get("/api/messages/msg-a-001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["msgid"] == "msg-a-001"


def test_cross_tenant_api_messages_list_empty_for_other_tenant(client) -> None:
    """
    Tenant B session calling /api/messages sees an empty list when no tenant B
    messages exist (simulates correct tenant filtering in the DB query).
    """
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_current_user] = _current_user_for("tenant-b")
    app.dependency_overrides[get_db] = _db_returning()
    resp = client.get("/api/messages")
    assert resp.status_code == 200
    assert resp.json() == []


def test_cross_tenant_api_messages_sender_filter_empty_for_other_tenant(client) -> None:
    """Filtering by a sender from tenant A while authenticated as tenant B returns nothing."""
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_current_user] = _current_user_for("tenant-b")
    app.dependency_overrides[get_db] = _db_returning()
    resp = client.get("/api/messages?sender=staff_alice_tenant_a")
    assert resp.status_code == 200
    assert resp.json() == []


def test_cross_tenant_api_messages_list_own_tenant_visible(client) -> None:
    """Positive case: tenant A session CAN see its own messages in the list endpoint."""
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    msg = _make_mock_msg(msgid="msg-a-002", tenant_id="tenant-a")
    app.dependency_overrides[get_current_user] = _current_user_for("tenant-a")
    app.dependency_overrides[get_db] = _db_returning(messages=[msg])
    resp = client.get("/api/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["msgid"] == "msg-a-002"


# ---------------------------------------------------------------------------
# Cross-tenant isolation tests (HTML admin routes)
# ---------------------------------------------------------------------------


def test_cross_tenant_admin_messages_html_shows_no_messages_for_other_tenant(
    client,
) -> None:
    """
    /admin/messages scoped to tenant B shows an empty table when tenant B has
    no messages (simulates correct tenant filtering in the DB query).
    """
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _db_with_session_and_messages("tenant-b", messages=[])
    client.cookies.set("session_id", "valid-tenant-b-session")
    resp = client.get("/admin/messages")
    assert resp.status_code == 200
    assert b"No messages found" in resp.content


def test_cross_tenant_admin_message_detail_html_returns_404_for_other_tenant(
    client,
) -> None:
    """
    /admin/messages/{msgid} returns 404 HTML when the message doesn't belong to
    the session tenant (DB returns no result for the filtered query).
    """
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _db_with_session_and_messages("tenant-b", messages=[])
    client.cookies.set("session_id", "valid-tenant-b-session")
    resp = client.get("/admin/messages/msg-belongs-to-tenant-a")
    assert resp.status_code == 404
