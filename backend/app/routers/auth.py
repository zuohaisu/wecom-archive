"""
Auth routes for RND-110 (WeCom OAuth) and RND-112 (password fallback).

Routes (public):
  GET  /admin/login                   Login page HTML (mode-aware: password or WeCom)
  GET  /api/auth/wecom/login          Redirect to WeCom OAuth URL
  GET  /api/auth/wecom/callback       Handle WeCom OAuth callback
  POST /api/auth/password/login       Temporary password login (AUTH_MODE=password only)

Routes (session required):
  GET  /api/auth/me                   Current user metadata (always 200; authenticated field)
  POST /api/auth/logout               Revoke session, clear cookie

Security notes:
  - /api/wecom/archive/events is NOT in this router and is NOT protected by session auth.
  - State is validated and single-use before code exchange (WeCom mode).
  - No user-controlled redirect URLs are accepted.
  - WeCom code, access_token, secrets are never logged.
  - Submitted password, hash, and session token are never logged (password mode).
  - /api/auth/password/login is a 404 when AUTH_MODE != 'password'.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import hmac as _hmac

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import (
    PASSWORD_MODE_WECOM_PREFIX,
    SESSION_COOKIE,
    SESSION_TTL_HOURS,
    _is_production,
    consume_state,
    generate_state,
    get_auth_mode,
    get_wecom_token,
    safe_log_value,
    strict_int_equals,
    verify_password,
)
from app.db.models import AdminSession, AdminUser, Tenant, TenantWecomConfig
from app.db.session import get_db
from app.i18n_assets import I18N_SCRIPT_TAG
from app.settings import get_auth_settings, get_wecom_oauth_settings
from app.web import render_template

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

# Maps an ?error= query code to its i18n key + zh-CN (default locale) fallback
# text. The fallback text is what's baked into the server-rendered HTML —
# the client doesn't know the visitor's language preference (it lives only in
# localStorage), so the page renders in the default locale and the i18n
# bootstrap script swaps it client-side if a different locale was persisted.
_ERROR_MESSAGES: dict[str, tuple[str, str]] = {
    "invalid_state": ("login.error.invalidState", "登录会话已过期或请求被篡改，请重试。"),
    "auth_failed": ("login.error.authFailed", "认证失败，请重试。"),
    "user_inactive": ("login.error.userInactive", "您的企业微信账号已停用，请联系管理员。"),
    "config_error": ("login.error.configError", "服务器配置错误，请联系管理员。"),
}

def _login_page(mode: str = "wecom", error: Optional[str] = None) -> str:
    """Builds the mode-specific body injected into templates/login.html via
    render_template — the shell (design-system stylesheet, aside marketing
    copy, language switcher, i18n bootstrap) lives in the template; only the
    password-form-vs-WeCom-button choice and any `?error=` banner are
    Python-side. This is a visual restyle onto the new design system's
    markup/CSS classes — the login flow itself is unchanged (same element
    ids, same fetch/redirect behavior). The i18n keys are NOT all
    unchanged: `login.subtitle` is new (the page previously reused
    `app.subtitle`, shared with review_console.html) and
    `login.forgotPasswordDisabled` is new for the inert forgot-password
    entry — every other `login.*` key predates this restyle."""
    error_html = ""
    if error:
        key, fallback_text = _ERROR_MESSAGES.get(
            error, _ERROR_MESSAGES["auth_failed"]
        )
        error_html = (
            f'<div class="alert alert-danger mt-4" id="login-error" role="alert">'
            f'<span class="alert-ico">⚠</span><div data-i18n="{key}">{fallback_text}</div></div>'
        )

    if mode == "password":
        login_body = f"""\
  <form id="pwd-form" onsubmit="doLogin(event)">
    <div class="field">
      <label class="field-label" for="uname" data-i18n="login.username">用户名</label>
      <input class="input" type="text" id="uname" name="username" data-i18n-placeholder="login.username" placeholder="用户名"
             autocomplete="username" required>
    </div>
    <div class="field">
      <label class="field-label" for="pwd" data-i18n="login.password">密码</label>
      <input class="input" type="password" id="pwd" name="password" data-i18n-placeholder="login.password" placeholder="密码"
             autocomplete="current-password" required>
    </div>
    <button class="btn btn-primary btn-block btn-lg" type="submit" id="submit-btn" data-i18n="login.submit">登录</button>
  </form>
  <p class="field-help mt-2" style="text-align:right">
    <span id="forgot-password-disabled" aria-disabled="true" style="color:var(--color-text-5);cursor:not-allowed" data-i18n="login.forgotPasswordDisabled">忘记密码？（即将上线）</span>
  </p>
  {error_html}
  <p class="field-help mt-2" style="text-align:center" data-i18n="login.footerPassword">临时管理员登录 — 企业微信登录即将上线</p>
