"""
WeCom OAuth auth utilities for RND-110.

Public surface:
  get_current_user    FastAPI dependency — raises HTTP 401 if no valid session
  generate_state      Generate and store an OAuth CSRF state token (5-min TTL, single-use)
  consume_state       Validate and remove a state token
  get_wecom_token     Get/cache a WeCom access_token keyed by corp_id

State store: in-memory dict, single-process (sufficient for single-instance MVP).
Token cache: in-memory dict keyed by corp_id, TTL ~7000 s (WeCom issues 7200 s tokens).
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import httpx
from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import AdminSession, AdminUser
from app.db.session import get_db

logger = logging.getLogger(__name__)

SESSION_COOKIE = "session_id"
SESSION_TTL_HOURS = 8
_STATE_TTL_SECONDS = 300  # 5 minutes
_TOKEN_CACHE_TTL = 7000   # seconds (WeCom tokens expire in 7200 s)

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


def get_wecom_token(corp_id: str, oauth_secret: str) -> str:
    """
    Return a cached or freshly-fetched WeCom access_token for the given corp_id.

    Cache key includes corp_id so multi-tenant future use stays safe.
    Never logs token value, code, or secret.
    """
    now = time.monotonic()
    with _token_lock:
        cached = _token_cache.get(corp_id)
        if cached and now < cached[1]:
            return cached[0]

    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params={"corpid": corp_id, "corpsecret": oauth_secret})
        data = resp.json()
    except Exception as exc:
        logger.error("WeCom gettoken request failed: %s", type(exc).__name__)
        raise RuntimeError("Failed to fetch WeCom access_token") from exc

    if data.get("errcode", -1) != 0:
        logger.error("WeCom gettoken returned error: errcode=%s", data.get("errcode"))
        raise RuntimeError(f"WeCom gettoken failed: errcode={data.get('errcode')}")

    token: str = data["access_token"]
    expires_in = int(data.get("expires_in", 7200))
    cached_until = now + min(expires_in - 200, _TOKEN_CACHE_TTL)

    with _token_lock:
        _token_cache[corp_id] = (token, cached_until)

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
        raise HTTPException(status_code=401, detail="Session expired or invalid")

    user = db.query(AdminUser).filter(AdminUser.id == session.admin_user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    return user, session.tenant_id


def _is_production() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() == "production"
