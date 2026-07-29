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


def _external_contact_get(
    path: str, access_token: str, **params: str
) -> Optional[dict]:
    """Call an external-contact read endpoint without exposing response data."""
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(
                f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/{path}",
                params={"access_token": access_token, **params},
            )
        data = resp.json()
    except Exception:
        logger.warning("external-contact request failed")
        return None

    return data if isinstance(data, dict) and data.get("errcode", -1) == 0 else None


def list_follow_userids(access_token: str) -> Optional[list[str]]:
    """Return internal userids which follow at least one external contact."""
    data = _external_contact_get("get_follow_user_list", access_token)
    if data is None:
        return None
    users = data.get("follow_user")
    if not isinstance(users, list):
        return None
    return [userid for value in users if (userid := _clean(value))]


def list_external_userids_by_user(
    access_token: str, userid: str
) -> Optional[list[str]]:
    """Return external userids followed by one internal WeCom user."""
    data = _external_contact_get("list", access_token, userid=userid)
    if data is None:
        return None
    userids = data.get("external_userid")
    if not isinstance(userids, list):
        return None
    return [
        external_userid for value in userids if (external_userid := _clean(value))
    ]


def get_external_contact(access_token: str, external_userid: str) -> Optional[dict]:
    """Return the complete externalcontact/get payload, or None on failure."""
    return _external_contact_get(
        "get", access_token, external_userid=external_userid
    )


def get_corp_tag_list(access_token: str) -> Optional[dict[str, str]]:
    """Return a mapping of corporate external-contact tag id to tag name."""
    data = _external_contact_get("get_corp_tag_list", access_token)
    if data is None:
        return None

    tags: dict[str, str] = {}
    for group in data.get("tag_group", []) or []:
        if not isinstance(group, dict):
            continue
        for tag in group.get("tag", []) or []:
            if not isinstance(tag, dict):
                continue
            tag_id = _clean(tag.get("id"))
            tag_name = _clean(tag.get("name"))
            if tag_id and tag_name:
                tags[tag_id] = tag_name
    return tags
