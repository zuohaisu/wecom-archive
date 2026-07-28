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
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import hmac as _hmac

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import (
    PASSWORD_MODE_WECOM_PREFIX,
    SESSION_COOKIE,
    _is_production,
    consume_state,
    generate_state,
    get_auth_mode,
    get_current_user,
    get_session_ttl_hours,
    get_wecom_token,
    safe_log_value,
    strict_int_equals,
    verify_password,
)
from app.db.models import AdminSession, AdminUser, Tenant, TenantWecomConfig
from app.db.session import get_db
from app.i18n_assets import I18N_SCRIPT_TAG
from app.session_lifecycle import cleanup_expired_sessions
from app.settings import (
    get_auth_settings,
    get_email_settings,
    get_wecom_oauth_settings,
)
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
    `app.subtitle`, shared with review_console.html); every other
    `login.*` key predates this restyle."""
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
    <a class="btn-link" href="/admin/forgot-password" data-i18n="login.forgotPassword">忘记密码</a>
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
  <section class="mt-4" aria-labelledby="wecom-qr-title">
    <h2 class="field-label" id="wecom-qr-title" data-i18n="login.qrTitle">扫码登录</h2>
    <p class="field-help" data-i18n="login.qrScanHint">打开企业微信，扫一扫登录</p>
    <iframe title="企业微信扫码登录" src="/api/auth/wecom/qr/login" style="width:300px;height:400px;border:0" loading="lazy"></iframe>
  </section>
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
# Password reset (RND-278 / F0-3)
# ---------------------------------------------------------------------------


class _ForgotBody(BaseModel):
    email: str


class _ResetBody(BaseModel):
    token: str
    password: str


class _InviteBody(BaseModel):
    wecom_user_id: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    role: str


class _AcceptBody(BaseModel):
    token: str
    password: str
    name: Optional[str] = None


def _password_reset_ttl_hours() -> int:
    """Read a valid reset TTL, falling back to the safe one-hour default."""
    try:
        ttl = int(get_email_settings().reset_token_ttl_hours or "1")
    except ValueError:
        return 1
    return ttl if ttl > 0 else 1


@router.post("/api/auth/password/forgot")
def password_forgot(body: _ForgotBody, db: Session = Depends(get_db)):
    """Request a password reset without revealing whether an account exists."""
    from app.auth import create_password_reset_token
    from app.email import send_password_reset_email

    submitted = (body.email or "").strip().lower()
    user = (
        db.query(AdminUser)
        .join(Tenant, Tenant.id == AdminUser.tenant_id)
        .filter(
            Tenant.slug == "default",
            Tenant.is_active.is_(True),
            func.lower(AdminUser.email) == submitted,
            AdminUser.status == "active",
            AdminUser.password_hash.isnot(None),
        )
        .first()
    )
    if user is not None and user.email:
        raw_token = create_password_reset_token(db, user, _password_reset_ttl_hours())
        email_settings = get_email_settings()
        base_url = (
            email_settings.reset_base_url or get_wecom_oauth_settings().admin_domain
        )
        reset_link = f"{base_url.rstrip('/')}/admin/reset-password?token={raw_token}"
        db.commit()
        send_password_reset_email(user.email, reset_link)
    else:
        db.rollback()
    return JSONResponse({"ok": True})


@router.post("/api/auth/password/reset")
def password_reset(body: _ResetBody, db: Session = Depends(get_db)):
    """Consume a one-time token and set the account's new PBKDF2 password."""
    from app.auth import consume_password_reset_token, hash_password

    result = consume_password_reset_token(db, body.token)
    if result is None:
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="weak_password")
    user, token_row = result
    user.password_hash = hash_password(body.password)
    token_row.used = True
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/api/admin/users/invite")
def invite_user(
    body: _InviteBody,
    current: Tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a disabled pending account and deliver its invitation link."""
    if body.role not in {"owner", "admin", "compliance", "legal", "readonlyaudit"}:
        raise HTTPException(status_code=400, detail="invalid_role")

    admin_user, tenant_id = current
    wecom_user_id = (body.wecom_user_id or "").strip()
    if not wecom_user_id:
        if not body.email:
            raise HTTPException(status_code=400, detail="wecom_user_id_or_email_required")
        wecom_user_id = f"invited:{body.email.lower()}"

    existing = (
        db.query(AdminUser)
        .filter(
            AdminUser.tenant_id == tenant_id,
            AdminUser.wecom_user_id == wecom_user_id,
            AdminUser.invite_status == "pending",
        )
        .first()
    )
    if existing is not None:
        raw_token = existing.invite_token
    else:
        raw_token = secrets.token_urlsafe(32)
        db.add(
            AdminUser(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                wecom_user_id=wecom_user_id,
                email=(body.email or "").strip() or None,
                name=body.name,
                role=body.role,
                status="disabled",
                invite_status="pending",
                invite_token=raw_token,
                invited_by=admin_user.id,
            )
        )
    db.commit()

    from app.email import send_invite_email

    settings = get_email_settings()
    base = settings.invite_base_url or get_wecom_oauth_settings().admin_domain
    accept_link = f"{base.rstrip('/')}/admin/accept-invite?token={raw_token}"
    if body.email:
        send_invite_email(body.email, accept_link)
    return JSONResponse({"ok": True})


@router.post("/api/admin/users/accept")
def accept_invite(body: _AcceptBody, db: Session = Depends(get_db)):
    """Activate a pending invited account after setting its password."""
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="weak_password")
    user = (
        db.query(AdminUser)
        .filter(
            AdminUser.invite_token == body.token,
            AdminUser.invite_status == "pending",
        )
        .first()
    )
    if user is None:
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")

    from app.auth import hash_password

    user.password_hash = hash_password(body.password)
    user.status = "active"
    user.invite_status = "accepted"
    if body.name:
        user.name = body.name
    db.commit()
    return JSONResponse({"ok": True})


@router.get("/admin/forgot-password", response_class=HTMLResponse)
def forgot_password_page():
    return HTMLResponse(
        content=render_template(
            "forgot_password", i18n_script=I18N_SCRIPT_TAG, login_body=_forgot_page_body()
        )
    )


@router.get("/admin/reset-password", response_class=HTMLResponse)
def reset_password_page():
    return HTMLResponse(
        content=render_template(
            "reset_password", i18n_script=I18N_SCRIPT_TAG, login_body=_reset_page_body()
        )
    )


def _forgot_page_body() -> str:
    return """\
  <form id="forgot-form" onsubmit="doForgot(event)">
    <div class="field">
      <label class="field-label" for="email" data-i18n="login.forgotEmail">邮箱</label>
      <input class="input" type="email" id="email" autocomplete="email" required data-i18n-placeholder="login.forgotEmailPlaceholder" placeholder="请输入您的注册邮箱">
    </div>
    <button class="btn btn-primary btn-block btn-lg" type="submit" id="forgot-submit" data-i18n="login.forgotSubmit">发送重置邮件</button>
  </form>
  <div class="alert mt-4" id="forgot-success" style="display:none" role="status"><div data-i18n="login.forgotEmailSent">若该邮箱已注册，重置链接已发送，请查收邮件。</div></div>
  <p class="field-help mt-4" style="text-align:center"><a class="btn-link" href="/admin/login" data-i18n="login.forgotBackToLogin">返回登录</a></p>
<script>
function doForgot(e){
  e.preventDefault();
  var btn=document.getElementById('forgot-submit');
  btn.disabled=true;
  fetch('/api/auth/password/forgot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value})})
  .then(function(){document.getElementById('forgot-success').style.display='';btn.disabled=false;})
  .catch(function(){document.getElementById('forgot-success').style.display='';btn.disabled=false;});
}
</script>"""


def _reset_page_body() -> str:
    return """\
  <form id="reset-form" onsubmit="doReset(event)">
    <input type="hidden" id="reset-token">
    <div class="field"><label class="field-label" for="new-password" data-i18n="login.resetNewPassword">新密码</label><input class="input" type="password" id="new-password" autocomplete="new-password" required></div>
    <div class="field"><label class="field-label" for="confirm-password" data-i18n="login.resetConfirmPassword">确认新密码</label><input class="input" type="password" id="confirm-password" autocomplete="new-password" required></div>
    <button class="btn btn-primary btn-block btn-lg" type="submit" id="reset-submit" data-i18n="login.resetSubmit">重置密码</button>
  </form>
  <div class="alert alert-danger mt-4" id="reset-error" style="display:none" role="alert"><div></div></div>
  <div class="alert mt-4" id="reset-success" style="display:none" role="status"><div data-i18n="login.resetSuccess">密码已重置，请使用新密码登录。</div></div>
  <p class="field-help mt-4" style="text-align:center"><a class="btn-link" href="/admin/login" data-i18n="login.resetBackToLogin">返回登录</a></p>
<script>
document.getElementById('reset-token').value=new URLSearchParams(window.location.search).get('token')||'';
function resetError(key){var el=document.getElementById('reset-error');el.style.display='';el.querySelector('div').textContent=I18N.t(key);}
function doReset(e){
  e.preventDefault();
  var password=document.getElementById('new-password').value;
  if(password.length<8){resetError('login.resetWeakPassword');return;}
  if(password!==document.getElementById('confirm-password').value){resetError('login.resetWeakPassword');return;}
  var btn=document.getElementById('reset-submit');btn.disabled=true;
  fetch('/api/auth/password/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.getElementById('reset-token').value,password:password})})
  .then(function(r){if(r.ok){document.getElementById('reset-success').style.display='';return;}return r.json().then(function(d){resetError(d.detail==='invalid_or_expired_token'?'login.resetInvalidToken':'login.resetWeakPassword');});})
  .catch(function(){resetError('login.resetInvalidToken');})
  .then(function(){btn.disabled=false;});
}
</script>"""


# ---------------------------------------------------------------------------
# Password login (RND-112 — AUTH_MODE=password only)
# ---------------------------------------------------------------------------


class _PasswordLoginBody(BaseModel):
    username: str
    password: str


def _upsert_env_admin_user(
    db: Session,
    tenant: Tenant,
    admin_username: str,
    now: datetime,
) -> AdminUser:
    """Create or update the legacy env-configured password account."""
    tenant_id: str = tenant.id
    wecom_sentinel = f"{PASSWORD_MODE_WECOM_PREFIX}{admin_username}__"
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
    return user


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

    now = datetime.now(timezone.utc)
    submitted = body.username.strip()
    normalized_email = submitted.lower()

    # F0-2 per-user path: match active users by normalized email in the
    # default tenant only. Inactive and absent users intentionally follow
    # the same failure path to avoid an account-enumeration oracle.
    candidate = (
        db.query(AdminUser)
        .filter(
            AdminUser.tenant_id == tenant.id,
            func.lower(AdminUser.email) == normalized_email,
            AdminUser.status == "active",
        )
        .first()
    )

    password_ok = False
    resolved_user = None
    if candidate is not None and candidate.password_hash:
        password_ok = verify_password(body.password, candidate.password_hash)
        if password_ok:
            resolved_user = candidate

    # Legacy env credentials remain available to bootstrap the first account.
    # These checks always run after an unsuccessful per-user verification, so
    # every failed request performs PBKDF2 verification regardless of whether
    # the submitted email matched an active account.
    if not password_ok:
        username_ok = _hmac.compare_digest(submitted, admin_username)
        env_ok = verify_password(body.password, admin_hash)
        if username_ok and env_ok:
            password_ok = True
            resolved_user = _upsert_env_admin_user(db, tenant, admin_username, now)

    if not password_ok or resolved_user is None:
        logger.warning("password_login: failed (credentials not logged)")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    resolved_user.last_login_at = now
    db.flush()

    # Create session row.
    session_id = str(uuid.uuid4())
    session_ttl_hours = get_session_ttl_hours()
    expires_at = now + timedelta(hours=session_ttl_hours)
    session = AdminSession(
        id=session_id,
        admin_user_id=resolved_user.id,
        tenant_id=resolved_user.tenant_id,
        wecom_user_id=resolved_user.wecom_user_id,
        expires_at=expires_at,
        is_revoked=False,
    )
    db.add(session)
    write_audit(
        db,
        tenant_id=resolved_user.tenant_id,
        admin_user_id=resolved_user.id,
        action=AuditAction.LOGIN,
        object_type=AuditObjectType.USER,
        object_id=resolved_user.id,
        detail={"mode": "password"},
    )
    db.commit()
    cleanup_expired_sessions(db)

    logger.info("password_login: success, session created (id not logged)")

    response = JSONResponse({"logged_in": True})
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        httponly=True,
        secure=_is_production(),
        samesite="lax",
        path="/",
        max_age=session_ttl_hours * 3600,
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


@router.get("/api/auth/wecom/qr/login")
def wecom_qr_login():
    """Redirect an iframe to WeCom's PC QR-login page (RND-321)."""
    wecom_oauth_settings = get_wecom_oauth_settings()
    corp_id = wecom_oauth_settings.wecom_corp_id.strip()
    agent_id = wecom_oauth_settings.wecom_agent_id.strip()
    admin_domain = wecom_oauth_settings.admin_domain.strip()

    if not corp_id or not agent_id:
        logger.error("wecom_qr_login: WECOM_CORP_ID or WECOM_AGENT_ID not configured")
        return RedirectResponse("/admin/login?error=config_error", status_code=302)

    callback_base = f"https://{admin_domain}" if admin_domain else "http://localhost:8035"
    import urllib.parse

    redirect_uri = urllib.parse.quote(
        f"{callback_base}/api/auth/wecom/qr/callback", safe=""
    )
    state = generate_state()
    qr_connect_url = (
        "https://open.work.weixin.qq.com/wwopen/sso/qrConnect"
        f"?appid={corp_id}"
        f"&agentid={agent_id}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
        "&self_redirect=true"
    )
    logger.info("wecom_qr_login: auth started")
    return RedirectResponse(qr_connect_url, status_code=302)


# ---------------------------------------------------------------------------
# WeCom OAuth and QR callback
# ---------------------------------------------------------------------------


def _resolve_and_sign_wecom_session(code: str, db: Session) -> RedirectResponse:
    """Exchange a validated callback code and issue the tenant-bound session.

    This is deliberately shared by the in-WeCom OAuth and PC QR flows so their
    employee verification, user upsert, session lifetime, and cookie flags
    cannot drift apart.
    """
    logger.info("wecom_callback: received (code not logged)")

    # Load config from env
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
        session_ttl_hours = get_session_ttl_hours()
        expires_at = now + timedelta(hours=session_ttl_hours)
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
        write_audit(
            db,
            tenant_id=tenant_id,
            admin_user_id=user.id,
            action=AuditAction.LOGIN,
            object_type=AuditObjectType.USER,
            object_id=user.id,
            detail={"corp_id": corp_id, "method": "wecom_oauth"},
        )

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
            max_age=session_ttl_hours * 3600,
        )
        logger.info("wecom_callback: login success, session created (id not logged)")
        db.commit()
        cleanup_expired_sessions(db)
    except Exception as exc:
        # Log only the exception type — DBAPI errors often embed bound
        # parameters (e.g. the new session id) in their string repr.
        logger.error("wecom_callback: session creation failed: %s", type(exc).__name__)
        db.rollback()
        return RedirectResponse("/admin/login?error=auth_failed", status_code=302)

    return response


@router.get("/api/auth/wecom/callback")
def wecom_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """Handle the in-WeCom OAuth callback after one-time state validation."""
    if not consume_state(state):
        logger.warning("wecom_callback: invalid or expired state")
        return RedirectResponse("/admin/login?error=invalid_state", status_code=302)
    return _resolve_and_sign_wecom_session(code, db)


@router.get("/api/auth/wecom/qr/callback")
def wecom_qr_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """Handle the PC QR callback using the identical OAuth session flow."""
    if not consume_state(state):
        logger.warning("wecom_qr_callback: invalid or expired state")
        return RedirectResponse("/admin/login?error=invalid_state", status_code=302)
    return _resolve_and_sign_wecom_session(code, db)


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
            "id": user.id,
            "role": user.role,
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
            write_audit(
                db,
                tenant_id=session.tenant_id,
                admin_user_id=session.admin_user_id,
                action=AuditAction.LOGOUT,
                object_type=AuditObjectType.SESSION,
                object_id=session_id,
            )
            db.commit()
            logger.info("wecom_logout: session revoked")

    response = JSONResponse({"logged_out": True})
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return response
