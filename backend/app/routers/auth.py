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
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import hmac as _hmac

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
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
    verify_password,
)
from app.db.models import AdminSession, AdminUser, Tenant, TenantWecomConfig
from app.db.session import get_db
from app.i18n_assets import I18N_SCRIPT_TAG

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

_PAGE_STYLE = """\
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#f0f2f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:8px;padding:2.5rem 2rem;width:100%;max-width:360px;box-shadow:0 2px 12px rgba(0,0,0,.09);text-align:center}
h1{font-size:1.15rem;color:#111;margin-bottom:.35rem;font-weight:700}
.sub{font-size:.82rem;color:#999;margin-bottom:2rem}
.btn-wecom{display:inline-flex;align-items:center;gap:.55rem;padding:.7rem 1.6rem;background:#07c160;color:#fff;border:none;border-radius:5px;font-size:.95rem;cursor:pointer;text-decoration:none;font-weight:600;letter-spacing:.01em}
.btn-wecom:hover{background:#06ad56}
.error{margin-top:1.2rem;padding:.55rem .75rem;background:#fff2f0;color:#cf1322;border:1px solid #ffccc7;border-radius:4px;font-size:.82rem;text-align:left}
.footer{margin-top:2rem;font-size:.75rem;color:#ccc}
.pwd-form{display:flex;flex-direction:column;gap:.7rem;margin-bottom:.5rem}
.pwd-form input{border:1px solid #d9d9d9;border-radius:4px;padding:.55rem .75rem;font-size:.92rem;outline:none;width:100%}
.pwd-form input:focus{border-color:#1890ff;box-shadow:0 0 0 2px rgba(24,144,255,.1)}
.btn-login{padding:.65rem 0;background:#1890ff;color:#fff;border:none;border-radius:4px;font-size:.95rem;font-weight:600;cursor:pointer;width:100%}
.btn-login:hover{background:#096dd9}
.btn-login:disabled{background:#91caff;cursor:not-allowed}
.lang-switch{position:fixed;top:1rem;right:1rem}
.btn-lang{background:#fff;border:1px solid #d9d9d9;border-radius:4px;padding:.3rem .65rem;font-size:.8rem;cursor:pointer;color:#555}
.btn-lang:hover{border-color:#1890ff;color:#1890ff}
.lang-menu{position:absolute;top:110%;right:0;background:#fff;border:1px solid #e8e8e8;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.12);min-width:7rem;overflow:hidden;z-index:20}
.lang-option{padding:.45rem .75rem;font-size:.83rem;color:#333;cursor:pointer;white-space:nowrap}
.lang-option:hover{background:#f5f5f5}
.lang-option.active{color:#1890ff;font-weight:600;background:#e6f4ff}
"""

# Shared client-side bootstrap: renders the language dropdown from
# I18N.availableLocales() (never a hardcoded list) and applies translations
# to every [data-i18n] / [data-i18n-placeholder] element. Both login modes
# (password/WeCom) reuse this verbatim.
_I18N_BOOTSTRAP_JS = """\
<script>
function renderLangMenu(){
  var menu=document.getElementById('lang-menu');
  if(!menu)return;
  var current=I18N.getLocale();
  var html='';
  I18N.availableLocales().forEach(function(loc){
    var cls='lang-option'+(loc.code===current?' active':'');
    html+='<div class="'+cls+'" onclick="selectLocale(&quot;'+loc.code+'&quot;)">'+loc.nativeName+'</div>';
  });
  menu.innerHTML=html;
}
function toggleLangMenu(){
  var menu=document.getElementById('lang-menu');
  if(!menu)return;
  if(menu.style.display==='block'){menu.style.display='none';return;}
  renderLangMenu();
  menu.style.display='block';
}
function selectLocale(code){
  I18N.setLocale(code);
  var menu=document.getElementById('lang-menu');
  if(menu)menu.style.display='none';
  applyI18n();
}
function applyI18n(){
  document.documentElement.lang=I18N.getLocale();
  var nodes=document.querySelectorAll('[data-i18n]');
  for(var i=0;i<nodes.length;i++){
    nodes[i].textContent=I18N.t(nodes[i].getAttribute('data-i18n'));
  }
  var placeholders=document.querySelectorAll('[data-i18n-placeholder]');
  for(var j=0;j<placeholders.length;j++){
    placeholders[j].setAttribute('placeholder',I18N.t(placeholders[j].getAttribute('data-i18n-placeholder')));
  }
}
document.addEventListener('click',function(e){
  var sw=document.getElementById('lang-switch');
  var menu=document.getElementById('lang-menu');
  if(sw&&menu&&!sw.contains(e.target))menu.style.display='none';
});
applyI18n();
</script>"""

_LANG_SWITCH_HTML = """\
<div class="lang-switch" id="lang-switch">
  <button class="btn-lang" id="btn-lang-toggle" type="button" onclick="toggleLangMenu()" data-i18n="nav.language">语言</button>
  <div class="lang-menu" id="lang-menu" style="display:none"></div>
</div>"""