<script>
function doLogin(e){{
  e.preventDefault();
  var btn=document.getElementById('submit-btn');
  btn.disabled=true;btn.textContent=I18N.t('login.submitting');
  var errEl=document.getElementById('login-error');
  if(errEl)errEl.style.display='none';
  fetch('/api/auth/password/login',{{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{
      username:document.getElementById('uname').value,
      password:document.getElementById('pwd').value
    }})
  }}).then(function(r){{
    if(r.ok){{window.location.href='/admin/conversations';return;}}
    return r.json().then(function(d){{
      var el=document.getElementById('login-error');
      if(!el){{
        el=document.createElement('div');
        el.id='login-error';
        el.className='alert alert-danger mt-4';
        el.setAttribute('role','alert');
        el.innerHTML='<span class="alert-ico">⚠</span><div></div>';
        document.getElementById('pwd-form').after(el);
      }}
      el.style.display='';
      el.querySelector('div').textContent=I18N.t('login.invalidCredentials');
      btn.disabled=false;btn.textContent=I18N.t('login.submit');
    }});
  }}).catch(function(){{
    var el=document.getElementById('login-error');
    if(!el){{
      el=document.createElement('div');
      el.id='login-error';
      el.className='alert alert-danger mt-4';
      el.setAttribute('role','alert');
      el.innerHTML='<span class="alert-ico">⚠</span><div></div>';
      document.getElementById('pwd-form').after(el);
    }}
    el.style.display='';
    el.querySelector('div').textContent=I18N.t('login.genericFailure');
    btn.disabled=false;btn.textContent=I18N.t('login.submit');
  }});
}}
</script>"""
    else:
        login_body = f"""\
  <a class="btn btn-primary btn-block btn-wecom" href="/api/auth/wecom/login">
    <span data-i18n="login.wecomButton">使用企业微信登录</span>
  </a>
  {error_html}
  <p class="field-help mt-2" data-i18n="login.footerWecom">仅限企业内部员工访问</p>"""

    return render_template("login", i18n_script=I18N_SCRIPT_TAG, login_body=login_body)


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(
    request: Request,
    error: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Login page. Redirects to /admin/conversations if already authenticated.

    Renders password form when AUTH_MODE=password; WeCom button otherwise.

    An `?error=` query param always takes priority over the already-
    authenticated redirect: a failed OAuth callback redirects here with an
    error code, and a stale-but-still-valid session cookie from a previous
    login must never silently swallow that error behind a bounce back to
    the console — the failure needs to be visible, not indistinguishable
    from success.
    """
    session_id = request.cookies.get(SESSION_COOKIE) if request else None
    if session_id and not error:
        now = datetime.now(timezone.utc)
        session = (
            db.query(AdminSession)
            .filter(
                AdminSession.id == session_id,
                AdminSession.expires_at > now,
                AdminSession.is_revoked.is_(False),
            )
            .first()
        )
        if session:
            return RedirectResponse("/admin/conversations", status_code=302)

    mode = get_auth_mode()
    safe_error = error if error in _ERROR_MESSAGES else (error and "auth_failed")
    return HTMLResponse(content=_login_page(mode=mode, error=safe_error))


# ---------------------------------------------------------------------------
# Password login (RND-112 — AUTH_MODE=password only)
# ---------------------------------------------------------------------------


class _PasswordLoginBody(BaseModel):
    username: str
    password: str


