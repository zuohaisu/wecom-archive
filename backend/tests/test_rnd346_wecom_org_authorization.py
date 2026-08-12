from __future__ import annotations

import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AdminLoginIdentity,
    AdminSession,
    AdminUser,
    Tenant,
    TenantWecomConfig,
    WecomAuthorizationAttempt,
    WecomAuthorizationProof,
    WecomOrganizationClaim,
    WecomSuiteTicketState,
)
from app.db.session import get_db
from app.main import create_app
from app.routers import wecom_org_authorization
from app.services.wecom_org_authorization import (
    AuthorizedOrganization,
    OfficialWecomOrganizationAuthorizationProvider,
    SuiteAccessTokenCache,
    WecomAuthorizationError,
)
from app.services.wecom_suite_ticket import store_suite_ticket
from app.settings import WecomThirdPartySettings


class FakeProvider:
    def __init__(self, *, mode: str = "admin"):
        self.mode = mode
        self.last_state = ""

    def build_install_url(self, state: str) -> str:
        self.last_state = state
        return f"https://provider.invalid/install?state={state}"

    def exchange(self, authorization_code: str) -> AuthorizedOrganization:
        if authorization_code == "reject":
            raise WecomAuthorizationError("rejected")
        return AuthorizedOrganization(
            corp_id="ww-sensitive-corp",
            corp_name="Official Corp Name",
            authorized_subject="management-admin",
            agent_id="1000002",
            permanent_code="sensitive-permanent-code",
            authorization_mode=self.mode,
        )


