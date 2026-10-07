"""
Tests for RND-225 — OAuth fail-open fix.

Root cause: in wecom_callback (backend/app/routers/auth.py), the
active/enabled-employee verification step treated "the WeCom user/get API
returned a non-zero errcode" the same as "verification not needed" and let
the login proceed. Only a raised exception happened to fail closed (by
coincidence, since {}.get("errcode", 0) == 0). A departed/disabled WeCom
employee could be granted a valid admin session if that one API call
returned any error response instead of throwing.

This file also covers the follow-up findings from the independent RND-225
security review (blockers B1-B4, majors M1-M3):

  B1 - HTTP status of the three WeCom API calls (gettoken, getuserinfo,
       user/get) was never checked; a non-2xx response whose body happened
       to parse as success-shaped JSON could still create a session. All
       three now check status_code == 200 before trusting the JSON body.
  B2 - The active-employee check required BOTH status==1 AND enable==1, but
       the real https://qyapi.weixin.qq.com/cgi-bin/user/get response has
       no "enable" field (confirmed against the official docs) — every
       real active employee (status=1, enable absent) was being rejected.
       Fixed to gate on status alone: 1=active, 2=disabled, 4=not-activated,
       5=left the enterprise.
  B3 - errcode/status could be satisfied by a bool (True == 1 in Python);
       UserId/userid could be a non-string; and user/get's returned userid
       was never cross-checked against the OAuth-resolved identity. Now:
       strict int checks (strict_int_equals), strict str checks, and an
       explicit case-insensitive userid match between getuserinfo and
       user/get.
  B4 - Exception logging in get_current_user, _resolve_session_tenant_id,
       and the callback's session-creation step used exc_info=True /
       logged str(exc), which can leak bound SQL parameters (session
       tokens, wecom_user_id) via DBAPI error reprs. Now logs only
       type(exc).__name__.
  M1 - A stale-but-still-valid session cookie caused /admin/login to bounce
       straight to the console even when an ?error= query param was
       present, masking a failed OAuth callback as indistinguishable from
       success. /admin/login now shows the error whenever one is present.

This file verifies:
  - Positive: a fully successful, active WeCom user (matching the real API
    response shape) completes login and is issued a session cookie.
  - Negative: every failure mode of the active-employee check (exception,
    non-zero errcode, non-2xx HTTP status, non-dict body, wrong-typed or
    mismatched identity fields, every non-active `status` code) rejects the
    login — no cookie is ever set and the user is never redirected to the
    console.
  - Negative: upstream failures earlier in the callback (token fetch,
    code exchange, missing/wrong-typed UserId) already failed closed;
    regression-guard them too since the fix touches the same function.
  - get_current_user (backend/app/auth.py) fails closed with 401 on missing,
    invalid, expired, and revoked sessions, on an orphaned session (user
    row missing), and on a DB failure during lookup — never falls back to
    an anonymous/default/cached identity.
  - _resolve_session_tenant_id (backend/app/main.py, used by HTML admin
    routes) fails closed (returns None -> redirect to /admin/login) on a
    DB failure during session lookup.
  - A DB failure while creating the admin_users/admin_sessions rows during
    the OAuth callback fails closed (redirect, no cookie) instead of
    leaking an uncaught 500.
  - Auth-path failure logs never contain the session token or other bound
    query parameters, only the exception's type name.
  - A stale-but-valid session cookie never masks a failed-login error.

Run (from backend/):
    pytest tests/test_rnd225_auth_fail_closed.py -v
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AdminAccessRequest,
    AdminLoginIdentity,
    AdminSession,
    AdminUser,
    Tenant,
    TenantWecomConfig,
)


# ---------------------------------------------------------------------------
# Fixtures — app/TestClient, isolated from other test modules' DB overrides
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _mock_db_no_session() -> Generator:
    mock = MagicMock()
    mock.query.return_value.filter.return_value.first.return_value = None
    yield mock


@pytest.fixture(autouse=True)
def override_db():
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_no_session
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Real sqlite-backed DB for callback / get_current_user tests that need
# actual row filtering (expiry, revocation, orphaned FKs) rather than a
# MagicMock that ignores filter arguments.
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantWecomConfig.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            AdminLoginIdentity.__table__,
            AdminAccessRequest.__table__,
        ],
    )
    session = Session(engine)
    yield session
    session.close()


def _make_tenant(db: Session, tenant_id: str = "tenant-1", slug: str = "default") -> Tenant:
    tenant = Tenant(id=tenant_id, name=slug, slug=slug)
    db.add(tenant)
    db.commit()
    return tenant


def _make_wecom_config(
    db: Session, tenant_id: str, corp_id: str = "corp-1"
) -> TenantWecomConfig:
    config = TenantWecomConfig(
        id=f"cfg-{tenant_id}",
        tenant_id=tenant_id,
        corp_id=corp_id,
        agent_id="1000001",
        app_secret="fake-secret",
        is_active=True,
    )
    db.add(config)
    db.commit()
    return config


def _make_admin_user(db: Session, tenant_id: str, wecom_user_id: str = "zhangsan") -> AdminUser:
    user = AdminUser(
        id=f"user-{wecom_user_id}",
        tenant_id=tenant_id,
        wecom_user_id=wecom_user_id,
        name="Zhang San",
    )
    db.add(user)
    db.commit()
    return user


def _make_session(
    db: Session,
    user: AdminUser,
    tenant_id: str,
    session_id: str = "sess-1",
    expires_delta: timedelta = timedelta(hours=1),
    is_revoked: bool = False,
) -> AdminSession:
    session = AdminSession(
        id=session_id,
        admin_user_id=user.id,
        tenant_id=tenant_id,
        wecom_user_id=user.wecom_user_id,
        expires_at=datetime.now(timezone.utc) + expires_delta,
        is_revoked=is_revoked,
    )
    db.add(session)
    db.commit()
    return session


# ---------------------------------------------------------------------------
# get_current_user — direct unit tests against a real sqlite DB
# ---------------------------------------------------------------------------


def test_get_current_user_valid_session_returns_user(db: Session) -> None:
    """Positive case: a valid, non-expired, non-revoked session resolves the user."""
    from app.auth import get_current_user

    tenant = _make_tenant(db)
    user = _make_admin_user(db, tenant.id)
    _make_session(db, user, tenant.id, session_id="sess-valid")

    resolved_user, tenant_id = get_current_user(session_id="sess-valid", db=db)
    assert resolved_user.id == user.id
    assert tenant_id == tenant.id


def test_get_current_user_missing_cookie_raises_401() -> None:
    from app.auth import get_current_user

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(session_id=None, db=MagicMock())
    assert exc_info.value.status_code == 401


def test_get_current_user_unknown_session_raises_401(db: Session) -> None:
    from app.auth import get_current_user

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(session_id="does-not-exist", db=db)
    assert exc_info.value.status_code == 401


def test_get_current_user_expired_session_raises_401(db: Session) -> None:
    from app.auth import get_current_user

    tenant = _make_tenant(db)
    user = _make_admin_user(db, tenant.id)
    _make_session(
        db, user, tenant.id, session_id="sess-expired", expires_delta=timedelta(hours=-1)
    )

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(session_id="sess-expired", db=db)
    assert exc_info.value.status_code == 401


def test_get_current_user_revoked_session_raises_401(db: Session) -> None:
    from app.auth import get_current_user

    tenant = _make_tenant(db)
    user = _make_admin_user(db, tenant.id)
    _make_session(db, user, tenant.id, session_id="sess-revoked", is_revoked=True)

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(session_id="sess-revoked", db=db)
    assert exc_info.value.status_code == 401


def test_get_current_user_orphaned_session_raises_401(db: Session) -> None:
    """Session row exists but its admin_user_id no longer resolves to a user."""
    from app.auth import get_current_user

    tenant = _make_tenant(db)
    user = _make_admin_user(db, tenant.id)
    _make_session(db, user, tenant.id, session_id="sess-orphan")
    db.query(AdminUser).filter(AdminUser.id == user.id).delete()
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(session_id="sess-orphan", db=db)
    assert exc_info.value.status_code == 401


def test_get_current_user_db_failure_on_session_lookup_raises_401() -> None:
    """DB failure during session lookup must deny access, never fall back to a
    cached/anonymous/default identity."""
    from app.auth import get_current_user

    broken_db = MagicMock()
    broken_db.query.side_effect = RuntimeError("connection lost")

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(session_id="whatever", db=broken_db)
    assert exc_info.value.status_code == 401


def test_get_current_user_db_failure_on_user_lookup_raises_401(db: Session) -> None:
    """Session lookup succeeds but the subsequent user lookup throws."""
    from app.auth import get_current_user

    tenant = _make_tenant(db)
    user = _make_admin_user(db, tenant.id)
    _make_session(db, user, tenant.id, session_id="sess-user-lookup-fails")

    real_query = db.query
    calls = {"n": 0}

    def _query(model):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("db down")
        return real_query(model)

    db.query = _query  # type: ignore[assignment]

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(session_id="sess-user-lookup-fails", db=db)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# _resolve_session_tenant_id — HTML-route equivalent of get_current_user
# ---------------------------------------------------------------------------


def test_resolve_session_tenant_id_db_failure_returns_none() -> None:
    from app.auth import require_html_session

    request = MagicMock()
    request.cookies = {"session_id": "whatever"}
    broken_db = MagicMock()
    broken_db.query.side_effect = RuntimeError("connection lost")

    assert require_html_session(request, broken_db) is None


def test_admin_conversations_redirects_to_login_when_db_lookup_fails(client) -> None:
    """End-to-end: a DB failure while resolving the session must redirect to
    login, never render the protected admin page."""
    from app.db.session import get_db
    from app.main import app

    def _broken_db():
        mock = MagicMock()
        mock.query.side_effect = RuntimeError("connection lost")
        yield mock

    app.dependency_overrides[get_db] = _broken_db
    client.cookies.set("session_id", "some-session")
    resp = client.get("/admin/conversations", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login"


# ---------------------------------------------------------------------------
# OAuth callback — httpx mocking helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """
    Routes .get() calls by URL substring so getuserinfo vs user/get can
    return independently-controlled payloads within a single test.

    A response entry may be:
      - a dict            -> 200 OK with that JSON body
      - (status, payload) -> that HTTP status with that JSON body (payload
                              may be any JSON-serializable value, including
                              a non-dict, to simulate a malformed response)
      - an Exception       -> raised from .get() (network/timeout failure)
    """

    def __init__(self, responses: dict) -> None:
        self._responses = responses

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def get(self, url, params=None):
        for key, outcome in self._responses.items():
            if key in url:
                if isinstance(outcome, Exception):
                    raise outcome
                if isinstance(outcome, tuple):
                    status_code, payload = outcome
                    return _FakeResponse(payload, status_code=status_code)
                return _FakeResponse(outcome)
        raise AssertionError(f"unexpected URL in test: {url}")


def _patch_wecom_http(monkeypatch, *, getuserinfo=None, user_get=None) -> None:
    """
    Patch app.routers.auth.httpx.Client to answer getuserinfo/user/get calls,
    and short-circuit get_wecom_token so tests don't need to mock gettoken too.
    """
    from app.routers import auth as auth_router_mod

    monkeypatch.setattr(auth_router_mod, "get_wecom_token", lambda corp_id, secret: "fake-token")

    responses = {}
    if getuserinfo is not None:
        responses["getuserinfo"] = getuserinfo
    if user_get is not None:
        responses["user/get"] = user_get

    monkeypatch.setattr(
        auth_router_mod.httpx, "Client", lambda timeout=None: _FakeClient(responses)
    )


def _prepare_callback_env(monkeypatch, db_override) -> str:
    """Set required env vars, override get_db, and return a valid state token."""
    from app.auth import generate_state
    from app.db.session import get_db
    from app.main import app

    monkeypatch.setenv("WECOM_CORP_ID", "corp-1")
    monkeypatch.setenv("WECOM_OAUTH_SECRET", "fake-secret")
    app.dependency_overrides[get_db] = db_override
    return generate_state()


def _real_db_override_with_engine(tenant_id: str):
    """Like _real_db_override, but also returns the underlying engine so a
    test can independently query AdminSession afterward — asserting "no
    cookie was sent" is not the same evidence as "no session row exists"."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantWecomConfig.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            AdminLoginIdentity.__table__,
            AdminAccessRequest.__table__,
        ],
    )
    seed = Session(engine)
    _make_tenant(seed, tenant_id=tenant_id)
    _make_wecom_config(seed, tenant_id=tenant_id, corp_id="corp-1")
    seed.close()

    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    return _override, engine