@router.post("/api/auth/password/login")
def password_login(
    body: _PasswordLoginBody,
    db: Session = Depends(get_db),
):
    """
    Temporary username/password login (RND-112).

    Only active when AUTH_MODE=password; returns 404 in wecom mode.
    Verifies ADMIN_USERNAME and ADMIN_PASSWORD_HASH from environment.
    On success: creates an AdminSession bound to the default tenant and sets
    an HttpOnly session cookie (same flags as WeCom OAuth sessions).
    On failure: returns 401 with a sanitized error — no session is created.
    Never logs submitted password, stored hash, or session token.
    """
    if get_auth_mode() != "password":
        raise HTTPException(status_code=404, detail="Not found")

    logger.info("password_login: attempt received")

    auth_settings = get_auth_settings()
    admin_username = auth_settings.admin_username.strip()
    admin_hash = auth_settings.admin_password_hash.strip()

    if not admin_username or not admin_hash:
        logger.error("password_login: ADMIN_USERNAME or ADMIN_PASSWORD_HASH not configured")
        raise HTTPException(status_code=500, detail="Server configuration error")

    # Constant-time username comparison + PBKDF2 password verification.
    # Both checks always run to prevent timing oracle on username enumeration.
    username_ok = _hmac.compare_digest(body.username, admin_username)
    password_ok = verify_password(body.password, admin_hash)

    if not (username_ok and password_ok):
        logger.warning("password_login: failed (credentials not logged)")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Resolve default tenant — bound to slug='default' created by RND-111 bootstrap.
    # Must NEVER fall back to any other tenant: password-mode sessions are only
    # ever valid for the RND-111 default tenant. If it's missing or inactive,
    # fail closed rather than binding to an arbitrary active tenant.
    tenant = (
        db.query(Tenant)
        .filter(Tenant.slug == "default", Tenant.is_active.is_(True))
        .first()
    )
    if tenant is None:
        logger.error("password_login: default tenant missing or inactive — run bootstrap_default_tenant.py")
        raise HTTPException(status_code=500, detail="Server configuration error")

    tenant_id: str = tenant.id
    now = datetime.now(timezone.utc)

    # Sentinel wecom_user_id for password-mode users (no real WeCom identity).
    wecom_sentinel = f"{PASSWORD_MODE_WECOM_PREFIX}{admin_username}__"

    # Upsert AdminUser row for the password-mode account.
    user = (
        db.query(AdminUser)
        .filter(
            AdminUser.tenant_id == tenant_id,
            AdminUser.wecom_user_id == wecom_sentinel,
        )
        .first()
    )
    if user is None:
        user = AdminUser(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            wecom_user_id=wecom_sentinel,
            name=admin_username,
            last_login_at=now,
        )
        db.add(user)
    else:
        user.last_login_at = now
    db.flush()

    # Create session row.
    session_id = str(uuid.uuid4())
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
    session = AdminSession(
        id=session_id,
        admin_user_id=user.id,
        tenant_id=tenant_id,
        wecom_user_id=wecom_sentinel,
        expires_at=expires_at,
        is_revoked=False,
    )
    db.add(session)
    db.commit()

    logger.info("password_login: success, session created (id not logged)")

    response = JSONResponse({"logged_in": True})
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
        max_age=SESSION_TTL_HOURS * 3600,
    )
    return response


# ---------------------------------------------------------------------------
# WeCom OAuth initiation
# ---------------------------------------------------------------------------


@router.get("/api/auth/wecom/login")
def wecom_login():
    """Redirect the browser to the WeCom OAuth authorization URL."""
    wecom_oauth_settings = get_wecom_oauth_settings()
    corp_id = wecom_oauth_settings.wecom_corp_id.strip()
    agent_id = wecom_oauth_settings.wecom_agent_id.strip()
    admin_domain = wecom_oauth_settings.admin_domain.strip()

    if not corp_id or not agent_id:
        logger.error("wecom_login: WECOM_CORP_ID or WECOM_AGENT_ID not configured")
        return RedirectResponse("/admin/login?error=config_error", status_code=302)

    if admin_domain:
        callback_base = f"https://{admin_domain}"
    else:
        callback_base = "http://localhost:8035"

    import urllib.parse

    redirect_uri = urllib.parse.quote(f"{callback_base}/api/auth/wecom/callback", safe="")
    state = generate_state()

    oauth_url = (
        "https://open.weixin.qq.com/connect/oauth2/authorize"
        f"?appid={corp_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope=snsapi_base"
        f"&state={state}"
        f"&agentid={agent_id}"
        f"#wechat_redirect"
    )

    logger.info("wecom_login: auth started")
    return RedirectResponse(oauth_url, status_code=302)


# ---------------------------------------------------------------------------
# WeCom OAuth callback
# ---------------------------------------------------------------------------


