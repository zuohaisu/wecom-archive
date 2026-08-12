"""Trusted WeCom third-party organization authorization primitives (RND-346)."""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Protocol
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.auth import strict_int_equals
from app.crypto import encrypt_value
from app.db.models import WecomAuthorizationAttempt, WecomAuthorizationProof
from app.services.wecom_suite_ticket import (
    SuiteTicketUnavailable,
    load_fresh_suite_ticket,
)
from app.settings import WecomThirdPartySettings, get_wecom_third_party_settings

AUTHORIZATION_TTL_SECONDS = 600
PROOF_TTL_SECONDS = 600
PROOF_COOKIE = "wecom_org_proof"
SUITE_TOKEN_MAX_TTL_SECONDS = 2 * 60 * 60
SUITE_TOKEN_REFRESH_WINDOW_SECONDS = 5 * 60

# httpx/httpcore diagnostic logs can include provider URLs whose query string
# carries suite_access_token. Keep those libraries at warnings/errors; this
# service reports only fixed, coarse errors of its own.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class WecomAuthorizationError(RuntimeError):
    """Safe, non-sensitive failure for the third-party authorization flow."""


@dataclass(frozen=True)
class AuthorizedOrganization:
    corp_id: str
    corp_name: str
    authorized_subject: str
    agent_id: str | None
    permanent_code: str
    authorization_mode: str = "admin"


class WecomOrganizationAuthorizationProvider(Protocol):
    def build_install_url(self, state: str) -> str: ...

    def exchange(self, authorization_code: str) -> AuthorizedOrganization: ...


def _required_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WecomAuthorizationError("WeCom authorization response is incomplete")
    return value.strip()


@dataclass(frozen=True)
class _CachedSuiteToken:
    value: str
    expires_at_monotonic: float


class SuiteAccessTokenCache:
    """Per-suite bounded cache with one refresh in flight per suite."""

    def __init__(self) -> None:
        self._entries: dict[str, _CachedSuiteToken] = {}
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()

    def _suite_lock(self, suite_id: str) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(suite_id, Lock())

    def get(
        self,
        suite_id: str,
        refresh: Callable[[], tuple[str, int]],
    ) -> str:
        with self._suite_lock(suite_id):
            now = time.monotonic()
            cached = self._entries.get(suite_id)
            if (
                cached is not None
                and cached.expires_at_monotonic - now
                > SUITE_TOKEN_REFRESH_WINDOW_SECONDS
            ):
                return cached.value
            try:
                value, provider_ttl = refresh()
            except WecomAuthorizationError:
                # A refresh-window failure may use a still-valid cached token,
                # but an explicitly expired token is never returned.
                if cached is not None and cached.expires_at_monotonic > now:
                    return cached.value
                raise
            ttl = min(provider_ttl, SUITE_TOKEN_MAX_TTL_SECONDS)
            self._entries[suite_id] = _CachedSuiteToken(value, now + ttl)
            return value


_SUITE_TOKEN_CACHE = SuiteAccessTokenCache()