def _login_page(mode: str = "wecom", error: Optional[str] = None) -> str:
    error_html = ""
    if error:
        key, fallback_text = _ERROR_MESSAGES.get(
            error, _ERROR_MESSAGES["auth_failed"]
        )
        error_html = f'<div class="error" id="login-error" data-i18n="{key}">{fallback_text}</div>'

    if mode == "password":
        login_body = f"""\
  <form class="pwd-form" id="pwd-form" onsubmit="doLogin(event)">
    <input type="text" id="uname" name="username" data-i18n-placeholder="login.username" placeholder="用户名"
           autocomplete="username" required>
    <input type="password" id="pwd" name="password" data-i18n-placeholder="login.password" placeholder="密码"
           autocomplete="current-password" required>
    <button class="btn-login" type="submit" id="submit-btn" data-i18n="login.submit">登录</button>
  </form>
  {error_html}
  <div class="footer" data-i18n="login.footerPassword">临时管理员登录 — 企业微信登录即将上线</div>
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
      if(!el){{el=document.createElement('div');el.id='login-error';el.className='error';document.getElementById('pwd-form').after(el);}}
      el.textContent=I18N.t('login.invalidCredentials');
      el.style.display='block';
      btn.disabled=false;btn.textContent=I18N.t('login.submit');
    }});
  }}).catch(function(){{
    var el=document.getElementById('login-error');
    if(!el){{el=document.createElement('div');el.id='login-error';el.className='error';document.getElementById('pwd-form').after(el);}}
    el.textContent=I18N.t('login.genericFailure');
    el.style.display='block';
    btn.disabled=false;btn.textContent=I18N.t('login.submit');
  }});
}}
</script>"""
    else:
        login_body = f"""\
  <a href="/api/auth/wecom/login" class="btn-wecom">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="white" xmlns="http://www.w3.org/2000/svg">
      <path d="M9.5 7C7.57 7 6 8.34 6 10c0 .99.58 1.87 1.47 2.44-.03.08-.05.16-.05.25 0 .27.22.5.5.5s.5-.23.5-.5c0-.12-.05-.22-.12-.31C9.02 12.2 9.25 12 9.5 12c.39 0 .71-.25.82-.6.05-.01.11-.02.16-.02.97 0 1.75-.67 1.75-1.5S10.45 8.38 9.5 8.38 7.75 9.05 7.75 9.88c0 .31.11.59.28.83"/>
      <path d="M15.5 9c-1.38 0-2.5.9-2.5 2 0 .6.32 1.14.83 1.51a.35.35 0 00-.08.23c0 .2.15.37.33.37.19 0 .33-.16.33-.37 0-.09-.03-.17-.08-.23.22.1.47.15.75.15.34 0 .65-.08.92-.22.05.03.09.05.15.05.45 0 .81-.4.81-.89 0-.18-.06-.35-.15-.49C16.12 11.32 16.28 11.19 16.5 11c.55-.27.94-.78.94-1.38C17.44 8.73 16.59 8 15.5 8c-.78 0-1.47.35-1.87.88"/>
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 14.5H9V9h2v7.5zm4 0h-2V9h2v7.5z" fill="white" opacity="0"/>
    </svg>
    <span data-i18n="login.wecomButton">使用企业微信登录</span>
  </a>
  {error_html}
  <div class="footer" data-i18n="login.footerWecom">仅限企业内部员工访问</div>"""

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — 365 WeCom Archive</title>
<style>
{_PAGE_STYLE}
</style>
</head>
<body>
{I18N_SCRIPT_TAG}
{_LANG_SWITCH_HTML}
<div class="card">
  <h1>365 WeCom Archive</h1>
  <p class="sub" data-i18n="app.subtitle">对话审阅控制台</p>
  {login_body}
</div>
{_I18N_BOOTSTRAP_JS}
</body>
</html>"""


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(
    request: Request,
    error: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Login page. Redirects to /admin/conversations if already authenticated.

    Renders password form when AUTH_MODE=password; WeCom button otherwise.
    """
    session_id = request.cookies.get(SESSION_COOKIE) if request else None
    if session_id:
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

    admin_username = os.getenv("ADMIN_USERNAME", "").strip()
    admin_hash = os.getenv("ADMIN_PASSWORD_HASH", "").strip()

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
    corp_id = os.getenv("WECOM_CORP_ID", "").strip()
    agent_id = os.getenv("WECOM_AGENT_ID", "").strip()
    admin_domain = os.getenv("ADMIN_DOMAIN", "").strip()

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
    corp_id = os.getenv("WECOM_CORP_ID", "").strip()
    oauth_secret = os.getenv("WECOM_OAUTH_SECRET", "").strip()

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
        data = resp.json()
    except Exception as exc:
        logger.error("wecom_callback: getuserinfo request failed: %s", type(exc).__name__)
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    if data.get("errcode", -1) != 0:
        logger.warning("wecom_callback: getuserinfo errcode=%s", data.get("errcode"))
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    wecom_user_id: str = data.get("UserId", "")
    if not wecom_user_id:
        logger.warning("wecom_callback: no UserId in getuserinfo response")
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    logger.info("wecom_callback: user identity resolved")

    # 5. Verify user is an active internal employee via user/get
    display_name = wecom_user_id
    try:
        with httpx.Client(timeout=10.0) as client:
            user_resp = client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/user/get",
                params={"access_token": access_token, "userid": wecom_user_id},
            )
        user_data = user_resp.json()
    except Exception as exc:
        logger.warning(
            "wecom_callback: user/get failed (%s), continuing without verification",
            type(exc).__name__,
        )
        user_data = {}

    if user_data.get("errcode", 0) == 0:
        # status=1 → active, enable=1 → not disabled
        if user_data.get("status") != 1 or user_data.get("enable") != 1:
            logger.warning("wecom_callback: user is inactive or disabled")
            return RedirectResponse("/admin/login?error=user_inactive", status_code=302)
        display_name = user_data.get("name") or wecom_user_id

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
    db.commit()

    logger.info("wecom_callback: login success, session created (id not logged)")

    # 9. Set cookie and redirect to console (hardcoded, never user-controlled)
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
