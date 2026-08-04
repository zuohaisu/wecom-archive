"""RND-321 — PC WeCom QR login uses the existing OAuth session flow."""

from __future__ import annotations

import json
import logging
import re
import shutil
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import AdminSession, AdminUser, Base, Tenant, TenantWecomConfig
from tests._node_runner import run_node


class _Response:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self):
        return self.payload


class _WeComClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, url, params=None):
        if "getuserinfo" in url:
            return _Response({"errcode": 0, "UserId": "qr-user"})
        if "user/get" in url:
            return _Response({"errcode": 0, "userid": "qr-user", "status": 1, "name": "QR User"})
        raise AssertionError(f"unexpected WeCom URL: {url}")


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, TenantWecomConfig.__table__, AdminUser.__table__, AdminSession.__table__],
    )
    yield engine
    engine.dispose()


@pytest.fixture()
def client(db_engine):
    from app.db.session import get_db
    from app.main import app

    def override_db():
        with Session(db_engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _mock_wecom_token(monkeypatch):
    """wecom_qr_login() calls get_wecom_token() as a config preflight
    (QA-002 round 2) — auto-mocked so hitting /api/auth/wecom/qr/login
    never makes a real network call by default. A test that needs
    different behavior (e.g. simulating invalid credentials) overrides
    this with its own patch.object(...), which composes fine: it restores
    to this fixture's value on exit, and this fixture restores the true
    original after the test.
    """
    from app.routers import auth as auth_router

    monkeypatch.setattr(auth_router, "get_wecom_token", lambda *a, **k: "token")


def _seed_config(engine, corp_id: str = "corp-qr") -> None:
    with Session(engine) as db:
        db.add(Tenant(id="tenant-qr", name="QR tenant", slug="qr", is_active=True))
        db.add(
            TenantWecomConfig(
                id="config-qr",
                tenant_id="tenant-qr",
                corp_id=corp_id,
                agent_id="100001",
                app_secret="test-only",
                is_active=True,
            )
        )
        db.commit()


def _add_active_qr_user(engine, *, role: str = "admin") -> None:
    """Pre-authorize the employee returned by ``_WeComClient`` for login."""
    with Session(engine) as db:
        db.add(
            AdminUser(
                id="pre-authorized-qr-user",
                tenant_id="tenant-qr",
                wecom_user_id="qr-user",
                name="QR User",
                role=role,
                status="active",
            )
        )
        db.commit()


_BREAKOUT_RE = re.compile(r"\(window\.top\|\|window\)\.location\.replace\((\".*?\")\);")


def _breakout_target(response) -> str:
    """The path the QR callback tells the *top* window to navigate to.

    The QR flow can't answer with a bare 302: WeCom's self_redirect=true
    lands the redirect inside the login iframe, so a 302 would render the
    console in a 300x400 box. The callback returns a same-origin breakout
    page instead, and this is the only assertion that proves the user
    actually leaves the login page.
    """
    assert response.status_code == 200, response.status_code
    match = _BREAKOUT_RE.search(response.text)
    assert match is not None, f"no top-window breakout in response: {response.text[:400]}"
    return json.loads(match.group(1))


def _set_wecom_env(monkeypatch) -> None:
    monkeypatch.setenv("WECOM_CORP_ID", "corp-qr")
    monkeypatch.setenv("WECOM_AGENT_ID", "100001")
    monkeypatch.setenv("WECOM_OAUTH_SECRET", "test-only")
    monkeypatch.setenv("ADMIN_DOMAIN", "admin.example.test")


def test_qr_login_redirects_to_qrconnect_and_login_page_has_i18n(client, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    response = client.get("/api/auth/wecom/qr/login", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert "open.work.weixin.qq.com/wwopen/sso/qrConnect" in location
    assert "state=" in location
    assert "self_redirect=true" in location

    page = client.get("/admin/login")
    assert 'data-i18n="login.qrTitle"' in page.text
    assert 'data-i18n="login.qrScanHint"' in page.text


# ---------------------------------------------------------------------------
# QA-005 — ADMIN_DOMAIN observed in production *with* a scheme prefix
# (docs/ops/rnd-261-domain-cutover-runbook.md: `ADMIN_DOMAIN=https://
# qwhhcd.crowntime.cn`, flagged as an anomaly but left unfixed because
# OAuth wasn't enabled yet). Naively prepending "https://" doubles up into
# a callback WeCom can't reach — both OAuth entry points share the
# construction and must both be immune to this input shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "configured_domain",
    ["archive.crowntime.cn", "https://archive.crowntime.cn", "http://archive.crowntime.cn"],
)
def test_qr_login_redirect_uri_never_double_schemes(configured_domain: str, client, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    monkeypatch.setenv("ADMIN_DOMAIN", configured_domain)
    location = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"]
    assert "redirect_uri=" in location
    redirect_uri = location.split("redirect_uri=")[1].split("&")[0]
    decoded = urllib.parse.unquote(redirect_uri)
    assert decoded == "https://archive.crowntime.cn/api/auth/wecom/qr/callback"
    assert "https%3A%2F%2Fhttps" not in redirect_uri
    assert "https://https" not in decoded


@pytest.mark.parametrize(
    "configured_domain",
    ["archive.crowntime.cn", "https://archive.crowntime.cn", "http://archive.crowntime.cn"],
)
def test_wecom_oauth_login_redirect_uri_never_double_schemes(configured_domain: str, client, monkeypatch) -> None:
    """Same construction, the pre-existing in-WeCom OAuth entry point
    (RND-110) — not RND-321's own route, but sharing this ticket's fix via
    the extracted _wecom_callback_base helper, verified so the two paths
    cannot drift apart on this input again."""
    _set_wecom_env(monkeypatch)
    monkeypatch.setenv("ADMIN_DOMAIN", configured_domain)
    location = client.get("/api/auth/wecom/login", follow_redirects=False).headers["location"]
    redirect_uri = location.split("redirect_uri=")[1].split("&")[0]
    decoded = urllib.parse.unquote(redirect_uri)
    assert decoded == "https://archive.crowntime.cn/api/auth/wecom/callback"
    assert "https://https" not in decoded


def test_qr_login_missing_config_fails_closed(client, monkeypatch) -> None:
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.delenv("WECOM_AGENT_ID", raising=False)
    response = client.get("/api/auth/wecom/qr/login", follow_redirects=False)
    assert response.headers["location"] == "/admin/login?error=config_error"


def test_qr_login_missing_oauth_secret_fails_closed(client, monkeypatch) -> None:
    """oauth_secret is required up front now (QA-002 round 2): without it
    the callback could never complete anyway, so there's no reason to ever
    open the iframe."""
    _set_wecom_env(monkeypatch)
    monkeypatch.delenv("WECOM_OAUTH_SECRET", raising=False)
    response = client.get("/api/auth/wecom/qr/login", follow_redirects=False)
    assert response.headers["location"] == "/admin/login?error=config_error"

    from app.routers.auth import _login_page

    assert 'id="wecom-qr"' not in _login_page(mode="password")


def test_qr_login_refuses_to_open_iframe_on_invalid_wecom_credentials(client, monkeypatch) -> None:
    """QA-002 round 2: a flatly wrong corp_id/oauth_secret pair is caught
    here — the token-fetch call the callback already performs, reused as a
    preflight — instead of only surfacing once WeCom's own qrConnect page
    renders an error *inside* the iframe, where this app cannot detect it
    (cross-origin, verified unreadable in Chrome regardless of success or
    failure)."""
    _set_wecom_env(monkeypatch)
    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", side_effect=RuntimeError("bad credentials")):
        response = client.get("/api/auth/wecom/qr/login", follow_redirects=False)

    assert response.headers["location"] == "/admin/login?error=config_error"
    # Never reaches WeCom at all — the whole point is to fail before the
    # iframe would ever be pointed at a doomed qrConnect request.
    assert "qrConnect" not in response.headers["location"]


def test_qr_callback_rejects_invalid_and_reused_state_without_cookie(client, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    invalid = client.get(
        "/api/auth/wecom/qr/callback?code=x&state=forged",
        follow_redirects=False,
    )
    assert _breakout_target(invalid) == "/admin/login?error=invalid_state"
    assert "session_id" not in invalid.headers.get("set-cookie", "")

    started = client.get("/api/auth/wecom/qr/login", follow_redirects=False)
    state = started.headers["location"].split("state=")[1].split("&")[0]
    monkeypatch.delenv("WECOM_OAUTH_SECRET")
    first = client.get(
        f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
    )
    assert _breakout_target(first) == "/admin/login?error=config_error"
    reused = client.get(
        f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
    )
    assert _breakout_target(reused) == "/admin/login?error=invalid_state"
    assert "session_id" not in reused.headers.get("set-cookie", "")


def test_qr_callback_creates_disabled_access_request_without_session(client, db_engine, monkeypatch) -> None:
    """A verified employee is not automatically authorized as an admin."""
    _set_wecom_env(monkeypatch)
    _seed_config(db_engine)
    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _WeComClient
    ):
        started = client.get("/api/auth/wecom/qr/login", follow_redirects=False)
        state = started.headers["location"].split("state=")[1].split("&")[0]
        response = client.get(
            f"/api/auth/wecom/qr/callback?code=one-time-code&state={state}",
            follow_redirects=False,
        )
        repeat_state = client.get(
            "/api/auth/wecom/qr/login", follow_redirects=False
        ).headers["location"].split("state=")[1].split("&")[0]
        repeated = client.get(
            f"/api/auth/wecom/qr/callback?code=another-code&state={repeat_state}",
            follow_redirects=False,
        )

    assert _breakout_target(response) == "/admin/login?error=access_pending"
    assert _breakout_target(repeated) == "/admin/login?error=access_pending"
    assert "session_id" not in response.headers.get("set-cookie", "")
    assert "session_id" not in repeated.headers.get("set-cookie", "")
    with Session(db_engine) as db:
        user = db.query(AdminUser).filter_by(tenant_id="tenant-qr", wecom_user_id="qr-user").one()
        assert user.role == "readonlyaudit"
        assert user.status == "disabled"
        assert user.invite_status == "access_requested"
        assert db.query(AdminSession).count() == 0


def test_qr_callback_creates_tenant_bound_session_only_for_pre_authorized_user(
    client, db_engine, monkeypatch
) -> None:
    _set_wecom_env(monkeypatch)
    _seed_config(db_engine)
    _add_active_qr_user(db_engine, role="compliance")
    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _WeComClient
    ):
        state = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
        response = client.get(
            f"/api/auth/wecom/qr/callback?code=one-time-code&state={state}",
            follow_redirects=False,
        )

    assert _breakout_target(response) == "/admin/conversations"
    cookie = response.headers.get("set-cookie", "")
    assert "session_id" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "one-time-code" not in response.text
    with Session(db_engine) as db:
        user = db.query(AdminUser).filter_by(tenant_id="tenant-qr", wecom_user_id="qr-user").one()
        assert user.role == "compliance"
        sessions = db.query(AdminSession).all()
        assert len(sessions) == 1
        assert sessions[0].tenant_id == "tenant-qr"
        assert sessions[0].wecom_user_id == "qr-user"


# ---------------------------------------------------------------------------
# AC-9 data minimization — docs/agent-data-minimization.md §5's required
# sentinel battery, applied recursively rather than the two ad hoc substring
# checks this file started with (QA-003: that was INSUFFICIENT_TEST_COVERAGE
# — a real leak of an untested field, or one that only appears at a nested
# path, would have passed).
# ---------------------------------------------------------------------------

SENTINELS = (
    "SENTINEL_MESSAGE_BODY",
    "SENTINEL_STRUCTURED_CONTENT",
    "SENTINEL_PASSWORD",
    "SENTINEL_PASSWORD_HASH",
    "SENTINEL_TOKEN",
    "SENTINEL_SECRET",
    "SENTINEL_SIGNED_URL",
    "SENTINEL_STORAGE_KEY",
    "/sentinel/fs/path",
    "SENTINEL_SEARCH_TEXT",
    "SENTINEL_TRACEBACK",
    "SENTINEL_SENDER",
    "SENTINEL_RECIPIENT",
    "SENTINEL_ROOM",
    "SENTINEL_RAW_MSGID",
)


def assert_no_sentinels(obj, path="$"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert not any(s in str(key) for s in SENTINELS), f"{path}.{key}"
            assert_no_sentinels(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            assert_no_sentinels(value, f"{path}[{i}]")
    else:
        assert not any(s in str(obj) for s in SENTINELS), path


def test_qr_callback_response_headers_and_logs_carry_no_sentinel_leakage(
    client, db_engine, monkeypatch, caplog
) -> None:
    """The breakout response (body + headers) and this module's own log
    records must be free of every sentinel, checked recursively. The
    callback code (`code`), state token, and WeCom access token are the
    values most likely to end up somewhere they shouldn't; each is set to a
    distinct sentinel so a leak of any single one is individually
    attributable.

    Deliberately does not exercise `/api/auth/me`: its response intentionally
    includes the caller's own `wecom_user_id` (telling authenticated users
    who they are is the endpoint's purpose, and that response shape predates
    RND-321) — a `SENTINEL_RAW_MSGID`-shaped value there is expected
    present, not a leak of the "raw msgid in archived message metadata"
    class docs/agent-data-minimization.md actually targets.
    """
    _set_wecom_env(monkeypatch)
    _seed_config(db_engine, corp_id="corp-qr")
    from app.routers import auth as auth_router

    class _SentinelClient(_WeComClient):
        def get(self, url, params=None):
            if "getuserinfo" in url:
                return _Response({"errcode": 0, "UserId": "sentinel-employee"})
            if "user/get" in url:
                return _Response(
                    {
                        "errcode": 0,
                        "userid": "sentinel-employee",
                        "status": 1,
                        "name": "SENTINEL_SENDER",
                    }
                )
            raise AssertionError(f"unexpected WeCom URL: {url}")

    with caplog.at_level(logging.INFO), patch.object(
        auth_router, "get_wecom_token", return_value="SENTINEL_TOKEN"
    ), patch.object(auth_router.httpx, "Client", _SentinelClient):
        state = client.get(
            "/api/auth/wecom/qr/login", follow_redirects=False
        ).headers["location"].split("state=")[1].split("&")[0]
        response = client.get(
            f"/api/auth/wecom/qr/callback?code=SENTINEL_SECRET&state={state}",
            follow_redirects=False,
        )

    # Scoped to this ticket's own logger namespace. The unfiltered caplog
    # stream also carries app.audit's write_audit() — fail-safe by design
    # (never raises), but its except-block logs the raw exception, and this
    # in-memory SQLite fixture has no audit_logs table (Postgres-only JSONB
    # column — see test_rnd293_audit_log.py, which gates real coverage
    # behind a live DATABASE_URL for the same reason). That failure mode
    # doesn't occur in production and app/audit.py isn't part of RND-321;
    # scoping to this module's own records tests what this diff owns
    # without asserting on an unrelated, pre-existing fixture gap.
    own_log_records = [r.getMessage() for r in caplog.records if r.name == "app.routers.auth"]
    assert own_log_records, "expected at least one log record from the callback"

    combined = {
        "breakout_response_body": response.text,
        "response_headers": dict(response.headers),
        "own_module_logs": own_log_records,
    }
    assert_no_sentinels(combined)


def _breakout_script(html: str) -> str:
    match = re.search(
        r"<script>\s*(var nodes=document\.querySelectorAll.*?location\.replace\([^;]*\);)\s*</script>",
        html,
        re.DOTALL,
    )
    assert match is not None, "breakout script not found in response"
    return match.group(1)


def test_qr_breakout_script_dom_console_and_storage_carry_no_sentinel_leakage() -> None:
    """docs/agent-data-minimization.md §5's frontend requirement: sentinels
    placed in a mocked response, asserted absent from
    document.body.innerHTML-equivalent, any attribute value, console
    output, and browser storage — run against the REAL breakout script
    (extracted from _break_out_of_qr_frame's actual output) under Node,
    the same technique test_i18n_foundation.py already uses for this
    repo's other server-rendered inline scripts (no template engine, no
    Jinja — see DEV_AGENT_RULES.md).

    The earlier sentinel test only checked the raw HTTP response text; this
    is what QA-003 round 2 correctly flagged as missing — the DOM/console/
    storage state *after* the script actually executes, not just the
    static markup it was served as.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available in this environment")

    from app.routers.auth import _break_out_of_qr_frame

    fake_redirect = SimpleNamespace(
        headers={"location": "/admin/login?error=SENTINEL_TOKEN"}, raw_headers=[]
    )
    html = _break_out_of_qr_frame(fake_redirect).body.decode("utf-8")
    script = _breakout_script(html)

    i18n_source = (
        Path(__file__).resolve().parent.parent / "app" / "assets" / "i18n.js"
    ).read_text(encoding="utf-8")

    harness = f"""
var __store={{}};
var localStorage={{
  getItem:function(k){{return Object.prototype.hasOwnProperty.call(__store,k)?__store[k]:null;}},
  setItem:function(k,v){{__store[k]=String(v);}},
  removeItem:function(k){{delete __store[k];}}
}};
eval({json.dumps(i18n_source)});

var domWrites=[];
var consoleCalls=[];
var storageWrites=[];
var navigations=[];

var realSetItem=localStorage.setItem;
localStorage.setItem=function(k,v){{storageWrites.push([k,v]);return realSetItem(k,v);}};
var sessionStorage={{setItem:function(k,v){{storageWrites.push([k,v]);}},getItem:function(){{return null;}},removeItem:function(){{}}}};

console={{log:function(){{consoleCalls.push(Array.prototype.slice.call(arguments));}},
         error:function(){{consoleCalls.push(Array.prototype.slice.call(arguments));}},
         warn:function(){{consoleCalls.push(Array.prototype.slice.call(arguments));}}}};

function makeNode(i18nKey){{
  return {{
    _text:null,
    getAttribute:function(name){{return name==='data-i18n'?i18nKey:null;}},
    set textContent(v){{this._text=v;domWrites.push(v);}},
    get textContent(){{return this._text;}}
  }};
}}
var __nodes=[makeNode('login.pageTitle'), makeNode('login.qrContinue')];
var document={{
  querySelectorAll:function(sel){{return sel==='[data-i18n]'?__nodes:[];}},
  documentElement:{{lang:null}}
}};
var window={{}};
window.location={{replace:function(url){{navigations.push(url);}}}};
window.top=window;

{script}

process.stdout.write(JSON.stringify({{domWrites:domWrites,consoleCalls:consoleCalls,storageWrites:storageWrites,navigations:navigations}}));
"""
    result = run_node(harness)
    assert result.returncode == 0, result.stderr
    captured = json.loads(result.stdout)

    # The navigation target itself legitimately carries the (here,
    # deliberately sentinel-laced) same-origin path — that's the
    # mechanism, not a leak. Everything else must be clean.
    assert_no_sentinels(
        {
            "dom_text_writes": captured["domWrites"],
            "console_calls": captured["consoleCalls"],
            "storage_writes": captured["storageWrites"],
        }
    )
    assert captured["navigations"] == ["/admin/login?error=SENTINEL_TOKEN"]


def test_qr_callback_fails_closed_without_tenant_config_or_for_inactive_user(client, db_engine, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _WeComClient
    ):
        state = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
        missing_tenant = client.get(
            f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
        )
    assert _breakout_target(missing_tenant) == "/admin/login?error=config_error"

    _seed_config(db_engine)

    class _InactiveClient(_WeComClient):
        def get(self, url, params=None):
            if "getuserinfo" in url:
                return _Response({"errcode": 0, "UserId": "qr-user"})
            return _Response({"errcode": 0, "userid": "qr-user", "status": 2})

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _InactiveClient
    ):
        state = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
        inactive = client.get(
            f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
        )
    assert _breakout_target(inactive) == "/admin/login?error=user_inactive"
    assert "session_id" not in inactive.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# QA-001 (fix round 1) — an internally-suspended account must fail closed
# even when WeCom itself reports the underlying employee as active. WeCom's
# employment status and this app's own admin_users.status are independent
# facts; only the upsert branch checked WeCom's, never our own, so a
# disabled account — or one an admin invited but who never accepted (created
# disabled on purpose, see _create_pending_invite) — could self-activate via
# a bare WeCom/QR login and skip password setup entirely.
# ---------------------------------------------------------------------------


def test_qr_callback_rejects_internally_disabled_existing_account(client, db_engine, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    _seed_config(db_engine)
    with Session(db_engine) as db:
        db.add(
            AdminUser(
                id="disabled-user",
                tenant_id="tenant-qr",
                wecom_user_id="qr-user",
                name="Disabled User",
                status="disabled",
            )
        )
        db.commit()

    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _WeComClient
    ):
        state = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
        response = client.get(
            f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
        )

    assert _breakout_target(response) == "/admin/login?error=user_inactive"
    assert "session_id" not in response.headers.get("set-cookie", "")
    with Session(db_engine) as db:
        assert db.query(AdminSession).count() == 0
        # The account itself must be left untouched — no accidental
        # re-activation, name overwrite, or last_login_at bump on a
        # rejected attempt.
        refreshed = db.query(AdminUser).filter_by(id="disabled-user").first()
        assert refreshed.status == "disabled"
        assert refreshed.name == "Disabled User"
        assert refreshed.last_login_at is None


def test_qr_callback_rejects_a_pending_invite_that_never_accepted(client, db_engine, monkeypatch) -> None:
    """The more severe case: an invited-but-not-yet-accepted account is
    created with status="disabled" specifically so the invitee must set a
    password via accept-invite first. A bare WeCom/QR login must not be a
    side door around that — the account must stay pending, not silently
    flip active."""
    _set_wecom_env(monkeypatch)
    _seed_config(db_engine)
    with Session(db_engine) as db:
        db.add(
            AdminUser(
                id="pending-user",
                tenant_id="tenant-qr",
                wecom_user_id="qr-user",
                status="disabled",
                invite_status="pending",
                invite_token="unused-invite-token",
            )
        )
        db.commit()

    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _WeComClient
    ):
        state = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
        response = client.get(
            f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
        )

    assert _breakout_target(response) == "/admin/login?error=user_inactive"
    assert "session_id" not in response.headers.get("set-cookie", "")
    with Session(db_engine) as db:
        refreshed = db.query(AdminUser).filter_by(id="pending-user").first()
        assert refreshed.status == "disabled"
        assert refreshed.invite_status == "pending"
        assert refreshed.invite_token == "unused-invite-token"


def test_me_stops_confirming_identity_the_moment_the_account_is_disabled(client, db_engine, monkeypatch) -> None:
    """Defense in depth: disabling an account must take effect on the very
    next request, not just block the *next login* — an already-issued,
    unexpired, unrevoked session cookie must stop working immediately."""
    _set_wecom_env(monkeypatch)
    _seed_config(db_engine)
    _add_active_qr_user(db_engine)
    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _WeComClient
    ):
        state = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
        login = client.get(
            f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
        )
    cookie_header = login.headers["set-cookie"]
    session_id = cookie_header.split("session_id=")[1].split(";")[0]

    before = client.get("/api/auth/me", cookies={"session_id": session_id})
    assert before.json()["authenticated"] is True

    with Session(db_engine) as db:
        db.query(AdminUser).filter_by(tenant_id="tenant-qr", wecom_user_id="qr-user").update(
            {"status": "disabled"}
        )
        db.commit()

    after = client.get("/api/auth/me", cookies={"session_id": session_id})
    assert after.json()["authenticated"] is False


def _login_and_get_session_id(client, db_engine, monkeypatch) -> str:
    _set_wecom_env(monkeypatch)
    _seed_config(db_engine)
    _add_active_qr_user(db_engine)
    from app.routers import auth as auth_router

    with patch.object(auth_router, "get_wecom_token", return_value="token"), patch.object(
        auth_router.httpx, "Client", _WeComClient
    ):
        state = client.get("/api/auth/wecom/qr/login", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
        login = client.get(
            f"/api/auth/wecom/qr/callback?code=x&state={state}", follow_redirects=False
        )
    return login.headers["set-cookie"].split("session_id=")[1].split(";")[0]


def _disable(db_engine) -> None:
    with Session(db_engine) as db:
        db.query(AdminUser).filter_by(tenant_id="tenant-qr", wecom_user_id="qr-user").update(
            {"status": "disabled"}
        )
        db.commit()


# ---------------------------------------------------------------------------
# QA-001 round 2 — the fix in app/auth.py's get_current_user and
# require_html_session is shared by every protected route in the app, not
# just this file's own /api/auth/me. Proven here against genuinely
# unrelated routers (analytics, audit-log HTML page) — not to test their
# business logic, but to prove the *dependency* rejects the request before
# that logic ever runs (a disabled user's DB rows for those features don't
# exist in this fixture; a 401/redirect proves the gate fired first).
# ---------------------------------------------------------------------------


def test_disabled_account_is_rejected_by_a_genuinely_unrelated_api_router(
    client, db_engine, monkeypatch
) -> None:
    """GET /api/admin/usage (app/routers/analytics.py) depends on
    get_current_user — same shared dependency /api/auth/me uses, but a
    completely different router this ticket doesn't own. If the fix only
    worked for this ticket's own endpoints, this would 200 (or fail on a
    missing table reached via business logic) instead of 401 at the gate.
    """
    session_id = _login_and_get_session_id(client, db_engine, monkeypatch)

    before = client.get("/api/admin/usage", cookies={"session_id": session_id})
    assert before.status_code != 401

    _disable(db_engine)

    after = client.get("/api/admin/usage", cookies={"session_id": session_id})
    assert after.status_code == 401


def test_disabled_account_is_redirected_to_login_by_a_genuinely_unrelated_html_router(
    client, db_engine, monkeypatch
) -> None:
    """GET /admin/audit-logs (app/routers/admin_audit_page.py) depends on
    require_html_session — the HTML-page counterpart of get_current_user,
    fixed the same way. A disabled account's still-valid session cookie
    must bounce to /admin/login, not render the page.
    """
    session_id = _login_and_get_session_id(client, db_engine, monkeypatch)

    before = client.get(
        "/admin/audit-logs", cookies={"session_id": session_id}, follow_redirects=False
    )
    assert before.status_code == 200

    _disable(db_engine)

    after = client.get(
        "/admin/audit-logs", cookies={"session_id": session_id}, follow_redirects=False
    )
    assert after.status_code == 302
    assert after.headers["location"] == "/admin/login"


# ---------------------------------------------------------------------------
# The delivered login page (AC-1 / AC-2)
#
# The original RND-321 regression: the QR block lived only in the `wecom`
# branch of _login_page, but production runs AUTH_MODE=password
# (docs/DEPLOYMENT.md), so the page users actually got had no scan entry at
# all. The historical suite missed it because the test env leaves AUTH_MODE
# unset, which defaults to `wecom` — the one mode that did render it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["password", "wecom"])
def test_login_page_shows_qr_entry_in_every_auth_mode(mode: str, monkeypatch) -> None:
    _set_wecom_env(monkeypatch)
    from app.routers.auth import _login_page

    html = _login_page(mode=mode)
    assert 'id="wecom-qr"' in html
    assert 'id="wecom-qr-iframe"' in html
    assert 'data-i18n="login.qrTitle"' in html
    # Not merely present — reachable without dev tools or a URL change.
    assert "hidden" not in html.split('id="wecom-qr"')[1].split(">")[0]


def test_password_mode_keeps_its_form_alongside_the_qr_entry(monkeypatch) -> None:
    """AC-7: the QR entry is an addition, not a replacement."""
    _set_wecom_env(monkeypatch)
    from app.routers.auth import _login_page

    html = _login_page(mode="password")
    assert 'id="pwd-form"' in html
    assert 'data-i18n="login.submit"' in html
    assert 'id="wecom-qr"' in html


def test_qr_entry_is_hidden_when_wecom_is_not_configured(monkeypatch) -> None:
    """An unconfigured deploy would otherwise frame a nested login page."""
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.delenv("WECOM_AGENT_ID", raising=False)
    from app.routers.auth import _login_page

    html = _login_page(mode="password")
    assert 'id="wecom-qr"' not in html
    assert 'id="pwd-form"' in html


def test_qr_block_exposes_loading_error_expired_and_retry_states(monkeypatch) -> None:
    """AC-2: a bare iframe gives the user no way to tell stuck from broken."""
    _set_wecom_env(monkeypatch)
    from app.routers.auth import _login_page

    html = _login_page(mode="password")
    for key in (
        "login.qrLoading",
        "login.qrError",
        "login.qrExpired",
        "login.qrRetry",
        "login.qrFrameTitle",
    ):
        assert f'data-i18n="{key}"' in html or f'data-i18n-title="{key}"' in html, key
    assert 'id="wecom-qr-loading"' in html
    assert 'id="wecom-qr-error"' in html
    assert 'id="wecom-qr-expired"' in html
    assert html.count("data-qr-retry") >= 2
    # The frame must not be lazy — it is the primary call to action.
    assert 'loading="lazy"' not in html
    # QA-002: a WeCom-side config error (e.g. wrong/unauthorized appid)
    # renders *inside* the cross-origin iframe, which this app's own
    # loading/error/expired states cannot detect or override — the
    # same-origin policy blocks reading a cross-origin iframe's content
    # (verified: contentDocument, contentWindow.length, and .location are
    # all unreadable regardless of whether the navigation succeeded or
    # failed). This always-visible line is the honest mitigation: it does
    # not pretend to detect that case, it gives the user a way out of it.
    assert 'data-i18n="login.qrTrouble"' in html


def test_retry_requests_a_fresh_one_time_state(client, monkeypatch) -> None:
    """AC-2/AC-5: refreshing must not replay a consumed or expired state."""
    _set_wecom_env(monkeypatch)
    seen = set()
    for _ in range(3):
        location = client.get(
            "/api/auth/wecom/qr/login", follow_redirects=False
        ).headers["location"]
        seen.add(location.split("state=")[1].split("&")[0])
    assert len(seen) == 3

    from app.routers.auth import _login_page

    html = _login_page(mode="password")
    # Cache-busted so the browser re-hits the endpoint instead of reusing
    # the already-consumed state from the previous frame load.
    assert "'/api/auth/wecom/qr/login?_='+myAttempt" in html


def test_login_page_is_inert_when_itself_framed(monkeypatch) -> None:
    """Stops a misconfigured deploy nesting login pages recursively."""
    _set_wecom_env(monkeypatch)
    from app.routers.auth import _login_page

    assert "window.self!==window.top" in _login_page(mode="password")


def _qr_script(monkeypatch) -> str:
    """The QR state-machine IIFE exactly as it is served to the browser."""
    _set_wecom_env(monkeypatch)
    from app.routers.auth import _login_page

    html = _login_page(mode="password")
    match = re.search(
        r"<script>\s*(\(function\(\)\{\s*//\s*When the login page is itself framed.*?\}\)\(\);)\s*</script>",
        html,
        re.DOTALL,
    )
    assert match is not None, "QR bootstrap script not found in rendered page"
    return match.group(1)


_QR_DOM_SHIM = """
var STATE={};
var listeners={};
var timers=[];
function el(id){
  return {id:id,hidden:false,
    addEventListener:function(ev,fn){listeners[id+':'+ev]=fn;},
    focus:function(){}};
}
// Controls what frame.contentWindow does when the load handler probes it —
// mirrors the real browser split verified in Chrome: same-origin (our own
// redirect target) is readable, WeCom's actual cross-origin page throws.
var frameSameOrigin=false;
var iframeNode=el('wecom-qr-iframe');
Object.defineProperty(iframeNode,'contentWindow',{get:function(){
  if(frameSameOrigin){
    return {location:{href:'http://127.0.0.1:8035/admin/login?error=config_error'}};
  }
  var err=new Error('Blocked a frame with origin from accessing a cross-origin frame.');
  err.name='SecurityError';
  throw err;
}});
var nodes={'wecom-qr-loading':el('wecom-qr-loading'),
           'wecom-qr-error':el('wecom-qr-error'),
           'wecom-qr-expired':el('wecom-qr-expired'),
           'wecom-qr-iframe':iframeNode};
var document={
  getElementById:function(id){return nodes[id]||null;},
  querySelectorAll:function(){return [];}
};
var window={self:1,top:1,fetch:null};
function setTimeout(fn,ms){timers.push({fn:fn,ms:ms});return timers.length;}
function clearTimeout(h){if(h)timers[h-1]=null;}
function showing(){
  return Object.keys(nodes).filter(function(k){return !nodes[k].hidden;}).join(',');
}
function fireTimer(ms){
  timers.slice().forEach(function(t){if(t&&t.ms===ms){t.fn();}});
}
"""


def _run_qr_state_machine(monkeypatch, scenario: str) -> dict:
    """Execute the real QR script under Node against a scripted scenario."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available in this environment")
    harness = f"""
{_QR_DOM_SHIM}
var fetchBehaviour={json.dumps(scenario)};
// Deferred on purpose: in a real browser the iframe's load event usually
// beats the reachability answer, which is exactly the ordering that must
// not be mistaken for success. `var fetch` also shadows Node 22's real
// global fetch, which would otherwise hit the actual WeCom host.
var settleProbe=null;
var fetch=function(){{
  return {{then:function(ok){{
    return {{catch:function(bad){{
      settleProbe=function(){{fetchBehaviour==='reachable'?ok():bad();}};
      return null;
    }}}};
  }}}};
}};
window.fetch=fetch;
{_qr_script(monkeypatch)}
var out={{}};
out.afterInit=showing();
// 1. Frame reports "loaded" while reachability is still unknown.
if(listeners['wecom-qr-iframe:load'])listeners['wecom-qr-iframe:load']();
out.afterFrameLoadOnly=showing();
// 2. Reachability answer arrives.
if(settleProbe)settleProbe();
out.afterProbe=showing();
fireTimer(300000);
out.afterExpiryTimer=showing();
process.stdout.write(JSON.stringify(out));
"""
    result = run_node(harness)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_frame_redirected_back_to_our_own_origin_is_treated_as_an_error(monkeypatch) -> None:
    """Regression (found via manual browser verification, QA-002 round 2
    fix): /api/auth/wecom/qr/login can itself redirect the iframe back to
    our own /admin/login (missing/invalid WeCom config) instead of ever
    reaching WeCom. Before this fix, that redirect's `load` event — plus a
    reachable WeCom host on the *separate* no-cors probe — was read as
    success, silently nesting a full login page inside the 300x400 QR box
    with no visible error. Unlike WeCom's actual page (genuinely
    cross-origin, unreadable), this redirect never leaves our own origin,
    so contentWindow.location is readable without throwing — verified both
    directions in Chrome — which is exactly what distinguishes the two
    cases here.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available in this environment")
    harness = f"""
{_QR_DOM_SHIM}
var fetch=function(){{
  return {{then:function(ok){{
    return {{catch:function(bad){{ok();return null;}}}};
  }}}};
}};
window.fetch=fetch;
{_qr_script(monkeypatch)}
frameSameOrigin=true;
var out={{}};
if(listeners['wecom-qr-iframe:load'])listeners['wecom-qr-iframe:load']();
out.afterSameOriginRedirect=showing();
process.stdout.write(JSON.stringify(out));
"""
    result = run_node(harness)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["afterSameOriginRedirect"] == "wecom-qr-error", out


def test_frame_load_alone_does_not_count_as_a_working_qr(monkeypatch) -> None:
    """Regression: a cross-origin iframe fires `load` even when the
    navigation failed, and the parent cannot read contentDocument,
    contentWindow.length or location to tell success from failure (verified
    in Chrome). Treating `load` as success is what put a blank frame on the
    page instead of the error state, so the reachability signal — not the
    load event — must gate the QR being shown.
    """
    out = _run_qr_state_machine(monkeypatch, "unreachable")
    # The load event fired, but nothing yet says the client reached WeCom —
    # the user must still be looking at the loading state, not a frame.
    assert out["afterFrameLoadOnly"] == "wecom-qr-loading", out
    assert out["afterProbe"] == "wecom-qr-error", out


def test_reachable_client_sees_the_qr_then_the_expiry_state(monkeypatch) -> None:
    out = _run_qr_state_machine(monkeypatch, "reachable")
    assert out["afterFrameLoadOnly"] == "wecom-qr-loading", out
    assert out["afterProbe"] == "wecom-qr-iframe", out
    assert out["afterExpiryTimer"] == "wecom-qr-expired", out


_QR_COPY_KEYS = (
    "login.qrTitle",
    "login.qrScanHint",
    "login.qrFrameTitle",
    "login.qrLoading",
    "login.qrError",
    "login.qrExpired",
    "login.qrRetry",
    "login.qrContinue",
    "login.qrTrouble",
)


@pytest.mark.parametrize("code", ["zh-CN", "zh-TW", "en"])
def test_new_qr_copy_resolves_in_every_locale(code: str) -> None:
    """AC-8: every new string must resolve in all three locales.

    Executes the real i18n.js through I18N.t() rather than grepping the
    source: a key present in the file but shadowed, misplaced in another
    locale's block, or silently falling back to zh-CN still reads as
    "present" to a text search. Only the runtime lookup proves it.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available in this environment")

    source = (
        Path(__file__).resolve().parent.parent / "app" / "assets" / "i18n.js"
    ).read_text(encoding="utf-8")
    harness = f"""
var __store={{}};
var localStorage={{
  getItem:function(k){{return Object.prototype.hasOwnProperty.call(__store,k)?__store[k]:null;}},
  setItem:function(k,v){{__store[k]=String(v);}},
  removeItem:function(k){{delete __store[k];}}
}};
eval({json.dumps(source)});
I18N.setLocale({json.dumps(code)});
var out={{}};
{"".join(f"out[{json.dumps(k)}]=I18N.t({json.dumps(k)});" for k in _QR_COPY_KEYS)}
process.stdout.write(JSON.stringify(out));
"""
    result = run_node(harness)
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)

    for key in _QR_COPY_KEYS:
        value = resolved[key]
        # Missing everywhere → i18n.js returns the key name itself.
        assert value != key, f"{key} unresolved in {code}"
        assert value.strip(), f"{key} empty in {code}"
        # A code identifier leaking through as UI copy.
        assert not value.startswith("login."), f"{key} looks like a key in {code}"


def test_qr_copy_is_actually_translated_not_copied_across_locales() -> None:
    """A key silently falling back to zh-CN would pass a per-locale check."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available in this environment")

    source = (
        Path(__file__).resolve().parent.parent / "app" / "assets" / "i18n.js"
    ).read_text(encoding="utf-8")
    harness = f"""
var __store={{}};
var localStorage={{
  getItem:function(k){{return Object.prototype.hasOwnProperty.call(__store,k)?__store[k]:null;}},
  setItem:function(k,v){{__store[k]=String(v);}},
  removeItem:function(k){{delete __store[k];}}
}};
eval({json.dumps(source)});
var out={{}};
['zh-CN','zh-TW','en'].forEach(function(code){{
  I18N.setLocale(code);
  var per={{}};
  {"".join(f"per[{json.dumps(k)}]=I18N.t({json.dumps(k)});" for k in _QR_COPY_KEYS)}
  out[code]=per;
}});
process.stdout.write(JSON.stringify(out));
"""
    result = run_node(harness)
    assert result.returncode == 0, result.stderr
    per_locale = json.loads(result.stdout)

    for key in _QR_COPY_KEYS:
        assert per_locale["en"][key] != per_locale["zh-CN"][key], (
            f"{key} falls back to zh-CN in en"
        )
        assert per_locale["zh-TW"][key] != per_locale["zh-CN"][key], (
            f"{key} falls back to zh-CN in zh-TW"
        )