def _real_db_override(tenant_id: str):
    override, _engine = _real_db_override_with_engine(tenant_id)
    return override


def _session_count(engine) -> int:
    with Session(engine) as s:
        return s.query(AdminSession).count()


# ---------------------------------------------------------------------------
# OAuth callback — positive case
# ---------------------------------------------------------------------------


def test_callback_active_enabled_user_logs_in_successfully(client, monkeypatch) -> None:
    """
    Positive case: a fully valid, active WeCom user completes login and
    receives a session cookie. The user/get payload here matches the actual
    documented response shape of https://qyapi.weixin.qq.com/cgi-bin/user/get
    (errcode, userid, status, name, ...) — there is no "enable" field in the
    real API (see B2 in the RND-225 review); a fixture that invented one
    would mask a check that rejects every real active employee in prod.
    """
    override, engine = _real_db_override_with_engine("tenant-ok")
    with Session(engine) as db:
        db.add(
            AdminUser(
                id="pre-authorized-zhangsan",
                tenant_id="tenant-ok",
                wecom_user_id="zhangsan",
                role="compliance",
                status="active",
            )
        )
        # RND-321: identity resolution is via AdminLoginIdentity, not a
        # scan of AdminUser.wecom_user_id — without this the scan would be
        # treated as unbound and produce an access request instead of a
        # session, which is exactly what this positive-case test verifies
        # does *not* happen for an already-authorized account.
        db.add(
            AdminLoginIdentity(
                id="login-identity-zhangsan",
                tenant_id="tenant-ok",
                provider="wecom",
                subject="zhangsan",
                admin_user_id="pre-authorized-zhangsan",
            )
        )
        db.commit()
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get={"errcode": 0, "userid": "zhangsan", "status": 1, "name": "Zhang San"},
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "good-code", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"
    assert "session_id" in resp.headers.get("set-cookie", "")
    assert _session_count(engine) == 1