@router.get("/api/auth/wecom/callback")
def wecom_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """
    Handle the WeCom OAuth callback.

    Validates state, exchanges code for UserId, upserts admin_users,
    creates admin_sessions, sets HttpOnly cookie, redirects to console.
    """
    logger.info("wecom_callback: received (code not logged)")

    # 1. Validate state (single-use, 5-min TTL)
    if not consume_state(state):
        logger.warning("wecom_callback: invalid or expired state")
        return RedirectResponse("/admin/login?error=invalid_state", status_code=302)

    # 2. Load config from env
    wecom_oauth_settings = get_wecom_oauth_settings()
    corp_id = wecom_oauth_settings.wecom_corp_id.strip()
    oauth_secret = wecom_oauth_settings.wecom_oauth_secret.strip()

    if not corp_id or not oauth_secret:
        logger.error("wecom_callback: WECOM_CORP_ID or WECOM_OAUTH_SECRET not configured")
        return RedirectResponse("/admin/login?error=config_error", status_code=302)

    # 3. Get cached/fresh access_token (never log token value)
    try:
        access_token = get_wecom_token(corp_id, oauth_secret)
    except Exception:
        logger.error("wecom_callback: failed to obtain access_token")
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    # 4. Exchange code for UserId
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo",
                params={"access_token": access_token, "code": code},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"unexpected HTTP status {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("unexpected response shape")
    except Exception as exc:
        logger.error("wecom_callback: getuserinfo request failed: %s", type(exc).__name__)
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    if not strict_int_equals(data.get("errcode", -1), 0):
        logger.warning("wecom_callback: getuserinfo errcode=%s", safe_log_value(data.get("errcode")))
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    raw_user_id = data.get("UserId")
    if not isinstance(raw_user_id, str) or not raw_user_id.strip():
        logger.warning("wecom_callback: no valid UserId in getuserinfo response")
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)
    wecom_user_id = raw_user_id.strip()

    logger.info("wecom_callback: user identity resolved")

    # 5. Verify user is an active internal employee via user/get.
    # This check must fail closed: any exception, non-zero errcode, missing
    # or unexpected response, or a wrong-typed field is treated as
    # "verification failed" and rejects the login — never as "verification
    # not needed, let them in".
    try:
        with httpx.Client(timeout=10.0) as client:
            user_resp = client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/user/get",
                params={"access_token": access_token, "userid": wecom_user_id},
            )
        if user_resp.status_code != 200:
            raise RuntimeError(f"unexpected HTTP status {user_resp.status_code}")
        user_data = user_resp.json()
        if not isinstance(user_data, dict):
            raise RuntimeError("unexpected response shape")
    except Exception as exc:
        logger.error(
            "wecom_callback: user/get request failed (%s), rejecting login",
            type(exc).__name__,
        )
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    if not strict_int_equals(user_data.get("errcode", -1), 0):
        logger.warning(
            "wecom_callback: user/get returned errcode=%s, rejecting login",
            safe_log_value(user_data.get("errcode")),
        )
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    # Cross-check identity: the response must describe the exact userid we
    # asked about (WeCom userids are case-insensitive), not merely *some*
    # active user — otherwise a provider-side mixup could verify the wrong
    # person's employment status against the session we're about to create.
    returned_user_id = user_data.get("userid")
    if (
        not isinstance(returned_user_id, str)
        or not returned_user_id.strip()
        or returned_user_id.strip().lower() != wecom_user_id.lower()
    ):
        logger.warning("wecom_callback: user/get userid missing or mismatched, rejecting login")
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    # status: 1=active, 2=disabled, 4=not-activated, 5=left the enterprise.
    # This is the complete official field set for activation state — there
    # is no separate "enable" field in this response; requiring one made
    # every real active employee fail to log in.
    if not strict_int_equals(user_data.get("status"), 1):
        logger.warning(
            "wecom_callback: user is inactive, disabled, or has left (status=%s)",
            safe_log_value(user_data.get("status")),
        )
        return RedirectResponse("/admin/login?error=user_inactive", status_code=302)

    display_name = user_data.get("name") or wecom_user_id

    # 6-8. Resolve tenant, upsert admin_users, create session row.
    # Wrapped so that any DB failure denies the login (redirect, no cookie
    # set) instead of surfacing an uncaught 500 from a half-completed write.
    try:
        # 6. Resolve tenant from DB via corp_id
        config = (
            db.query(TenantWecomConfig)
            .filter(
                TenantWecomConfig.corp_id == corp_id,
                TenantWecomConfig.is_active.is_(True),
            )
            .first()
        )
        if config is None:
            logger.error("wecom_callback: no active tenant config found for corp")
            return RedirectResponse("/admin/login?error=config_error", status_code=302)

        tenant_id: str = config.tenant_id

        # 7. Upsert admin_users
        now = datetime.now(timezone.utc)
        user = (
            db.query(AdminUser)
            .filter(
                AdminUser.tenant_id == tenant_id,
                AdminUser.wecom_user_id == wecom_user_id,
            )
            .first()
        )
        if user is None:
            user = AdminUser(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                wecom_user_id=wecom_user_id,
                name=display_name,
                last_login_at=now,
            )
            db.add(user)
        else:
            user.last_login_at = now
            user.name = display_name
        db.flush()

        # 8. Create session row
        session_id = str(uuid.uuid4())
        expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
        session = AdminSession(
            id=session_id,
            admin_user_id=user.id,
            tenant_id=tenant_id,
            wecom_user_id=wecom_user_id,
            expires_at=expires_at,
            is_revoked=False,
        )
        db.add(session)
        db.flush()

        # 9. Build the response (cookie included) BEFORE committing. If
        # anything here somehow fails, the except-block below still rolls
        # back — the session row must never persist without a cookie
        # already in hand to send with it. db.commit() must be the LAST
        # statement in this block: nothing that can raise may run between a
        # successful commit and `return response` below, or a committed
        # session could end up with its cookie never actually reaching the
        # client.
        response = RedirectResponse("/admin/conversations", status_code=302)
        response.set_cookie(
            key=SESSION_COOKIE,
            value=session_id,
            httponly=True,
            secure=_is_production(),
            samesite="lax",
            path="/",
            max_age=SESSION_TTL_HOURS * 3600,
        )
        logger.info("wecom_callback: login success, session created (id not logged)")
        db.commit()
    except Exception as exc:
        # Log only the exception type — DBAPI errors often embed bound
        # parameters (e.g. the new session id) in their string repr.
        logger.error("wecom_callback: session creation failed: %s", type(exc).__name__)
        db.rollback()
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    return response