def test_official_provider_uses_third_party_admin_list_http_contract(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WECOM_THIRD_PARTY_SUITE_TICKET", "obsolete-static-ticket")
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "body": body,
            }
        )
        payloads = {
            "/cgi-bin/service/get_suite_token": {
                "suite_access_token": "suite-token",
                "expires_in": 7200,
            },
            "/cgi-bin/service/v2/get_permanent_code": {
                "errcode": 0,
                "auth_corp_info": {"corpid": "ww-official", "corp_name": "Official Corp"},
                "auth_user_info": {"userid": "management-admin"},
                "permanent_code": "permanent-code",
            },
            "/cgi-bin/service/v2/get_auth_info": {
                "errcode": 0,
                "auth_info": {"agent": [{"agentid": 1000002, "auth_mode": 0}]},
            },
            "/cgi-bin/service/get_admin_list": {
                "errcode": 0,
                "admin": [{"userid": "management-admin", "auth_type": 1}],
            },
        }
        return httpx.Response(200, json=payloads[request.url.path])

    settings = WecomThirdPartySettings(
        wecom_third_party_suite_id="suite-id",
        wecom_third_party_suite_secret="suite-secret",
        wecom_third_party_callback_url="https://console.example.test/callback",
    )
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[WecomSuiteTicketState.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db:
        store_suite_ticket(
            db,
            suite_id="suite-id",
            ticket="suite-ticket",
            source_timestamp=1,
        )
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            provider = OfficialWecomOrganizationAuthorizationProvider(
                settings,
                db,
                http_client,
                SuiteAccessTokenCache(),
            )
            organization = provider.exchange("authorization-code")

    assert organization.agent_id == "1000002"
    assert requests[0]["body"]["suite_ticket"] == "suite-ticket"
    assert "obsolete-static-ticket" not in json.dumps(requests)
    assert requests[-1] == {
        "method": "POST",
        "path": "/cgi-bin/service/get_admin_list",
        "query": {"suite_access_token": "suite-token"},
        "body": {"auth_corpid": "ww-official", "agentid": 1000002},
    }
    assert all(request["path"] != "/cgi-bin/agent/get_admin_list" for request in requests)
    assert all(request["path"] != "/cgi-bin/service/get_corp_token" for request in requests)


def _client(monkeypatch, provider: FakeProvider | None = None):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantWecomConfig.__table__,
            AdminUser.__table__,
            AdminLoginIdentity.__table__,
            AdminSession.__table__,
            WecomAuthorizationAttempt.__table__,
            WecomAuthorizationProof.__table__,
            WecomOrganizationClaim.__table__,
            WecomSuiteTicketState.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    app = create_app()

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    if provider is not None:
        monkeypatch.setattr(
            wecom_org_authorization,
            "get_wecom_org_authorization_provider",
            lambda _db: provider,
        )
    return TestClient(app), factory


def test_unconfigured_provider_redirects_without_writing_authorization_data(monkeypatch):
    for setting in (
        "WECOM_THIRD_PARTY_SUITE_ID",
        "WECOM_THIRD_PARTY_SUITE_SECRET",
        "WECOM_THIRD_PARTY_CALLBACK_URL",
    ):
        monkeypatch.delenv(setting, raising=False)
    client, factory = _client(monkeypatch, provider=None)

    install = client.get("/api/auth/wecom/third-party/install", follow_redirects=False)
    callback = client.get(
        "/api/auth/wecom/third-party/callback",
        params={"code": "one-use-code", "state": "valid-looking-state"},
        follow_redirects=False,
    )

    for response in (install, callback):
        assert response.status_code == 302
        assert response.headers["location"] == "/admin/login?error=config_error"
    with factory() as db:
        assert db.query(WecomAuthorizationAttempt).count() == 0
        assert db.query(WecomAuthorizationProof).count() == 0
        assert db.query(Tenant).count() == 0
        assert db.query(TenantWecomConfig).count() == 0
        assert db.query(AdminUser).count() == 0
        assert db.query(AdminLoginIdentity).count() == 0
        assert db.query(AdminSession).count() == 0


def test_persisted_fresh_ticket_is_required_before_install_writes_state(monkeypatch):
    monkeypatch.setenv("WECOM_THIRD_PARTY_SUITE_ID", "suite-id")
    monkeypatch.setenv("WECOM_THIRD_PARTY_SUITE_SECRET", "suite-secret")
    monkeypatch.setenv(
        "WECOM_THIRD_PARTY_CALLBACK_URL",
        "https://console.example.test/api/auth/wecom/third-party/callback",
    )
    monkeypatch.setenv("WECOM_THIRD_PARTY_SUITE_TICKET", "obsolete-static-ticket")
    client, factory = _client(monkeypatch, provider=None)

    response = client.get("/api/auth/wecom/third-party/install", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login?error=config_error"
    with factory() as db:
        assert db.query(WecomAuthorizationAttempt).count() == 0
        assert db.query(WecomSuiteTicketState).count() == 0


@pytest.mark.parametrize("field_key", [None, "not-a-valid-fernet-key"])
def test_install_rejects_invalid_field_encryption_config_without_writing_state(
    monkeypatch, field_key
):
    provider = FakeProvider()
    client, factory = _client(monkeypatch, provider)
    if field_key is None:
        monkeypatch.delenv("FIELD_ENCRYPTION_KEY")
    else:
        monkeypatch.setenv("FIELD_ENCRYPTION_KEY", field_key)

    response = client.get("/api/auth/wecom/third-party/install", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login?error=config_error"
    assert provider.last_state == ""
    with factory() as db:
        assert db.query(WecomAuthorizationAttempt).count() == 0
        assert db.query(WecomAuthorizationProof).count() == 0
        assert db.query(Tenant).count() == 0
        assert db.query(TenantWecomConfig).count() == 0
        assert db.query(AdminUser).count() == 0
        assert db.query(AdminLoginIdentity).count() == 0
        assert db.query(AdminSession).count() == 0


@pytest.mark.parametrize("field_key", [None, "not-a-valid-fernet-key"])
def test_callback_rejects_invalid_field_encryption_config_without_consuming_state(
    monkeypatch, field_key
):
    provider = FakeProvider()
    client, factory = _client(monkeypatch, provider)
    started = client.get("/api/auth/wecom/third-party/install", follow_redirects=False)
    assert started.status_code == 302
    if field_key is None:
        monkeypatch.delenv("FIELD_ENCRYPTION_KEY")
    else:
        monkeypatch.setenv("FIELD_ENCRYPTION_KEY", field_key)

    callback = client.get(
        "/api/auth/wecom/third-party/callback",
        params={"code": "one-use-code", "state": provider.last_state},
        follow_redirects=False,
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/admin/login?error=config_error"
    with factory() as db:
        assert db.query(WecomAuthorizationAttempt).one().status == "pending"
        assert db.query(WecomAuthorizationProof).count() == 0
        assert db.query(Tenant).count() == 0
        assert db.query(TenantWecomConfig).count() == 0
        assert db.query(AdminUser).count() == 0
        assert db.query(AdminLoginIdentity).count() == 0
        assert db.query(AdminSession).count() == 0


def test_authorization_creates_only_short_lived_server_side_proof(monkeypatch):
    provider = FakeProvider()
    client, factory = _client(monkeypatch, provider)
    started = client.get("/api/auth/wecom/third-party/install", follow_redirects=False)
    assert started.status_code == 302
    assert provider.last_state and provider.last_state in started.headers["location"]

    finished = client.get(
        "/api/auth/wecom/third-party/callback",
        params={"code": "one-use-code", "state": provider.last_state},
        follow_redirects=False,
    )
    assert finished.status_code == 302
    cookie = finished.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    for secret in ("ww-sensitive-corp", "Official Corp Name", "management-admin", "sensitive-permanent-code"):
        assert secret not in cookie
        assert secret not in finished.headers["location"]

    with factory() as db:
        proof = db.query(WecomAuthorizationProof).one()
        assert proof.corp_name == "Official Corp Name"
        assert proof.permanent_code_encrypted != "sensitive-permanent-code"
        assert db.query(Tenant).count() == 0
        assert db.query(TenantWecomConfig).count() == 0
        assert db.query(AdminUser).count() == 0
        assert db.query(AdminLoginIdentity).count() == 0
        assert db.query(AdminSession).count() == 0


def test_state_is_single_use_and_replay_has_no_new_proof(monkeypatch):
    provider = FakeProvider()
    client, factory = _client(monkeypatch, provider)
    client.get("/api/auth/wecom/third-party/install", follow_redirects=False)
    params = {"code": "one-use-code", "state": provider.last_state}
    assert client.get("/api/auth/wecom/third-party/callback", params=params, follow_redirects=False).status_code == 302
    replay = client.get("/api/auth/wecom/third-party/callback", params=params, follow_redirects=False)
    assert replay.headers["location"] == "/admin/login?error=auth_failed"
    with factory() as db:
        assert db.query(WecomAuthorizationAttempt).one().status == "consumed"
        assert db.query(WecomAuthorizationProof).count() == 1


def test_member_authorization_fails_closed_without_proof(monkeypatch):
    provider = FakeProvider(mode="member")
    client, factory = _client(monkeypatch, provider)
    client.get("/api/auth/wecom/third-party/install", follow_redirects=False)
    response = client.get(
        "/api/auth/wecom/third-party/callback",
        params={"code": "one-use-code", "state": provider.last_state},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin/login?error=auth_failed"
    with factory() as db:
        assert db.query(WecomAuthorizationProof).count() == 0