class OfficialWecomOrganizationAuthorizationProvider:
    """Small replaceable client over WeCom's official third-party APIs."""

    _API = "https://qyapi.weixin.qq.com/cgi-bin"

    def __init__(
        self,
        settings: WecomThirdPartySettings,
        db: Session,
        client: httpx.Client | None = None,
        token_cache: SuiteAccessTokenCache | None = None,
    ):
        self.settings = settings
        required = (
            settings.wecom_third_party_suite_id,
            settings.wecom_third_party_suite_secret,
            settings.wecom_third_party_callback_url,
        )
        if not all(value.strip() for value in required):
            raise WecomAuthorizationError("WeCom third-party authorization is not configured")
        self.db = db
        try:
            load_fresh_suite_ticket(
                self.db,
                self.settings.wecom_third_party_suite_id.strip(),
            )
        except SuiteTicketUnavailable as exc:
            raise WecomAuthorizationError(
                "WeCom third-party authorization is not configured"
            ) from exc
        # Provider credentials must not be routed through ambient developer
        # proxy variables. Deployments that require an egress proxy must pass
        # an explicitly controlled client at composition time.
        self.client = client or httpx.Client(timeout=10.0, trust_env=False)
        self.token_cache = token_cache or _SUITE_TOKEN_CACHE

    def _post(
        self,
        path: str,
        *,
        params: dict,
        body: dict,
        allow_missing_errcode: bool = False,
    ) -> dict:
        try:
            response = self.client.post(f"{self._API}{path}", params=params, json=body)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            # httpx exception text may contain the full request URL, including
            # suite_access_token query parameters. Never chain it outward.
            raise WecomAuthorizationError(
                "WeCom authorization provider is unavailable"
            ) from None
        if not isinstance(payload, dict):
            raise WecomAuthorizationError("WeCom authorization provider rejected the request")
        errcode = payload.get("errcode")
        if (errcode is None and not allow_missing_errcode) or (
            errcode is not None and not strict_int_equals(errcode, 0)
        ):
            raise WecomAuthorizationError("WeCom authorization provider rejected the request")
        return payload

    def _get(self, path: str, *, params: dict) -> dict:
        try:
            response = self.client.get(f"{self._API}{path}", params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            # See _post(): the underlying request URL is sensitive.
            raise WecomAuthorizationError(
                "WeCom authorization provider is unavailable"
            ) from None
        if not isinstance(payload, dict) or not strict_int_equals(payload.get("errcode"), 0):
            raise WecomAuthorizationError("WeCom authorization provider rejected the request")
        return payload

    def _refresh_suite_token(self) -> tuple[str, int]:
        suite_id = self.settings.wecom_third_party_suite_id.strip()
        try:
            ticket = load_fresh_suite_ticket(self.db, suite_id)
        except SuiteTicketUnavailable as exc:
            raise WecomAuthorizationError(
                "WeCom third-party authorization is not configured"
            ) from exc
        payload = self._post(
            "/service/get_suite_token",
            params={},
            body={
                "suite_id": suite_id,
                "suite_secret": self.settings.wecom_third_party_suite_secret.strip(),
                "suite_ticket": ticket,
            },
            # The official API documents that successful responses may omit
            # errcode; a present non-zero errcode still fails closed.
            allow_missing_errcode=True,
        )
        token = _required_text(payload, "suite_access_token")
        if len(token.encode("utf-8")) > 512:
            raise WecomAuthorizationError("WeCom authorization response is incomplete")
        expires_in = payload.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in <= 0
        ):
            raise WecomAuthorizationError("WeCom authorization response is incomplete")
        return token, expires_in

    def _suite_token(self) -> str:
        suite_id = self.settings.wecom_third_party_suite_id.strip()
        return self.token_cache.get(suite_id, self._refresh_suite_token)

    def build_install_url(self, state: str) -> str:
        suite_token = self._suite_token()
        payload = self._post(
            "/service/get_pre_auth_code",
            params={"suite_access_token": suite_token},
            body={},
        )
        query = urlencode(
            {
                "suite_id": self.settings.wecom_third_party_suite_id.strip(),
                "pre_auth_code": _required_text(payload, "pre_auth_code"),
                "redirect_uri": self.settings.wecom_third_party_callback_url.strip(),
                "state": state,
            }
        )
        return f"https://open.work.weixin.qq.com/3rdapp/install?{query}"

    def exchange(self, authorization_code: str) -> AuthorizedOrganization:
        suite_token = self._suite_token()
        permanent = self._post(
            "/service/v2/get_permanent_code",
            params={"suite_access_token": suite_token},
            body={"auth_code": authorization_code},
        )
        corp = permanent.get("auth_corp_info")
        user = permanent.get("auth_user_info")
        if not isinstance(corp, dict) or not isinstance(user, dict):
            raise WecomAuthorizationError("WeCom authorization response is incomplete")
        corp_id = _required_text(corp, "corpid")
        corp_name = _required_text(corp, "corp_name")
        subject = _required_text(user, "userid")
        permanent_code = _required_text(permanent, "permanent_code")

        auth_info = self._post(
            "/service/v2/get_auth_info",
            params={"suite_access_token": suite_token},
            body={"auth_corpid": corp_id, "permanent_code": permanent_code},
        )
        auth = auth_info.get("auth_info")
        agents = auth.get("agent") if isinstance(auth, dict) else None
        if not isinstance(agents, list) or not agents:
            raise WecomAuthorizationError("WeCom authorization response is incomplete")
        agent = agents[0]
        if not isinstance(agent, dict) or not strict_int_equals(agent.get("auth_mode"), 0):
            raise WecomAuthorizationError("Administrator authorization is required")
        agent_id = agent.get("agentid")
        if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id <= 0:
            raise WecomAuthorizationError("WeCom authorization response is incomplete")

        admins = self._post(
            "/service/get_admin_list",
            params={"suite_access_token": suite_token},
            body={"auth_corpid": corp_id, "agentid": agent_id},
        )
        admin_list = admins.get("admin")
        if not isinstance(admin_list, list) or not any(
            isinstance(item, dict)
            and item.get("userid") == subject
            and strict_int_equals(item.get("auth_type"), 1)
            for item in admin_list
        ):
            raise WecomAuthorizationError("A management administrator must authorize the application")
        return AuthorizedOrganization(corp_id, corp_name, subject, str(agent_id), permanent_code)


