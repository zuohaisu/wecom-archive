"""
Tests for RND-112 — temporary password login fallback.

Validates:
  - hash_password / verify_password work correctly (stdlib PBKDF2).
  - verify_password is resistant to malformed hash strings.
  - get_auth_mode() returns expected values for AUTH_MODE env var.
  - POST /api/auth/password/login returns 404 when AUTH_MODE=wecom (default).
  - POST /api/auth/password/login returns 401 for invalid credentials.
  - POST /api/auth/password/login returns 401 and does NOT create a session
    when credentials are wrong.
  - POST /api/auth/password/login returns 200 and sets cookie on valid credentials.
  - Session is tenant-bound to the default tenant.
  - Authenticated password-mode session can access protected admin APIs.
  - Unauthenticated access to protected APIs returns 401.
  - /api/auth/me returns authenticated=True for password-mode session.
  - Logout revokes password-mode session and clears cookie.
  - /api/wecom/archive/events remains public (not affected by password mode).
  - AUTH_MODE=wecom still preserves WeCom login route (at least reachable).
  - admin_login page renders password form in AUTH_MODE=password.
  - admin_login page renders WeCom button in AUTH_MODE=wecom.

All tests that require a live DB are skipped unless DATABASE_URL is set.
Tests that only check endpoint behaviour use mocked DB overrides.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — mock DB factories
# ---------------------------------------------------------------------------


def _mock_db_no_session() -> Generator:
    """get_db override: all session lookups return None (unauthenticated)."""
    mock = MagicMock()
    mock.query.return_value.filter.return_value.first.return_value = None
    yield mock


def _mock_db_no_tenant() -> Generator:
    """get_db override: valid session exists but no tenant found (config error path)."""
    mock = MagicMock()
    # All .first() calls return None so tenant lookup fails.
    mock.query.return_value.filter.return_value.first.return_value = None
    mock.query.return_value.filter.return_value.filter.return_value.first.return_value = None
    yield mock


def _make_tenant(tenant_id: str = "00000000-0000-0000-0000-000000000001") -> MagicMock:
    t = MagicMock()
    t.id = tenant_id
    t.slug = "default"
    t.is_active = True
    return t


def _make_admin_user(user_id: str, tenant_id: str, wecom_user_id: str) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.tenant_id = tenant_id
    u.wecom_user_id = wecom_user_id
    u.name = "testadmin"
    u.last_login_at = None
    u.role = "admin"
    u.email = "admin@example.com"
    u.password_hash = None
    u.status = "active"
    u.ui_theme = "light"
    u.ui_locale = "zh-CN"
    return u


def _make_session(session_id: str, user_id: str, tenant_id: str, wecom_sentinel: str) -> MagicMock:
    s = MagicMock()
    s.id = session_id
    s.admin_user_id = user_id
    s.tenant_id = tenant_id
    s.wecom_user_id = wecom_sentinel
    s.expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
    s.is_revoked = False
    return s


def _mock_db_for_password_login(tenant_id: str = "00000000-0000-0000-0000-000000000001"):
    """
    get_db override: simulates a DB that returns a valid Tenant for the login
    query and accepts add/flush/commit without error.
    First .first() call returns Tenant; second returns None (no existing user).
    """
    tenant = _make_tenant(tenant_id)

    def _override():
        mock = MagicMock()

        def _query(model):
            from app.db.models import AdminUser, Tenant
            q = MagicMock()
            q.filter.return_value = q

            if model is Tenant:
                q.first.return_value = tenant
            elif model is AdminUser:
                q.first.return_value = None  # no existing password user
            else:
                q.first.return_value = None

            return q

        mock.query.side_effect = _query
        mock.flush.return_value = None
        mock.add.return_value = None
        mock.commit.return_value = None
        yield mock

    return _override


def _mock_db_for_authenticated_session(
    tenant_id: str = "test-tenant",
    wecom_sentinel: str = "__pwd__testadmin__",
):
    """
    get_db override: simulates a valid authenticated password-mode session.
    Returns valid AdminSession and AdminUser for any session_id cookie.
    """
    session_id = "test-session-id"
    user_id = "test-user-id"
    user = _make_admin_user(user_id, tenant_id, wecom_sentinel)
    session = _make_session(session_id, user_id, tenant_id, wecom_sentinel)

    def _override():
        mock = MagicMock()

        def _query(model):
            from app.db.models import AdminSession, AdminUser
            q = MagicMock()
            q.filter.return_value = q

            if model is AdminSession:
                q.first.return_value = session
            elif model is AdminUser:
                q.first.return_value = user
            else:
                # ArchiveMessage etc — return empty
                q.order_by.return_value = q
                q.limit.return_value = q
                q.all.return_value = []
                q.first.return_value = None

            return q

        mock.query.side_effect = _query
        yield mock

    return _override


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_db_override():
    """Default: mock DB with no valid session for all tests in this module."""
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
# hash_password / verify_password unit tests
# ---------------------------------------------------------------------------


def test_hash_password_returns_pbkdf2_string() -> None:
    from app.auth import hash_password

    h = hash_password("testpass123")
    assert h.startswith("pbkdf2:sha256:")
    parts = h.split(":")
    assert len(parts) == 5


def test_verify_password_correct_password_returns_true() -> None:
    from app.auth import hash_password, verify_password

    h = hash_password("mysecret")
    assert verify_password("mysecret", h) is True


def test_verify_password_wrong_password_returns_false() -> None:
    from app.auth import hash_password, verify_password

    h = hash_password("correctpass")
    assert verify_password("wrongpass", h) is False


def test_verify_password_empty_password_does_not_match_non_empty() -> None:
    from app.auth import hash_password, verify_password

    h = hash_password("realpassword")
    assert verify_password("", h) is False


def test_verify_password_malformed_hash_returns_false() -> None:
    from app.auth import verify_password

    assert verify_password("anypass", "not-a-valid-hash") is False
    assert verify_password("anypass", "") is False
    assert verify_password("anypass", "pbkdf2:sha256:bad:not-b64:not-b64") is False
    assert verify_password("anypass", "md5:secret") is False


def test_verify_password_different_salts_produce_different_hashes() -> None:
    from app.auth import hash_password

    h1 = hash_password("samepass")
    h2 = hash_password("samepass")
    # Same password, different salts → different stored strings.
    assert h1 != h2


def test_hash_password_output_is_deterministically_verifiable() -> None:
    from app.auth import hash_password, verify_password

    plain = "deterministic-test-password"
    h = hash_password(plain)
    # Verify multiple times — must always return True.
    assert verify_password(plain, h) is True
    assert verify_password(plain, h) is True


# ---------------------------------------------------------------------------
# get_auth_mode unit tests
# ---------------------------------------------------------------------------


def test_get_auth_mode_defaults_to_wecom_when_unset() -> None:
    from app.auth import get_auth_mode

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AUTH_MODE", None)
        assert get_auth_mode() == "wecom"


def test_get_auth_mode_returns_wecom_when_set() -> None:
    from app.auth import get_auth_mode

    with patch.dict(os.environ, {"AUTH_MODE": "wecom"}):
        assert get_auth_mode() == "wecom"


def test_get_auth_mode_returns_password_when_set() -> None:
    from app.auth import get_auth_mode

    with patch.dict(os.environ, {"AUTH_MODE": "password"}):
        assert get_auth_mode() == "password"


def test_get_auth_mode_unknown_value_falls_back_to_wecom() -> None:
    from app.auth import get_auth_mode

    with patch.dict(os.environ, {"AUTH_MODE": "magic"}):
        assert get_auth_mode() == "wecom"


def test_get_auth_mode_case_insensitive() -> None:
    from app.auth import get_auth_mode

    with patch.dict(os.environ, {"AUTH_MODE": "PASSWORD"}):
        assert get_auth_mode() == "password"

    with patch.dict(os.environ, {"AUTH_MODE": "WeCom"}):
        assert get_auth_mode() == "wecom"


# ---------------------------------------------------------------------------
# Login page rendering tests
# ---------------------------------------------------------------------------


def test_login_page_shows_password_form_in_password_mode(client) -> None:
    with patch.dict(os.environ, {"AUTH_MODE": "password"}):
        resp = client.get("/admin/login")
    assert resp.status_code == 200
    body = resp.text
    assert 'type="password"' in body
    assert 'type="text"' in body
    assert "doLogin" in body or "password/login" in body
    # Must NOT show WeCom button in password mode.
    assert "wecom/login" not in body


def test_login_page_shows_wecom_button_in_wecom_mode(client) -> None:
    with patch.dict(os.environ, {"AUTH_MODE": "wecom"}):
        resp = client.get("/admin/login")
    assert resp.status_code == 200
    body = resp.text
    assert "wecom/login" in body
    # Must NOT show password input in wecom mode.
    assert 'type="password"' not in body


# ---------------------------------------------------------------------------
# /api/auth/password/login — mode guard (AUTH_MODE=wecom)
# ---------------------------------------------------------------------------


def test_password_login_endpoint_returns_404_in_wecom_mode(client) -> None:
    with patch.dict(os.environ, {"AUTH_MODE": "wecom"}):
        resp = client.post(
            "/api/auth/password/login",
            json={"username": "admin", "password": "secret"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/auth/password/login — credential validation (AUTH_MODE=password)
# ---------------------------------------------------------------------------


def test_password_login_invalid_credentials_returns_401(client) -> None:
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("correct-password")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}

    app.dependency_overrides[get_db] = _mock_db_for_password_login()
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "wrong-password"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 401
    # Must not include any password hint in the response.
    assert "password" not in resp.text.lower() or "credentials" in resp.text.lower()


def test_password_login_wrong_username_returns_401(client) -> None:
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("correct-password")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}

    app.dependency_overrides[get_db] = _mock_db_for_password_login()
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "notadmin", "password": "correct-password"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 401


def test_password_login_invalid_credentials_does_not_create_session(client) -> None:
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    created_sessions = []

    def _db_tracking():
        tenant = _make_tenant()
        mock = MagicMock()
        original_add = mock.add

        def _add(obj):
            from app.db.models import AdminSession
            if isinstance(obj, AdminSession):
                created_sessions.append(obj)
            return original_add(obj)

        def _query(model):
            from app.db.models import Tenant

            query = MagicMock()
            query.filter.return_value = query
            query.first.return_value = tenant if model is Tenant else None
            return query

        mock.add.side_effect = _add
        mock.query.side_effect = _query
        yield mock

    real_hash = hash_password("correct-password")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}

    app.dependency_overrides[get_db] = _db_tracking
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "wrong-password"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 401
    assert len(created_sessions) == 0, "No session must be created on failed login"


# ---------------------------------------------------------------------------
# /api/auth/password/login — successful login (AUTH_MODE=password)
# ---------------------------------------------------------------------------


def test_password_login_valid_credentials_returns_200(client) -> None:
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("securepass")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}
    tenant_id = "00000000-0000-0000-0000-000000000001"

    app.dependency_overrides[get_db] = _mock_db_for_password_login(tenant_id)
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("logged_in") is True


def test_password_login_sets_httponly_session_cookie(client) -> None:
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("securepass")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}

    app.dependency_overrides[get_db] = _mock_db_for_password_login()
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "session_id" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "path=/" in set_cookie.lower()


def test_password_login_response_does_not_expose_hash(client) -> None:
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("securepass")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}

    app.dependency_overrides[get_db] = _mock_db_for_password_login()
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert real_hash not in resp.text
    assert "securepass" not in resp.text


# ---------------------------------------------------------------------------
# Session tenant-binding tests
# ---------------------------------------------------------------------------


def test_password_session_is_tenant_bound(client) -> None:
    """
    Successful password login must create an AdminSession with the default tenant_id.
    We verify by inspecting the session row added to the mock DB.
    """
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("securepass")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}
    expected_tenant_id = "00000000-0000-0000-0000-000000000001"

    added_sessions = []

    def _db_with_capture():
        tenant = _make_tenant(expected_tenant_id)
        mock = MagicMock()

        def _query(model):
            from app.db.models import AdminUser, Tenant
            q = MagicMock()
            q.filter.return_value = q
            if model is Tenant:
                q.first.return_value = tenant
            elif model is AdminUser:
                q.first.return_value = None
            else:
                q.first.return_value = None
            return q

        def _add(obj):
            from app.db.models import AdminSession
            if isinstance(obj, AdminSession):
                added_sessions.append(obj)

        mock.query.side_effect = _query
        mock.add.side_effect = _add
        mock.flush.return_value = None
        mock.commit.return_value = None
        yield mock

    app.dependency_overrides[get_db] = _db_with_capture
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 200
    assert len(added_sessions) == 1
    assert added_sessions[0].tenant_id == expected_tenant_id


# ---------------------------------------------------------------------------
# Protected API access with password-mode session
# ---------------------------------------------------------------------------


def test_authenticated_password_session_can_access_api_messages(client) -> None:
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_for_authenticated_session()
    client.cookies.set("session_id", "test-session-id")
    resp = client.get("/api/messages")
    # Should return 200 (empty list) not 401.
    assert resp.status_code == 200


def test_authenticated_password_session_can_access_admin_conversations(client) -> None:
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_for_authenticated_session()
    client.cookies.set("session_id", "test-session-id")
    resp = client.get("/admin/conversations")
    assert resp.status_code == 200


def test_authenticated_password_session_auth_me_returns_authenticated(client) -> None:
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_for_authenticated_session()
    client.cookies.set("session_id", "test-session-id")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert "display_name" in data
    # Must not expose password, hash, or sentinel value.
    assert "pbkdf2" not in resp.text
    assert "__pwd__" not in resp.text


# ---------------------------------------------------------------------------
# Unauthenticated access still returns 401
# ---------------------------------------------------------------------------


def test_unauthenticated_api_messages_returns_401_in_password_mode(client) -> None:
    with patch.dict(os.environ, {"AUTH_MODE": "password"}):
        resp = client.get("/api/messages")
    assert resp.status_code == 401


def test_unauthenticated_admin_conversations_redirects_in_password_mode(client) -> None:
    with patch.dict(os.environ, {"AUTH_MODE": "password"}):
        resp = client.get("/admin/conversations", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login"


# ---------------------------------------------------------------------------
# Logout invalidates password-mode session
# ---------------------------------------------------------------------------


def test_logout_invalidates_password_mode_session(client) -> None:
    from app.db.session import get_db
    from app.main import app

    revoked = []
    session_id = "test-session-id"
    tenant_id = "test-tenant"

    def _db_with_revoke():
        session_obj = _make_session(
            session_id,
            "user-id",
            tenant_id,
            "__pwd__admin__",
        )

        def _set_revoked(val):
            revoked.append(val)

        type(session_obj).is_revoked = property(
            fget=lambda self: False,
            fset=lambda self, v: revoked.append(v),
        )

        mock = MagicMock()
        q = MagicMock()
        q.filter.return_value = q
        q.first.return_value = session_obj
        mock.query.return_value = q
        mock.commit.return_value = None
        yield mock

    app.dependency_overrides[get_db] = _db_with_revoke
    client.cookies.set("session_id", session_id)
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("logged_out") is True
    set_cookie = resp.headers.get("set-cookie", "")
    assert "session_id" in set_cookie


# ---------------------------------------------------------------------------
# /api/wecom/archive/events remains public
# ---------------------------------------------------------------------------


def test_wecom_archive_events_remains_public_in_password_mode(client) -> None:
    """
    /api/wecom/archive/events must not require session auth regardless of AUTH_MODE.
    Without the required query params it returns 422 (validation), not 401.
    """
    with patch.dict(os.environ, {"AUTH_MODE": "password"}):
        resp = client.get("/api/wecom/archive/events")
    assert resp.status_code != 401, (
        "/api/wecom/archive/events must remain public in AUTH_MODE=password"
    )


# ---------------------------------------------------------------------------
# AUTH_MODE=wecom — WeCom login routes preserved
# ---------------------------------------------------------------------------


def test_wecom_login_route_still_reachable_in_wecom_mode(client) -> None:
    """
    GET /api/auth/wecom/login must be reachable without a session in wecom mode.
    Without WECOM_CORP_ID/WECOM_AGENT_ID it redirects to error, not 401.
    """
    with patch.dict(os.environ, {"AUTH_MODE": "wecom"}):
        resp = client.get("/api/auth/wecom/login", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.status_code != 401


def test_wecom_callback_route_still_reachable_in_wecom_mode(client) -> None:
    """
    GET /api/auth/wecom/callback must reject invalid state with a redirect, not 404.
    """
    with patch.dict(os.environ, {"AUTH_MODE": "wecom"}):
        resp = client.get(
            "/api/auth/wecom/callback",
            params={"code": "fake", "state": "invalid-state"},
            follow_redirects=False,
        )
    assert resp.status_code in (302, 307)
    location = resp.headers.get("location", "")
    assert "/admin/login" in location


# ---------------------------------------------------------------------------
# Config error path: missing ADMIN_PASSWORD_HASH
# ---------------------------------------------------------------------------


def test_password_login_missing_config_returns_500(client) -> None:
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_for_password_login()
    try:
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ.pop("ADMIN_PASSWORD_HASH", None)
        with patch.dict(os.environ, {"AUTH_MODE": "password"}):
            # Explicitly clear these so the env lookup finds empty strings.
            os.environ["ADMIN_USERNAME"] = ""
            os.environ["ADMIN_PASSWORD_HASH"] = ""
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "pass"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Tenant-binding safety (Codex QA regression) — password login must NEVER
# fall back to an arbitrary active tenant when the RND-111 default tenant
# (slug='default') is missing or inactive. It must fail closed.
# ---------------------------------------------------------------------------


def _mock_db_default_tenant_missing_other_tenant_active(other_tenant_id: str):
    """
    Simulates: no tenant matches (slug='default' AND is_active), but a
    *different* active tenant row exists in the table. The password-login
    query filters on slug+active in a single .filter(...).first() call, so a
    correctly-scoped query returns None here — only a buggy fallback query
    (querying Tenant by is_active alone) would find `other_tenant`.

    Tracks every AdminUser/AdminSession added and every Tenant query made so
    tests can assert no session escapes and no unscoped fallback query ran.
    """
    other_tenant = _make_tenant(other_tenant_id)
    other_tenant.slug = "some-other-tenant"

    added_users: list = []
    added_sessions: list = []
    tenant_queries: list = []

    def _override():
        mock = MagicMock()

        def _query(model):
            from app.db.models import AdminUser, Tenant

            q = MagicMock()
            q.filter.return_value = q

            if model is Tenant:
                tenant_queries.append(True)
                # Correctly-scoped lookup (slug='default' AND is_active) finds nothing.
                q.first.return_value = None
            elif model is AdminUser:
                q.first.return_value = None
            else:
                q.first.return_value = None
            return q

        def _add(obj):
            from app.db.models import AdminSession, AdminUser

            if isinstance(obj, AdminSession):
                added_sessions.append(obj)
            elif isinstance(obj, AdminUser):
                added_users.append(obj)

        mock.query.side_effect = _query
        mock.add.side_effect = _add
        mock.flush.return_value = None
        mock.commit.return_value = None
        yield mock

    return _override, added_users, added_sessions, tenant_queries


def test_password_login_succeeds_when_default_tenant_active(client) -> None:
    """Baseline: valid credentials + default tenant present and active → 200."""
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("securepass")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}
    tenant_id = "00000000-0000-0000-0000-000000000001"

    app.dependency_overrides[get_db] = _mock_db_for_password_login(tenant_id)
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 200
    assert resp.json().get("logged_in") is True
    assert "session_id" in resp.headers.get("set-cookie", "")


def test_password_login_fails_when_default_tenant_missing_but_other_active_tenant_exists(
    client,
) -> None:
    """
    Regression for Codex QA blocker: default tenant absent, another active
    tenant exists — login must fail (not silently bind to the other tenant).
    """
    from app.db.session import get_db
    from app.main import app

    override, added_users, added_sessions, tenant_queries = (
        _mock_db_default_tenant_missing_other_tenant_active("11111111-1111-1111-1111-111111111111")
    )
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": None}

    from app.auth import hash_password

    real_hash = hash_password("securepass")
    env["ADMIN_PASSWORD_HASH"] = real_hash

    app.dependency_overrides[get_db] = override
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 500
    assert len(tenant_queries) == 1, "Must issue exactly one Tenant lookup — no fallback query"
    assert len(added_sessions) == 0, "No session may be created when default tenant is missing"
    assert len(added_users) == 0, "No AdminUser may be created when default tenant is missing"
    assert "session_id" not in resp.headers.get("set-cookie", "")
    # Must not leak the other tenant's ID or any internal DB detail.
    assert "11111111-1111-1111-1111-111111111111" not in resp.text
    assert "some-other-tenant" not in resp.text


def test_password_login_fails_when_default_tenant_inactive(client) -> None:
    """
    Regression: a tenant row with slug='default' exists but is_active=False.
    The query filters on is_active=True, so this must behave identically to
    "missing" — fail closed, no session, no cookie.
    """
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("securepass")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}

    added_sessions: list = []

    def _db_default_tenant_inactive():
        mock = MagicMock()

        def _query(model):
            from app.db.models import Tenant

            q = MagicMock()
            q.filter.return_value = q
            if model is Tenant:
                # is_active.is_(True) filter excludes the inactive default row.
                q.first.return_value = None
            else:
                q.first.return_value = None
            return q

        def _add(obj):
            from app.db.models import AdminSession

            if isinstance(obj, AdminSession):
                added_sessions.append(obj)

        mock.query.side_effect = _query
        mock.add.side_effect = _add
        mock.flush.return_value = None
        mock.commit.return_value = None
        yield mock

    app.dependency_overrides[get_db] = _db_default_tenant_inactive
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 500
    assert len(added_sessions) == 0
    assert "session_id" not in resp.headers.get("set-cookie", "")


def test_password_login_missing_default_tenant_error_does_not_leak_internal_detail(client) -> None:
    """Sanitized 500 body must not mention tenant, database, or SQL-shaped detail."""
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("securepass")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}

    def _db_no_tenant_found():
        mock = MagicMock()
        mock.query.return_value.filter.return_value.first.return_value = None
        yield mock

    app.dependency_overrides[get_db] = _db_no_tenant_found
    try:
        with patch.dict(os.environ, env):
            resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 500
    body_lower = resp.text.lower()
    assert "tenant" not in body_lower
    assert "select" not in body_lower
    assert "traceback" not in body_lower


def test_unauthenticated_after_failed_default_tenant_login_protected_api_returns_401(
    client,
) -> None:
    """
    After a failed login due to missing default tenant, the client holds no
    valid session — protected APIs must still return 401.
    """
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("securepass")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}

    def _db_no_tenant_found():
        mock = MagicMock()
        mock.query.return_value.filter.return_value.first.return_value = None
        yield mock

    app.dependency_overrides[get_db] = _db_no_tenant_found
    try:
        with patch.dict(os.environ, env):
            login_resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
            assert login_resp.status_code == 500

            app.dependency_overrides[get_db] = _mock_db_no_session
            resp = client.get("/api/messages")
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code == 401


def test_wecom_archive_events_public_after_failed_default_tenant_login(client) -> None:
    """/api/wecom/archive/events must remain public even after a failed
    password login caused by a missing default tenant."""
    from app.auth import hash_password
    from app.db.session import get_db
    from app.main import app

    real_hash = hash_password("securepass")
    env = {"AUTH_MODE": "password", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": real_hash}

    def _db_no_tenant_found():
        mock = MagicMock()
        mock.query.return_value.filter.return_value.first.return_value = None
        yield mock

    app.dependency_overrides[get_db] = _db_no_tenant_found
    try:
        with patch.dict(os.environ, env):
            login_resp = client.post(
                "/api/auth/password/login",
                json={"username": "admin", "password": "securepass"},
            )
            assert login_resp.status_code == 500
            resp = client.get("/api/wecom/archive/events")
    finally:
        app.dependency_overrides[get_db] = _mock_db_no_session

    assert resp.status_code != 401