def test_callback_userid_case_insensitive_match_still_logs_in(client, monkeypatch) -> None:
    """WeCom userids are case-insensitive; a provider echoing a different
    case for the same identity must not be treated as a mismatch."""
    override, engine = _real_db_override_with_engine("tenant-case")
    with Session(engine) as db:
        db.add(
            AdminUser(
                id="pre-authorized-zhangsan-case",
                tenant_id="tenant-case",
                wecom_user_id="ZhangSan",
                role="compliance",
                status="active",
            )
        )
        # Bound with the exact case getuserinfo's UserId returns below
        # ("ZhangSan") — the callback's identity lookup is a plain equality
        # match on that value; it's the *separate* getuserinfo-vs-user/get
        # cross-check that's case-insensitive (via .lower()), not this one.
        db.add(
            AdminLoginIdentity(
                id="login-identity-zhangsan-case",
                tenant_id="tenant-case",
                provider="wecom",
                subject="ZhangSan",
                admin_user_id="pre-authorized-zhangsan-case",
            )
        )
        db.commit()
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "ZhangSan"},
        user_get={"errcode": 0, "userid": "zhangsan", "status": 1, "name": "Zhang San"},
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "good-code", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"
    assert _session_count(engine) == 1


# ---------------------------------------------------------------------------
# OAuth callback — negative cases (the fail-open bug and its neighbors)
# ---------------------------------------------------------------------------


