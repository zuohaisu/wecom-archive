"""
Minimal WeCom contact metadata client for RND-130 (contact display name sync).

Wraps two read-only WeCom OpenAPI calls used to resolve a human-readable
display name for a wecom_userid seen in the archive:

  fetch_member_display_name             internal employee (user/get)
  fetch_external_contact_display_name   external customer (externalcontact/get)

Both functions return None on any failure — missing user, network error,
malformed response, or simply "no usable name field" — rather than raising.
Callers treat "no metadata found" as an expected outcome, not an error.

Security notes:
  - access_token is a request parameter only; it is never logged.
  - No response body is logged (may contain external contact PII).
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10.0


def _clean(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def fetch_member_display_name(access_token: str, userid: str) -> Optional[str]:
    """Return an internal member's configured name via user/get, or None.

    None is returned both when the user does not exist (likely an external
    contact rather than an internal member) and when the request fails.
    """
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/user/get",
                params={"access_token": access_token, "userid": userid},
            )
        data = resp.json()
    except Exception:
        logger.warning("fetch_member_display_name: request failed (userid not logged)")
        return None

    if data.get("errcode", -1) != 0:
        return None

    return _clean(data.get("name"))


def fetch_external_contact_display_name(
    access_token: str, external_userid: str
) -> Optional[str]:
    """Return a deterministic display label for an external contact, or None.

    Label precedence:
      1. remark set by any internal user following this contact
         (follow_user[].remark) — the internal team's own naming.
      2. the external contact's own nickname (external_contact.name).
      3. None — caller falls back to the raw ID.
    """
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/externalcontact/get",
                params={"access_token": access_token, "external_userid": external_userid},
            )
        data = resp.json()
    except Exception:
        logger.warning(
            "fetch_external_contact_display_name: request failed (userid not logged)"
        )
        return None

    if data.get("errcode", -1) != 0:
        return None

    for follow_user in data.get("follow_user", []) or []:
        remark = _clean(follow_user.get("remark"))
        if remark:
            return remark

    contact = data.get("external_contact") or {}
    return _clean(contact.get("name"))