def get_wecom_org_authorization_provider(
    db: Session,
) -> WecomOrganizationAuthorizationProvider:
    return OfficialWecomOrganizationAuthorizationProvider(
        get_wecom_third_party_settings(),
        db,
    )


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def begin_authorization(db: Session, provider: WecomOrganizationAuthorizationProvider) -> str:
    raw_state = secrets.token_urlsafe(32)
    install_url = provider.build_install_url(raw_state)
    now = datetime.now(timezone.utc)
    db.add(
        WecomAuthorizationAttempt(
            id=str(uuid.uuid4()),
            state_hash=_digest(raw_state),
            status="pending",
            expires_at=now + timedelta(seconds=AUTHORIZATION_TTL_SECONDS),
        )
    )
    db.commit()
    return install_url


def complete_authorization(
    db: Session,
    provider: WecomOrganizationAuthorizationProvider,
    *,
    state: str,
    authorization_code: str,
) -> str:
    now = datetime.now(timezone.utc)
    attempt = (
        db.query(WecomAuthorizationAttempt)
        .filter(
            WecomAuthorizationAttempt.state_hash == _digest(state),
            WecomAuthorizationAttempt.status == "pending",
            WecomAuthorizationAttempt.expires_at > now,
        )
        .with_for_update()
        .first()
    )
    if attempt is None:
        raise WecomAuthorizationError("Authorization state is invalid or expired")
    attempt.status = "consumed"
    attempt.consumed_at = now
    db.commit()

    organization = provider.exchange(authorization_code)
    if organization.authorization_mode != "admin":
        raise WecomAuthorizationError("Administrator authorization is required")
    raw_browser_token = secrets.token_urlsafe(32)
    proof = WecomAuthorizationProof(
        id=str(uuid.uuid4()),
        browser_token_hash=_digest(raw_browser_token),
        corp_id=organization.corp_id,
        corp_name=organization.corp_name,
        authorized_subject=organization.authorized_subject,
        agent_id=organization.agent_id,
        authorization_mode="admin",
        permanent_code_encrypted=encrypt_value(organization.permanent_code),
        status="pending",
        expires_at=now + timedelta(seconds=PROOF_TTL_SECONDS),
    )
    db.add(proof)
    db.commit()
    return raw_browser_token