def _assert_login_rejected(resp) -> None:
    assert resp.status_code in (302, 307)
    location = resp.headers.get("location", "")
    assert location != "/admin/conversations"
    assert "/admin/login" in location
    assert "error=" in location
    assert "session_id" not in resp.headers.get("set-cookie", "")


def test_callback_user_get_errcode_nonzero_rejects_login(client, monkeypatch) -> None:
    """
    THE FAIL-OPEN BUG (RND-225): previously, a non-zero errcode from user/get
    (e.g. permission-scope issue, rate limiting) skipped the active/enabled
    check entirely and let a departed/disabled employee log in. Must now be
    rejected exactly like an exception would be.
    """
    override, engine = _real_db_override_with_engine("tenant-bug")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "departed_employee"},
        user_get={"errcode": 60111, "errmsg": "userid not found"},
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_user_get_raises_exception_rejects_login(client, monkeypatch) -> None:
    override, engine = _real_db_override_with_engine("tenant-exc")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get=RuntimeError("network timeout"),
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


@pytest.mark.parametrize(
    "status_code, label",
    [(2, "disabled"), (4, "not_activated"), (5, "left_enterprise")],
)
def test_callback_user_non_active_status_rejects_login(
    client, monkeypatch, status_code, label
) -> None:
    """
    Every official non-active `status` value (2=disabled, 4=not-activated,
    5=left the enterprise — see https://developer.work.weixin.qq.com/document/path/90196)
    must reject the login with error=user_inactive, not just status=0 (which
    the real API never actually returns).
    """
    override, engine = _real_db_override_with_engine(f"tenant-{label}")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": label},
        user_get={"errcode": 0, "userid": label, "status": status_code},
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert "error=user_inactive" in resp.headers["location"]
    assert _session_count(engine) == 0


