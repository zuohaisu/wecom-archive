"""
Auth utilities for RND-110 (WeCom OAuth) and RND-112 (password fallback).

Public surface:
  get_auth_mode       Returns AUTH_MODE env var ('wecom' | 'password')
  get_current_user    FastAPI dependency — raises HTTP 401 if no valid session
  generate_state      Generate and store an OAuth CSRF state token (5-min TTL, single-use)
  consume_state       Validate and remove a state token
  get_wecom_token     Get/cache a WeCom access_token keyed by corp_id
  hash_password       Hash a plaintext password for storage in ADMIN_PASSWORD_HASH
  verify_password     Constant-time verify a submitted password against the stored hash

State store: in-memory dict, single-process (sufficient for single-instance MVP).
Token cache: in-memory dict keyed by corp_id, TTL ~7000 s (WeCom issues 7200 s tokens).

Password hashing (RND-112):
  Algorithm: PBKDF2-HMAC-SHA256, 260 000 iterations, 16-byte random salt.
  Format:    pbkdf2:sha256:<iterations>:<salt_b64>:<hash_b64>
  Never log submitted password, stored hash, or session token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import httpx
from fastapi import Cookie, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.db.models import (
    AdminSession,
    AdminUser,
    PasswordResetToken,
    PlatformAdmin,
    PlatformAdminSession,
    Tenant,
)
from app.db.session import get_db
from app.services.service_access import (
    INTERACTIVE,
    tenant_service_denial,
)
from app.session_lifecycle import touch_last_active
from app.settings import get_auth_settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "session_id"
PLATFORM_SESSION_COOKIE = "platform_session_id"
DEFAULT_SESSION_TTL_HOURS = 8
# Compatibility default for existing imports; new sessions use the configured value.
SESSION_TTL_HOURS = DEFAULT_SESSION_TTL_HOURS
_STATE_TTL_SECONDS = 300  # 5 minutes
_TOKEN_CACHE_TTL = 7000   # seconds (WeCom tokens expire in 7200 s)

# ---------------------------------------------------------------------------
# Auth mode (RND-112)
# ---------------------------------------------------------------------------

_VALID_AUTH_MODES = frozenset({"wecom", "password"})


def strict_int_equals(value: object, expected: int) -> bool:
    """
    True only if value is a real int (not bool — bool is an int subclass in
    Python, so `True == 1` is True) equal to expected. Used everywhere an
    OAuth/API response field (errcode, status) gates a login decision, so a
    provider returning a boolean or other truthy-but-wrong type can't slip
    past an `== 1` / `== 0` comparison.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def safe_log_value(value: object) -> str:
    """
    Render an externally-controlled response field for logging. A plain int
    (never bool) is shown verbatim — provider error/status codes are meant
    to be small integers, and legitimate ones (e.g. errcode=40029) are
    useful for debugging. Anything else — which by construction only
    reaches this function after failing a strict_int_equals gate — is
    rendered as just its type name, never its content: a malformed or
    adversarial response must not be able to inject arbitrary text
    (log forging, control characters) into application logs by putting it
    in a field we happen to log.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return f"<{type(value).__name__}>"

# PBKDF2 parameters — kept as constants so they are easy to audit.
_PBKDF2_HASH = "sha256"
_PBKDF2_ITERATIONS = 260_000
_PBKDF2_SALT_BYTES = 16


def get_session_ttl_hours() -> int:
    """Read ``SESSION_TTL_HOURS``; default to 8 and clamp to [1, 8760]."""
    raw = get_auth_settings().session_ttl_hours.strip()
    if not raw:
        return DEFAULT_SESSION_TTL_HOURS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "SESSION_TTL_HOURS=%r not an int; using default %d",
            raw,
            DEFAULT_SESSION_TTL_HOURS,
        )
        return DEFAULT_SESSION_TTL_HOURS
    return max(1, min(8760, value))


def get_auth_mode() -> str:
    """
    Return the configured auth mode.

    - 'wecom'    → WeCom OAuth flow (RND-110, default).
    - 'password' → Temporary username/password fallback (RND-112).

    If AUTH_MODE is unset or unrecognized, defaults to 'wecom' and logs a warning.
    """
    raw = get_auth_settings().auth_mode.strip().lower()
    if raw in _VALID_AUTH_MODES:
        return raw
    if raw:
        logger.warning("AUTH_MODE=%r not recognized; defaulting to 'wecom'", raw)
    return "wecom"


def hash_password(plain: str) -> str:
    """
    Hash a plaintext password using PBKDF2-HMAC-SHA256.

    Returns a portable string suitable for ADMIN_PASSWORD_HASH.
    Format: pbkdf2:sha256:<iterations>:<salt_b64>:<hash_b64>

    Usage (from backend/):
        python -c "from app.auth import hash_password; print(hash_password('yourpassword'))"
    """
    salt = secrets.token_bytes(_PBKDF2_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_HASH, plain.encode(), salt, _PBKDF2_ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode()
    hash_b64 = base64.b64encode(dk).decode()
    return f"pbkdf2:{_PBKDF2_HASH}:{_PBKDF2_ITERATIONS}:{salt_b64}:{hash_b64}"


def verify_password(plain: str, stored_hash: str) -> bool:
    """
    Constant-time verify a submitted plaintext password against a stored PBKDF2 hash.

    Returns True only when both the hash parses correctly and the digest matches.
    Never logs plain, stored_hash, or any derived value.
    """
    try:
        parts = stored_hash.split(":")
        if len(parts) != 5 or parts[0] != "pbkdf2":
            return False
        _, hash_name, iterations_str, salt_b64, expected_b64 = parts
        iterations = int(iterations_str)
        if iterations <= 0:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(expected_b64)
        dk = hashlib.pbkdf2_hmac(hash_name, plain.encode(), salt, iterations)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# —— RND-306 (B1-1) 平台超管认证原语（HTTP 登录/session 属 B1-2/RND-305）——
def verify_platform_admin(db, email: str, password: str):
    """Return the active PlatformAdmin for correct credentials, else None.

    Reuses hash_password/verify_password (PBKDF2). Does NOT issue sessions
    or cookies — that is B1-2's job. Disabled admins are rejected.
    """
    from app.db.models import PlatformAdmin  # lazy import to avoid cycles

    admin = (
        db.query(PlatformAdmin)
        .filter(PlatformAdmin.email == email.strip().lower())
        .first()
    )
    if admin is None or admin.status != "active":
        return None
    if not verify_password(password, admin.password_hash):
        return None
    return admin


# —— RND-413 (B1-2) 平台超管登录会话 ——
# Browser login issues a platform_session_id cookie backed by
# platform_admin_sessions rows; HTTP Basic remains the API-client path.
PLATFORM_BASIC_REALM = "platform admin"
_WWW_AUTHENTICATE_HEADERS = {"WWW-Authenticate": f'Basic realm="{PLATFORM_BASIC_REALM}"'}


def create_platform_admin_session(
    db: Session, admin: PlatformAdmin, ttl_hours: int | None = None
) -> tuple[str, int]:
    """Create and flush a platform-admin session row; return (cookie id, ttl).

    The caller commits and sets the cookie. TTL follows the same configured
    ``SESSION_TTL_HOURS`` used by tenant-admin sessions. Never log the raw id.
    """
    ttl_hours = ttl_hours or get_session_ttl_hours()
    session_id = str(uuid.uuid4())
    db.add(
        PlatformAdminSession(
            id=session_id,
            platform_admin_id=admin.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
            is_revoked=False,
        )
    )
    db.flush()
    return session_id, ttl_hours


def _platform_admin_from_session(
    session_id: str, db: Session
) -> Optional[PlatformAdmin]:
    """Resolve an active PlatformAdmin from a valid session id, else None.

    Deny-by-default, mirroring get_current_user: a DB failure, unknown or
    expired/revoked row, or a disabled account all resolve to None. Only
    the exception type is logged — DBAPI errors embed bound parameters
    (here, the session token) in their string repr.
    """
    now = datetime.now(timezone.utc)
    try:
        row = (
            db.query(PlatformAdminSession)
            .filter(
                PlatformAdminSession.id == session_id,
                PlatformAdminSession.expires_at > now,
                PlatformAdminSession.is_revoked.is_(False),
            )
            .first()
        )
    except Exception as exc:
        logger.error(
            "_platform_admin_from_session: lookup failed: %s", type(exc).__name__
        )
        return None
    if row is None:
        return None
    admin = db.get(PlatformAdmin, row.platform_admin_id)
    if admin is None or admin.status != "active":
        return None
    return admin


async def require_platform_admin_optional(
    request: Request, db: Session = Depends(get_db)
) -> Optional[PlatformAdmin]:
    """Resolve the platform admin from session cookie, then HTTP Basic.

    Returns None when neither credential is valid. HTML routes use this to
    redirect to /platform/login instead of surfacing the strict 401, while
    ``require_platform_admin`` wraps it for API endpoints.
    """
    session_id = request.cookies.get(PLATFORM_SESSION_COOKIE)
    if session_id:
        admin = _platform_admin_from_session(session_id, db)
        if admin is not None:
            return admin
    credentials = await _platform_admin_basic(request)
    if credentials is not None:
        return verify_platform_admin(db, credentials.username, credentials.password)
    return None


# ---------------------------------------------------------------------------
# RND-305 (B1-2) — platform-admin cross-tenant scope
# ---------------------------------------------------------------------------
# HTTP Basic remains the API-client credential path; the session-cookie
# resolver (RND-413) is tried first by require_platform_admin and
# require_platform_admin_optional above.
_platform_admin_basic = HTTPBasic(auto_error=False)


def _get_platform_admin_credentials(
    credentials: Optional[HTTPBasicCredentials] = Depends(_platform_admin_basic),
) -> Optional[HTTPBasicCredentials]:
    """Resolve temporary platform-admin credentials; replace this for B1 sessions."""
    return credentials


def require_platform_admin(
    session_id: Optional[str] = Cookie(default=None, alias=PLATFORM_SESSION_COOKIE),
    credentials: Optional[HTTPBasicCredentials] = Depends(_get_platform_admin_credentials),
    db: Session = Depends(get_db),
) -> PlatformAdmin:
    """Return an authenticated active PlatformAdmin, or raise HTTP 401.

    Resolution order (RND-413): platform session cookie first, then HTTP
    Basic. Both 401 branches carry ``WWW-Authenticate: Basic`` so the
    credential challenge is never silent — API clients get the native
    browser dialog instead of an opaque 401 JSON body.

    Platform-admin credentials are deliberately independent of tenant admin
    sessions: a ``session_id`` cookie and any tenant role cannot satisfy
    this dependency.
    """
    if session_id:
        admin = _platform_admin_from_session(session_id, db)
        if admin is not None:
            return admin

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Platform admin authentication required",
            headers=_WWW_AUTHENTICATE_HEADERS,
        )

    admin = verify_platform_admin(db, credentials.username, credentials.password)
    if admin is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid platform admin credentials",
            headers=_WWW_AUTHENTICATE_HEADERS,
        )
    return admin


PLATFORM_TENANT_ACCESS_ACTION = AuditAction.PLATFORM_TENANT_ACCESSED
PLATFORM_TENANT_OBJECT_TYPE = AuditObjectType.TENANT


@dataclass(frozen=True)
class PlatformAdminTenantScope:
    """An audited target-tenant scope granted to a platform super-admin."""

    platform_admin: PlatformAdmin
    tenant_id: str


def require_platform_tenant_scope(
    # INTENTIONAL EXCEPTION: tenant APIs must never obtain tenant_id from a
    # request parameter; they must use the tenant_id in their authenticated
    # session. This platform-only dependency is different: its identity has
    # already been proven as a tenant-less PlatformAdmin, and every explicit
    # target tenant access is written through write_audit. Do not copy this
    # pattern into tenant APIs.
    tenant_id: str = Query(..., min_length=1),
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformAdminTenantScope:
    """Resolve explicit ``tenant_id`` and append its mandatory access audit row."""
    write_audit(
        db,
        tenant_id=tenant_id,
        action=PLATFORM_TENANT_ACCESS_ACTION,
        object_type=PLATFORM_TENANT_OBJECT_TYPE,
        object_id=tenant_id,
        # AuditLog.admin_user_id references tenant-scoped admin_users; never
        # put the tenant-less PlatformAdmin id in that foreign-key column.
        admin_user_id=None,
        detail={"platform_admin_id": platform_admin.id},
    )
    # This dependency can be the only write in a read-only platform request.
    # Commit before returning scope so its required access evidence survives
    # request teardown even when no route body performs a later commit.
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error("require_platform_tenant_scope: audit commit failed")
    return PlatformAdminTenantScope(platform_admin=platform_admin, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Password reset tokens (RND-278 / F0-3)
# ---------------------------------------------------------------------------


def create_password_reset_token(db: Session, user: AdminUser, ttl_hours: int = 1) -> str:
    """Create a one-time reset token and return its raw value for email delivery.

    Only the SHA-256 digest is persisted; callers must never log or return the
    raw value outside the reset link sent to the account's email address.
    """
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    db.query(PasswordResetToken).filter(
        PasswordResetToken.admin_user_id == user.id,
        PasswordResetToken.used.is_(False),
    ).update({PasswordResetToken.used: True})
    db.add(
        PasswordResetToken(
            id=str(uuid.uuid4()),
            admin_user_id=user.id,
            tenant_id=user.tenant_id,
            token=token_hash,
            expires_at=now + timedelta(hours=ttl_hours),
            used=False,
        )
    )
    db.flush()
    return raw


def consume_password_reset_token(
    db: Session, raw_token: str
) -> Optional[tuple[AdminUser, PasswordResetToken]]:
    """Return an active user and usable token row, or ``None`` if invalid."""
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    row = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token == token_hash)
        .first()
    )
    if row is None or row.used:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        # SQLite-based offline tests return naive timestamps even for a
        # timezone-aware column; production PostgreSQL returns an aware one.
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        row.used = True
        db.flush()
        return None
    user = db.query(AdminUser).filter(AdminUser.id == row.admin_user_id).first()
    if user is None or user.status != "active":
        return None
    return user, row


# ---------------------------------------------------------------------------
# CSRF OAuth state store
# ---------------------------------------------------------------------------

_state_store: dict[str, float] = {}
_state_lock = threading.Lock()


def generate_state() -> str:
    """Return a 32-byte URL-safe random state token stored with 5-min TTL."""
    token = secrets.token_urlsafe(32)
    expiry = time.monotonic() + _STATE_TTL_SECONDS
    with _state_lock:
        _state_store[token] = expiry
        # Evict expired entries opportunistically to prevent unbounded growth.
        now = time.monotonic()
        expired = [k for k, v in _state_store.items() if v < now]
        for k in expired:
            del _state_store[k]
    return token


def consume_state(token: str) -> bool:
    """Validate and atomically remove a state token. Returns True if valid."""
    with _state_lock:
        expiry = _state_store.pop(token, None)
    if expiry is None:
        return False
    return time.monotonic() <= expiry


# ---------------------------------------------------------------------------
# WeCom access_token cache
# ---------------------------------------------------------------------------

_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


def get_wecom_token(corp_id: str, oauth_secret: str, cache_key: Optional[str] = None) -> str:
    """
    Return a cached or freshly-fetched WeCom access_token for the given corp_id.

    Cache key defaults to corp_id but can be overridden via cache_key — this
    lets callers hold two independent tokens for the same corp_id when a
    second app secret is used (e.g. an external-contact secret alongside the
    main agent secret), without colliding in the shared token cache.
    Never logs token value, code, or secret.
    """
    key = cache_key or corp_id
    now = time.monotonic()
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and now < cached[1]:
            return cached[0]

    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params={"corpid": corp_id, "corpsecret": oauth_secret})
        if resp.status_code != 200:
            raise RuntimeError(f"unexpected HTTP status {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict):
            raise TypeError("unexpected response shape")
    except Exception as exc:
        logger.error("WeCom gettoken request failed: %s", type(exc).__name__)
        raise RuntimeError("Failed to fetch WeCom access_token") from exc

    if not strict_int_equals(data.get("errcode", -1), 0):
        safe_errcode = safe_log_value(data.get("errcode"))
        logger.error("WeCom gettoken returned error: errcode=%s", safe_errcode)
        raise RuntimeError(f"WeCom gettoken failed: errcode={safe_errcode}")

    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        logger.error("WeCom gettoken response missing access_token")
        raise RuntimeError("WeCom gettoken response missing access_token")
    expires_in = int(data.get("expires_in", 7200))
    cached_until = now + min(expires_in - 200, _TOKEN_CACHE_TTL)

    with _token_lock:
        _token_cache[key] = (token, cached_until)

    logger.info("WeCom access_token refreshed for corp (value not logged)")
    return token


# ---------------------------------------------------------------------------
# FastAPI session dependency
# ---------------------------------------------------------------------------


def get_current_user(
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> Tuple[AdminUser, str]:
    """
    FastAPI dependency.  Returns (AdminUser, tenant_id) for a valid, non-expired,
    non-revoked session cookie.  Raises HTTP 401 otherwise.

    The returned tenant_id is the sole authorization scope for all admin queries.
    Never accept tenant_id from user-supplied request params.
    """
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    now = datetime.now(timezone.utc)
    try:
        session = (
            db.query(AdminSession)
            .filter(
                AdminSession.id == session_id,
                AdminSession.expires_at > now,
                AdminSession.is_revoked.is_(False),
                AdminSession.session_scope == "admin",
            )
            .first()
        )
    except Exception as exc:
        # A DB failure means identity cannot be confirmed — deny, never
        # fall back to a cached/anonymous/default identity. Log only the
        # exception type: DBAPI errors often embed bound parameters (here,
        # the session token) in their string repr, so exc_info/str(exc)
        # must never be logged on this path.
        logger.error("get_current_user: session lookup failed: %s", type(exc).__name__)
        raise HTTPException(status_code=401, detail="Not authenticated")

    if session is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

    try:
        user = db.query(AdminUser).filter(AdminUser.id == session.admin_user_id).first()
    except Exception as exc:
        logger.error("get_current_user: user lookup failed: %s", type(exc).__name__)
        raise HTTPException(status_code=401, detail="Not authenticated")

    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    # RND-321 QA-001 round 2: an unexpired, unrevoked session row only
    # proves the login was once valid — not that this app still authorizes
    # the account today. Re-checking status here (not just at session
    # creation) is what makes disabling someone take effect on their very
    # next request, for every route that depends on get_current_user, not
    # only the WeCom/QR login path that created the session.
    if user.status != "active":
        raise HTTPException(status_code=401, detail="Not authenticated")

    # RND-402: the tenant's authoritative service projection is re-checked
    # on every request, exactly like the account status above. A frozen or
    # suspended tenant loses admin-console business access immediately,
    # even with a still-valid session; grace is already folded into the
    # "active" projection by the billing lifecycle service, so no time
    # logic is needed here. Owner billing stays reachable through the
    # separate get_billing_context dependency.
    try:
        tenant = db.get(Tenant, session.tenant_id)
    except Exception as exc:
        logger.error("get_current_user: tenant lookup failed: %s", type(exc).__name__)
        raise HTTPException(status_code=401, detail="Not authenticated")
    denial = tenant_service_denial(
        tenant.lifecycle_status if tenant is not None else None, INTERACTIVE
    )
    if denial is not None:
        raise HTTPException(status_code=403, detail=denial)

    if isinstance(user, AdminUser):
        touch_last_active(user, db)
    return user, session.tenant_id


# ---------------------------------------------------------------------------
# RND-280 (F0-5) — RBAC role vocabulary + require_role scaffold
# ---------------------------------------------------------------------------
# `AdminUser.role` is a plain `str` (the ORM `Enum` for `admin_user_role`
# stores/returns Python strings), so RBAC checks compare against this tuple.
ADMIN_ROLES: tuple[str, ...] = ("owner", "admin", "compliance", "legal", "readonlyaudit")


def require_role(*allowed_roles: str):
    """FastAPI dependency factory (RND-280 / F0-5 RBAC scaffold).

    Wrap with `Depends(require_role("admin", "owner"))` on a route to gate it
    by role. Unauthenticated callers get `get_current_user`'s 401 first; an
    authenticated caller whose `role` is not in `allowed_roles` gets 403.
    Passing no roles allows any authenticated admin role.

    Returns the same `(AdminUser, tenant_id)` tuple as `get_current_user` so
    downstream route signatures are unchanged.
    """
    allowed = set(allowed_roles) if allowed_roles else set(ADMIN_ROLES)

    def _checker(auth: Tuple[AdminUser, str] = Depends(get_current_user)) -> Tuple[AdminUser, str]:
        user, _tenant_id = auth
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="Insufficient role for this operation")
        return auth

    return _checker


def require_html_session(
    request: Request, db: Session = Depends(get_db)
) -> Optional[str]:
    """
    FastAPI dependency for HTML admin routes. Returns tenant_id for the
    authenticated session, or None if unauthenticated. Callers redirect to
    /admin/login on None instead of the 401 that get_current_user raises.
    tenant_id is the authoritative scope for all subsequent archive queries.
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    now = datetime.now(timezone.utc)
    try:
        session = (
            db.query(AdminSession)
            .filter(
                AdminSession.id == session_id,
                AdminSession.expires_at > now,
                AdminSession.is_revoked.is_(False),
                AdminSession.session_scope == "admin",
            )
            .first()
        )
    except Exception as exc:
        # A DB failure means identity cannot be confirmed — treat as
        # unauthenticated (redirect to login), never as authenticated.
        # Log only the exception type — DBAPI errors often embed bound
        # parameters (here, the session token) in their string repr.
        logger.error("require_html_session: session lookup failed: %s", type(exc).__name__)
        return None
    if session is None:
        return None

    user = db.query(AdminUser).filter(AdminUser.id == session.admin_user_id).first()
    # RND-321 QA-001 round 2: same re-check as get_current_user — a
    # suspended account must stop reaching HTML admin pages immediately,
    # not just be blocked from logging in again.
    if user is None or user.status != "active":
        return None

    # RND-402: HTML admin console entry shares the same authoritative
    # interactive gate as the API dependency. A frozen/suspended tenant's
    # business pages redirect to login; the login page then routes a
    # frozen Owner to the billing surface, so there is no redirect loop.
    try:
        tenant = db.get(Tenant, session.tenant_id)
    except Exception as exc:
        logger.error("require_html_session: tenant lookup failed: %s", type(exc).__name__)
        return None
    denial = tenant_service_denial(
        tenant.lifecycle_status if tenant is not None else None, INTERACTIVE
    )
    if denial is not None:
        return None

    touch_last_active(user, db)
    return session.tenant_id