# ---------------------------------------------------------------------------
# /api/auth/me
# ---------------------------------------------------------------------------


@router.get("/api/auth/me")
def auth_me(request: Request, db: Session = Depends(get_db)):
    """
    Return current session metadata. Always returns HTTP 200.
    {"authenticated": false} when no valid session.
    No secrets, tokens, or cookie values in the response.
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return JSONResponse({"authenticated": False})

    now = datetime.now(timezone.utc)
    session = (
        db.query(AdminSession)
        .filter(
            AdminSession.id == session_id,
            AdminSession.expires_at > now,
            AdminSession.is_revoked.is_(False),
        )
        .first()
    )
    if session is None:
        return JSONResponse({"authenticated": False})

    user = db.query(AdminUser).filter(AdminUser.id == session.admin_user_id).first()
    if user is None:
        return JSONResponse({"authenticated": False})

    # For password-mode users the wecom_user_id is an internal sentinel;
    # return None rather than exposing the implementation detail to the frontend.
    is_password_user = user.wecom_user_id.startswith(PASSWORD_MODE_WECOM_PREFIX)
    exposed_wecom_id = None if is_password_user else user.wecom_user_id

    return JSONResponse(
        {
            "authenticated": True,
            "wecom_user_id": exposed_wecom_id,
            "display_name": user.name or user.wecom_user_id,
            "tenant_id": session.tenant_id,
            "role": None,
        }
    )


# ---------------------------------------------------------------------------
# /api/auth/logout
# ---------------------------------------------------------------------------


@router.post("/api/auth/logout")
def auth_logout(
    request: Request,
    db: Session = Depends(get_db),
):
    """Revoke the current session and clear the session cookie."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        session = (
            db.query(AdminSession)
            .filter(AdminSession.id == session_id)
            .first()
        )
        if session:
            session.is_revoked = True
            db.commit()
            logger.info("wecom_logout: session revoked")

    response = JSONResponse({"logged_out": True})
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return response
