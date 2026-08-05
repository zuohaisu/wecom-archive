from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Tenant,
    TenantWecomConfig,
    WecomAuthorizationAttempt,
    WecomAuthorizationProof,
    WecomOrganizationClaim,
)
from app.db.session import get_db
from app.main import create_app
from app.services.wecom_org_authorization import (
    AuthorizedOrganization,
    get_wecom_org_authorization_provider,
)


class FakeProvider:
    def __init__(self):
        self.state = ""

    def build_install_url(self, state: str) -> str:
        self.state = state
        return f"https://provider.invalid/install?state={state}"

    def exchange(self, authorization_code: str) -> AuthorizedOrganization:
        return AuthorizedOrganization(
            "ww-private-corp",
            "企业微信官方名称",
            "private-admin-subject",
            "1000002",
            "private-permanent-code",
        )


def _setup(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantWecomConfig.__table__,
            WecomAuthorizationAttempt.__table__,
            WecomAuthorizationProof.__table__,
            WecomOrganizationClaim.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    provider = FakeProvider()
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_wecom_org_authorization_provider] = lambda: provider
    return TestClient(app), factory, provider


def _authorize(client: TestClient, provider: FakeProvider):
    client.get("/api/auth/wecom/third-party/install", follow_redirects=False)
    return client.get(
        "/api/auth/wecom/third-party/callback",
        params={"code": "one-use", "state": provider.state},
        follow_redirects=False,
    )


def test_unknown_org_uses_existing_login_page_and_only_valid_claim_gets_action(monkeypatch):
    client, factory, provider = _setup(monkeypatch)
    response = _authorize(client, provider)
    assert response.headers["location"] == "/admin/login?error=organization_not_found"
    assert "ww-private-corp" not in response.headers.get("set-cookie", "")

    valid_page = client.get(response.headers["location"])
    assert "组织不存在，请先创建组织" in valid_page.text
    assert 'href="/admin/organization/confirm"' in valid_page.text

    other_client = TestClient(client.app)
    forged_page = other_client.get("/admin/login?error=organization_not_found")
    assert 'data-i18n="login.error.organizationNotFound"' not in forged_page.text
    assert 'data-i18n="login.error.authFailed"' in forged_page.text
    assert 'href="/admin/organization/confirm"' not in forged_page.text

    with factory() as db:
        claim = db.query(WecomOrganizationClaim).one()
        assert claim.state == "pending"
        assert claim.public_ref_hash not in response.headers.get("set-cookie", "")


def test_confirmation_is_read_only_safe_and_localized(monkeypatch):
    client, _, provider = _setup(monkeypatch)
    _authorize(client, provider)
    for language, expected in (("zh-CN", "确认创建组织"), ("en", "Confirm organization creation"), ("ja", "組織の作成を確認")):
        page = client.get("/admin/organization/confirm", headers={"accept-language": language})
        assert page.status_code == 200
        assert expected in page.text
        assert "企业微信官方名称" in page.text
        assert "ww-private-corp" not in page.text
        assert "private-admin-subject" not in page.text
        assert "private-permanent-code" not in page.text
        assert 'name="corp_name"' not in page.text
        assert "<output" in page.text


def test_confirm_and_cancel_are_single_browser_claim_transitions(monkeypatch):
    client, factory, provider = _setup(monkeypatch)
    _authorize(client, provider)
    confirmed = client.post(
        "/api/auth/wecom/organization-claim/confirm", follow_redirects=False
    )
    assert confirmed.status_code == 303
    with factory() as db:
        assert db.query(WecomOrganizationClaim).one().state == "confirmed"
    replay = client.post(
        "/api/auth/wecom/organization-claim/confirm", follow_redirects=False
    )
    assert replay.headers["location"] == "/admin/login?error=auth_failed"

    cancelled = client.post(
        "/api/auth/wecom/organization-claim/cancel", follow_redirects=False
    )
    assert cancelled.status_code == 303
    with factory() as db:
        assert db.query(WecomOrganizationClaim).one().state == "cancelled"


def test_existing_corp_is_safe_conflict_without_claim(monkeypatch):
    client, factory, provider = _setup(monkeypatch)
    with factory() as db:
        db.add(Tenant(id="existing", name="Existing", slug="existing", is_active=True))
        config = TenantWecomConfig(
            id="config",
            tenant_id="existing",
            corp_id="ww-private-corp",
            agent_id="1000001",
            app_secret="encrypted-placeholder",
            callback_domain="",
            is_active=True,
        )
        db.add(config)
        db.commit()
    response = _authorize(client, provider)
    assert response.headers["location"] == "/admin/login?error=organization_exists"
    with factory() as db:
        assert db.query(WecomOrganizationClaim).count() == 0