def _is_production() -> bool:
    return get_auth_settings().app_env.strip().lower() == "production"


def get_provisioning_user(
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> Tuple[AdminUser, Tenant]:
    """Authorize only the restricted self-service provisioning surface."""
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    now = datetime.now(timezone.utc)
    session = (
        db.query(AdminSession)
        .filter(
            AdminSession.id == session_id,
            AdminSession.expires_at > now,
            AdminSession.is_revoked.is_(False),
            AdminSession.session_scope == "provisioning",
        )
        .first()
    )
    if session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(AdminUser).filter(AdminUser.id == session.admin_user_id).first()
    tenant = db.query(Tenant).filter(Tenant.id == session.tenant_id).first()
    if (
        user is None
        or user.status != "active"
        or user.role != "owner"
        or tenant is None
        or tenant.lifecycle_status != "provisioning"
    ):
        raise HTTPException(status_code=403, detail="Provisioning access denied")
    return user, tenant


# Roles allowed to create, refresh, or close payment orders (RND-407).
BILLING_MANAGER_ROLES: tuple[str, ...] = ("owner", "admin")


@dataclass(frozen=True)
class BillingAccessContext:
    user_id: str
    tenant_id: str
    lifecycle_status: str
    session_scope: str
    role: str


def get_billing_context(
    *allowed_roles: str, allowed_lifecycle: frozenset[str] | None = None
):
    """FastAPI dependency factory for the billing surface (RND-407).

    Authorizes an active user whose role is in ``allowed_roles`` (default:
    every admin role, i.e. read-only visibility) and whose tenant matches
    the session's tenant with a lifecycle status consistent with the
    session scope. A ``provisioning`` session scope always requires the
    tenant to still be ``provisioning``; an ``admin`` session scope requires
    the tenant's lifecycle status to be a member of ``allowed_lifecycle``
    (default: ``{"active"}``, preserving the pre-RND-404 behavior).
    RND-402: a billing-frozen tenant's Owner keeps the billing / renewal /
    payment-query surface (the recovery path), while a manually
    ``suspended`` tenant is denied — payment must never clear a manual
    suspension; ``get_billing_manager``/``get_billing_owner`` set
    ``allowed_lifecycle`` to ``{"active", "frozen"}`` accordingly.
    Role-gate the payment-write endpoints by depending on the
    ``get_billing_manager`` instance instead.

    RND-404: the Owner billing surface must stay reachable while a tenant
    is billing-``frozen`` (ADR-0005 §2.6 — Owner billing/renewal is the one
    surface still allowed) but never while ``suspended`` (superadmin-only
    recovery). Callers pick the right ``allowed_lifecycle`` set per
    endpoint instead of each router re-deriving this policy locally.

    Returns a :class:`BillingAccessContext`; unauthenticated callers get
    401, authenticated callers outside the role/lifecycle contract get 403.
    """
    allowed = set(allowed_roles) if allowed_roles else set(ADMIN_ROLES)
    admin_lifecycle = allowed_lifecycle or frozenset({"active"})

    def _checker(
        session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
        db: Session = Depends(get_db),
    ) -> BillingAccessContext:
        if not session_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
        now = datetime.now(timezone.utc)
        try:
            session = (
                db.query(AdminSession)
                .filter(
                    AdminSession.id == session_id,
                    AdminSession.expires_at > now,
                    AdminSession.is_revoked.is_(False),
                    AdminSession.session_scope.in_(("admin", "provisioning")),
                )
                .first()
            )
            if session is None:
                raise HTTPException(status_code=401, detail="Not authenticated")
            user = db.query(AdminUser).filter(AdminUser.id == session.admin_user_id).first()
            tenant = db.query(Tenant).filter(Tenant.id == session.tenant_id).first()
            lifecycle_ok = tenant is not None and (
                tenant.lifecycle_status == "provisioning"
                if session.session_scope == "provisioning"
                else tenant.lifecycle_status in admin_lifecycle
            )
            if (
                user is None
                or user.status != "active"
                or user.role not in allowed
                or user.tenant_id != session.tenant_id
                or not lifecycle_ok
            ):
                raise HTTPException(status_code=403, detail="Billing access denied")
            context = BillingAccessContext(
                user_id=user.id,
                tenant_id=tenant.id,
                lifecycle_status=tenant.lifecycle_status,
                session_scope=session.session_scope,
                role=user.role,
            )
            db.rollback()
            return context
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("get_billing_context: lookup failed: %s", type(exc).__name__)
            raise HTTPException(status_code=401, detail="Not authenticated")

    return _checker


# Read-only billing access for every active admin role. RND-404: readable
# through frozen and even suspended so the page can render an accurate,
# non-misleading status instead of an opaque 403.
get_billing_viewer = get_billing_context(
    allowed_lifecycle=frozenset({"active", "frozen", "suspended"})
)
# Payment-authorized billing access for owners and admins. RND-404: frozen
# tenants may still pay to unfreeze; suspended tenants may not self-serve
# any payment action (ADR-0005 §2.6 — superadmin-only recovery).
get_billing_manager = get_billing_context(
    *BILLING_MANAGER_ROLES, allowed_lifecycle=frozenset({"active", "frozen"})
)
# Owner-only billing access for the cancel-at-period-end intent endpoint
# (RND-404): billing_lifecycle.set_cancel_at_period_end already requires an
# active owner AdminUser row, so the role gate here matches that contract.
get_billing_owner = get_billing_context(
    "owner", allowed_lifecycle=frozenset({"active", "frozen"})
)


# Sentinel wecom_user_id prefix used for password-mode AdminUser rows.
PASSWORD_MODE_WECOM_PREFIX = "__pwd__"