def test_callback_status_as_bool_true_rejects_login(client, monkeypatch) -> None:
    """
    Type-confusion guard: Python's `True == 1`, so a naive `status == 1`
    comparison would treat a boolean `True` the same as the real active
    status. status must be a strict int, not merely truthy/equal-by-coercion.
    """
    override, engine = _real_db_override_with_engine("tenant-bool-status")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get={"errcode": 0, "userid": "zhangsan", "status": True},
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_userid_missing_from_user_get_rejects_login(client, monkeypatch) -> None:
    """user/get response omits `userid` entirely -> can't confirm identity."""
    override, engine = _real_db_override_with_engine("tenant-nouserid")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get={"errcode": 0, "status": 1, "name": "Zhang San"},
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_userid_mismatch_rejects_login(client, monkeypatch) -> None:
    """
    user/get returns status/employment data for a *different* userid than
    the one that was authenticated via OAuth. Must never bind the session
    to the OAuth identity using someone else's employment verification.
    """
    override, engine = _real_db_override_with_engine("tenant-mismatch")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get={"errcode": 0, "userid": "someone_else", "status": 1},
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_getuserinfo_integer_userid_rejects_login(client, monkeypatch) -> None:
    """
    Type-confusion guard: a non-string UserId (e.g. an integer) must be
    rejected outright rather than silently coerced/accepted as an identity.
    """
    override, engine = _real_db_override_with_engine("tenant-int-uid")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(monkeypatch, getuserinfo={"errcode": 0, "UserId": 123456})

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_user_get_http_500_with_success_shaped_body_rejects_login(
    client, monkeypatch
) -> None:
    """
    B1: an HTTP-layer failure (e.g. a gateway/proxy returning 500 with an
    attacker- or misconfig-controlled body that happens to parse as a
    success-shaped JSON payload) must be rejected on the HTTP status alone,
    never on JSON content parsed from a non-2xx response.
    """
    override, engine = _real_db_override_with_engine("tenant-http500")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get=(500, {"errcode": 0, "userid": "zhangsan", "status": 1}),
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_getuserinfo_http_302_with_success_shaped_body_rejects_login(
    client, monkeypatch
) -> None:
    override, engine = _real_db_override_with_engine("tenant-http302")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(monkeypatch, getuserinfo=(302, {"errcode": 0, "UserId": "zhangsan"}))

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_user_get_non_dict_json_rejects_login_gracefully(client, monkeypatch) -> None:
    """A malformed (non-object) JSON body must be rejected via the normal
    redirect-with-error path, not surfaced as an uncaught 500."""
    override, engine = _real_db_override_with_engine("tenant-nondict")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get=["not", "an", "object"],
    )

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_getuserinfo_errcode_nonzero_rejects_login(client, monkeypatch) -> None:
    """Regression guard: code-exchange failure (invalid/expired code) already
    failed closed before this fix; keep it that way."""
    override, engine = _real_db_override_with_engine("tenant-badcode")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(monkeypatch, getuserinfo={"errcode": 40029, "errmsg": "invalid code"})

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "bad-code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_getuserinfo_missing_userid_rejects_login(client, monkeypatch) -> None:
    override, engine = _real_db_override_with_engine("tenant-nouid")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(monkeypatch, getuserinfo={"errcode": 0})

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_getuserinfo_raises_exception_rejects_login(client, monkeypatch) -> None:
    override, engine = _real_db_override_with_engine("tenant-getuserinfo-exc")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(monkeypatch, getuserinfo=RuntimeError("network down"))

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_token_fetch_failure_rejects_login(client, monkeypatch) -> None:
    """get_wecom_token raising (provider error / network failure) must reject
    login rather than proceed with a stale/empty token."""
    from app.routers import auth as auth_router_mod

    override, engine = _real_db_override_with_engine("tenant-token-fail")
    state = _prepare_callback_env(monkeypatch, override)

    def _raise(*a, **k):
        raise RuntimeError("WeCom gettoken failed")

    monkeypatch.setattr(auth_router_mod, "get_wecom_token", _raise)

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_missing_token_env_config_rejects_login(client, monkeypatch) -> None:
    """WECOM_OAUTH_SECRET missing entirely -> config error, never proceeds."""
    from app.auth import generate_state
    from app.db.session import get_db
    from app.main import app

    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.delenv("WECOM_OAUTH_SECRET", raising=False)
    override, engine = _real_db_override_with_engine("tenant-noenv")
    app.dependency_overrides[get_db] = override
    state = generate_state()

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_session_creation_db_failure_rejects_login(client, monkeypatch) -> None:
    """DB failure while upserting admin_users / creating admin_sessions must
    fail closed (redirect, no cookie) instead of an uncaught 500 or a
    partially-issued session."""
    from app.db.session import get_db
    from app.main import app

    override, engine = _real_db_override_with_engine("tenant-dbfail")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get={"errcode": 0, "userid": "zhangsan", "status": 1, "name": "Zhang San"},
    )

    real_override = app.dependency_overrides[get_db]

    def _broken_after_tenant_lookup():
        gen = real_override()
        session = next(gen)

        real_query = session.query
        calls = {"n": 0}

        def _query(model):
            calls["n"] += 1
            # Let the tenant-config lookup (call #1) succeed; break the
            # admin_users upsert (call #2) that follows it.
            if calls["n"] >= 2:
                raise RuntimeError("db connection lost")
            return real_query(model)

        session.query = _query  # type: ignore[assignment]
        yield session

    app.dependency_overrides[get_db] = _broken_after_tenant_lookup

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


