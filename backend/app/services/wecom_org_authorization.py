"""Trusted WeCom third-party organization authorization primitives (RND-346)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.auth import strict_int_equals
from app.crypto import encrypt_value
from app.db.models import WecomAuthorizationAttempt, WecomAuthorizationProof
from app.settings import WecomThirdPartySettings, get_wecom_third_party_settings

AUTHORIZATION_TTL_SECONDS = 600
PROOF_TTL_SECONDS = 600
PROOF_COOKIE = "wecom_org_proof"


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


class OfficialWecomOrganizationAuthorizationProvider:
    """Small replaceable client over WeCom's official third-party APIs."""

    _API = "https://qyapi.weixin.qq.com/cgi-bin"

    def __init__(self, settings: WecomThirdPartySettings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=10.0)
        required = (
            settings.wecom_third_party_suite_id,
            settings.wecom_third_party_suite_secret,
            settings.wecom_third_party_suite_ticket,
            settings.wecom_third_party_callback_url,
        )
        if not all(value.strip() for value in required):
            raise WecomAuthorizationError("WeCom third-party authorization is not configured")

    def _post(self, path: str, *, params: dict, body: dict) -> dict:
        try:
            response = self.client.post(f"{self._API}{path}", params=params, json=body)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WecomAuthorizationError("WeCom authorization provider is unavailable") from exc
        if not isinstance(payload, dict) or not strict_int_equals(payload.get("errcode"), 0):
            raise WecomAuthorizationError("WeCom authorization provider rejected the request")
        return payload

    def _get(self, path: str, *, params: dict) -> dict:
        try:
            response = self.client.get(f"{self._API}{path}", params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WecomAuthorizationError("WeCom authorization provider is unavailable") from exc
        if not isinstance(payload, dict) or not strict_int_equals(payload.get("errcode"), 0):
            raise WecomAuthorizationError("WeCom authorization provider rejected the request")
        return payload

    def _suite_token(self) -> str:
        payload = self._post(
            "/service/get_suite_token",
            params={},
            body={
                "suite_id": self.settings.wecom_third_party_suite_id.strip(),
                "suite_secret": self.settings.wecom_third_party_suite_secret.strip(),
                "suite_ticket": self.settings.wecom_third_party_suite_ticket.strip(),
            },
        )
        return _required_text(payload, "suite_access_token")

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


def get_wecom_org_authorization_provider() -> WecomOrganizationAuthorizationProvider:
    return OfficialWecomOrganizationAuthorizationProvider(get_wecom_third_party_settings())


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