def test_callback_cookie_construction_failure_leaves_no_orphan_session(
    client, monkeypatch
) -> None:
    """
    B2 (review round 2): the DB write (admin_users upsert + session insert)
    must not be committed until the response — cookie included — has been
    fully built. Simulate a failure in RedirectResponse.set_cookie() itself
    (the last thing that happens before db.commit()) and confirm no
    AdminSession row is left in the database with no cookie ever having
    been sent to any client.
    """
    from app.routers import auth as auth_router_mod

    override, engine = _real_db_override_with_engine("tenant-cookie-fail")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get={"errcode": 0, "userid": "zhangsan", "status": 1, "name": "Zhang San"},
    )

    def _broken_set_cookie(self, *a, **k):
        raise RuntimeError("cookie construction failed")

    monkeypatch.setattr(auth_router_mod.RedirectResponse, "set_cookie", _broken_set_cookie)

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


# ---------------------------------------------------------------------------
# get_wecom_token — direct hardening tests (B1/B2/B3 applied to the shared
# token-fetch helper, not just the two callback-specific HTTP calls)
# ---------------------------------------------------------------------------


def _patch_gettoken_http(monkeypatch, outcome) -> None:
    from app import auth as auth_mod

    monkeypatch.setattr(
        auth_mod.httpx, "Client", lambda timeout=None: _FakeClient({"gettoken": outcome})
    )


def test_get_wecom_token_success_returns_token(monkeypatch) -> None:
    from app.auth import get_wecom_token

    _patch_gettoken_http(monkeypatch, {"errcode": 0, "access_token": "tok-abc", "expires_in": 7200})
    assert get_wecom_token("corp-gettoken-ok", "secret") == "tok-abc"


def test_get_wecom_token_http_non_200_with_success_shaped_body_raises(monkeypatch) -> None:
    """B1: an HTTP-layer failure must not be trusted just because the body
    happens to parse as a success-shaped JSON payload."""
    from app.auth import get_wecom_token

    _patch_gettoken_http(
        monkeypatch, (500, {"errcode": 0, "access_token": "tok", "expires_in": 7200})
    )
    with pytest.raises(RuntimeError):
        get_wecom_token("corp-gettoken-500", "secret")


def test_get_wecom_token_non_dict_json_raises(monkeypatch) -> None:
    from app.auth import get_wecom_token

    _patch_gettoken_http(monkeypatch, ["not", "an", "object"])
    with pytest.raises(RuntimeError):
        get_wecom_token("corp-gettoken-nondict", "secret")


def test_get_wecom_token_boolean_errcode_raises(monkeypatch) -> None:
    """Type-confusion guard: errcode=False must not satisfy the errcode==0
    gate via Python's bool/int coercion (False == 0 is True)."""
    from app.auth import get_wecom_token

    _patch_gettoken_http(
        monkeypatch, {"errcode": False, "access_token": "tok", "expires_in": 7200}
    )
    with pytest.raises(RuntimeError):
        get_wecom_token("corp-gettoken-boolerrcode", "secret")


def test_get_wecom_token_missing_access_token_field_raises(monkeypatch) -> None:
    from app.auth import get_wecom_token

    _patch_gettoken_http(monkeypatch, {"errcode": 0, "expires_in": 7200})
    with pytest.raises(RuntimeError):
        get_wecom_token("corp-gettoken-noaccesstoken", "secret")


# ---------------------------------------------------------------------------
# B3 — uvicorn access log must not record the OAuth callback's code/state
# ---------------------------------------------------------------------------


def _make_access_log_record(path_with_query: str) -> "logging.LogRecord":
    """Build a LogRecord matching the exact call shape uvicorn's h11
    protocol implementation uses for its access logger (see
    uvicorn/protocols/http/h11_impl.py: access_logger.info('%s - "%s %s
    HTTP/%s" %d', client_addr, method, path_with_query, http_version,
    status))."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:12345", "GET", path_with_query, "1.1", 302),
        exc_info=None,
    )


def test_access_log_filter_redacts_oauth_callback_query() -> None:
    from app.main import _RedactOAuthCallbackQueryFilter

    record = _make_access_log_record(
        "/api/auth/wecom/callback?code=REAL_OAUTH_CODE&state=REAL_CSRF_STATE"
    )
    _RedactOAuthCallbackQueryFilter().filter(record)

    formatted = record.getMessage()
    assert "REAL_OAUTH_CODE" not in formatted
    assert "REAL_CSRF_STATE" not in formatted
    assert "/api/auth/wecom/callback?[REDACTED]" in formatted


def test_access_log_filter_leaves_unrelated_paths_untouched() -> None:
    from app.main import _RedactOAuthCallbackQueryFilter

    record = _make_access_log_record("/api/conversations?sender=someone")
    _RedactOAuthCallbackQueryFilter().filter(record)

    assert "/api/conversations?sender=someone" in record.getMessage()


def test_access_log_filter_registered_on_uvicorn_access_logger() -> None:
    """Guard against the filter only existing as an unused class — it must
    actually be attached to the logger uvicorn's access log writes through."""
    import app.main as main_mod  # noqa: F401 — import triggers registration

    access_logger = logging.getLogger("uvicorn.access")
    assert any(
        isinstance(f, main_mod._RedactOAuthCallbackQueryFilter)
        for f in access_logger.filters
    )


# ---------------------------------------------------------------------------
# B3-R — externally-controlled fields must never be logged verbatim unless
# they're a confirmed plain int (log-forging / injection guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "0"),
        (40029, "40029"),
        (-1, "-1"),
    ],
)
def test_safe_log_value_shows_real_ints_verbatim(value, expected) -> None:
    from app.auth import safe_log_value

    assert safe_log_value(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        "QA_SECRET_MARKER_RND225",
        "0",
        None,
        ["not", "an", "int"],
        {"nested": "object"},
    ],
)
def test_safe_log_value_redacts_everything_else(value) -> None:
    """
    B3-R: a field that failed strict_int_equals (so is anything other than
    a real int) must never appear in a log verbatim — an adversarial or
    malformed provider response could otherwise inject arbitrary text
    (including things that merely look like secrets) into application logs.
    """
    from app.auth import safe_log_value

    rendered = safe_log_value(value)
    assert rendered == f"<{type(value).__name__}>"
    if isinstance(value, str):
        assert value not in rendered


def test_callback_user_get_errcode_marker_value_not_logged_verbatim(
    client, monkeypatch, caplog
) -> None:
    """
    End-to-end guard for B3-R: a non-integer errcode containing text that
    resembles a secret/credential must not reach the application log
    verbatim — only its type name should appear.
    """
    marker = "QA_OAUTH_CODE_MARKER_RND225"
    override, engine = _real_db_override_with_engine("tenant-errcode-marker")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get={"errcode": marker, "userid": "zhangsan", "status": 1},
    )

    with caplog.at_level(logging.WARNING):
        resp = client.get(
            "/api/auth/wecom/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )

    _assert_login_rejected(resp)
    assert _session_count(engine) == 0
    assert marker not in caplog.text
    assert "<str>" in caplog.text


# ---------------------------------------------------------------------------
# B2-R — nothing that can raise may run between a successful db.commit() and
# `return response`; the informational success log must happen BEFORE
# commit, not after, or a log failure could leave a committed session whose
# cookie never actually reached the client
# ---------------------------------------------------------------------------


def test_callback_success_log_failure_before_commit_leaves_no_session(
    client, monkeypatch
) -> None:
    """
    Regression guard for B2-R: even if the informational "login success" log
    call itself raises, that must not leave a committed-but-cookie-less
    session — because it now runs BEFORE db.commit(), a failure there still
    triggers the same rollback-and-reject path as any other failure in this
    block, rather than firing after the point of no return.
    """
    from app.routers import auth as auth_router_mod

    override, engine = _real_db_override_with_engine("tenant-success-log-fail")
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get={"errcode": 0, "userid": "zhangsan", "status": 1, "name": "Zhang San"},
    )

    real_logger_info = auth_router_mod.logger.info

    def _raise_on_success_log(msg, *a, **k):
        if "login success" in msg:
            raise RuntimeError("logging backend unavailable")
        return real_logger_info(msg, *a, **k)

    monkeypatch.setattr(auth_router_mod.logger, "info", _raise_on_success_log)

    resp = client.get(
        "/api/auth/wecom/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    _assert_login_rejected(resp)
    assert _session_count(engine) == 0


# ---------------------------------------------------------------------------
# B4 — auth-path failure logs must never leak session tokens / SQL params
# ---------------------------------------------------------------------------


def test_get_current_user_db_failure_log_does_not_leak_session_token(caplog) -> None:
    import logging as _logging

    from app.auth import get_current_user

    sensitive_session_id = "super-secret-session-token-abcdef123456"

    class _LeakyError(RuntimeError):
        """Simulates a DBAPI error whose str() embeds bound query params —
        a real occurrence with several SQLAlchemy driver error classes."""

        def __str__(self) -> str:
            return f"connection failed while binding params: session_id={sensitive_session_id}"

    broken_db = MagicMock()
    broken_db.query.side_effect = _LeakyError()

    with caplog.at_level(_logging.ERROR):
        with pytest.raises(HTTPException):
            get_current_user(session_id=sensitive_session_id, db=broken_db)

    assert sensitive_session_id not in caplog.text
    assert "_LeakyError" in caplog.text


def test_resolve_session_tenant_id_db_failure_log_does_not_leak_session_token(caplog) -> None:
    import logging as _logging

    from app.auth import require_html_session

    sensitive_session_id = "super-secret-session-token-zyxwvu987654"

    class _LeakyError(RuntimeError):
        def __str__(self) -> str:
            return f"connection failed while binding params: session_id={sensitive_session_id}"

    request = MagicMock()
    request.cookies = {"session_id": sensitive_session_id}
    broken_db = MagicMock()
    broken_db.query.side_effect = _LeakyError()

    with caplog.at_level(_logging.ERROR):
        assert require_html_session(request, broken_db) is None

    assert sensitive_session_id not in caplog.text
    assert "_LeakyError" in caplog.text


def test_callback_session_creation_db_failure_log_does_not_leak_session_id(
    client, monkeypatch, caplog
) -> None:
    import logging as _logging

    from app.db.session import get_db
    from app.main import app

    override, engine = _real_db_override_with_engine("tenant-dbfail-log")
    with Session(engine) as db:
        # RND-321: a bound, active identity is required to reach the
        # session-creation step this test targets — an unbound scan now
        # exits earlier via the access-request path and never gets to the
        # commit this test injects a failure into.
        db.add(
            AdminUser(
                id="pre-authorized-dbfail-account",
                tenant_id="tenant-dbfail-log",
                wecom_user_id="zhangsan",
                role="compliance",
                status="active",
            )
        )
        db.add(
            AdminLoginIdentity(
                id="login-identity-dbfail-account",
                tenant_id="tenant-dbfail-log",
                provider="wecom",
                subject="zhangsan",
                admin_user_id="pre-authorized-dbfail-account",
            )
        )
        db.commit()
    state = _prepare_callback_env(monkeypatch, override)
    _patch_wecom_http(
        monkeypatch,
        getuserinfo={"errcode": 0, "UserId": "zhangsan"},
        user_get={"errcode": 0, "userid": "zhangsan", "status": 1, "name": "Zhang San"},
    )

    class _LeakyCommitError(RuntimeError):
        def __str__(self) -> str:
            return "commit failed while binding params: wecom_user_id=zhangsan"

    real_override = app.dependency_overrides[get_db]

    def _broken_commit():
        gen = real_override()
        session = next(gen)
        session.commit = MagicMock(side_effect=_LeakyCommitError())
        yield session

    app.dependency_overrides[get_db] = _broken_commit

    with caplog.at_level(_logging.ERROR):
        resp = client.get(
            "/api/auth/wecom/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )

    _assert_login_rejected(resp)
    assert "zhangsan" not in caplog.text
    assert "_LeakyCommitError" in caplog.text
    assert _session_count(engine) == 0


# ---------------------------------------------------------------------------
# M1 — a stale-but-valid session cookie must not mask a failed-login error
# ---------------------------------------------------------------------------


def test_admin_login_with_error_and_valid_session_shows_error_not_redirect(client, db) -> None:
    """
    A failed OAuth callback redirects to /admin/login?error=.... If the
    browser also still carries an older, still-valid session cookie, the
    login page must show the error, not silently bounce to the console —
    otherwise a failed re-auth attempt is indistinguishable from success.
    """
    from app.db.session import get_db
    from app.main import app

    tenant = _make_tenant(db)
    user = _make_admin_user(db, tenant.id)
    _make_session(db, user, tenant.id, session_id="sess-stale-valid")

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    client.cookies.set("session_id", "sess-stale-valid")
    resp = client.get(
        "/admin/login", params={"error": "auth_failed"}, follow_redirects=False
    )

    assert resp.status_code == 200
    assert b"login-error" in resp.content


def test_admin_login_without_error_and_valid_session_still_redirects(client, db) -> None:
    """Regression guard: the ordinary already-authenticated bounce to the
    dashboard (no error param) is unchanged by the M1 fix."""
    from app.db.session import get_db
    from app.main import app

    tenant = _make_tenant(db)
    user = _make_admin_user(db, tenant.id)
    _make_session(db, user, tenant.id, session_id="sess-stale-valid-2")

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    client.cookies.set("session_id", "sess-stale-valid-2")
    resp = client.get("/admin/login", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"
