from __future__ import annotations

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
)
from app.db.session import get_db
from app.main import create_app
from app.routers import wecom_org_authorization
from app.services.wecom_org_authorization import AuthorizedOrganization
from app.settings import get_self_service_trial_settings


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
        ],
    )
    factory = sessionmaker(bind=engine)
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    if provider is not None:
        monkeypatch.setattr(
            wecom_org_authorization,
            "get_wecom_org_authorization_provider",
            lambda _db: provider,
        )
    return TestClient(app), factory


def test_flag_defaults_closed(monkeypatch):
    for name in ("SELF_SERVICE_TRIAL_ENTRY_ENABLED",):
        monkeypatch.delenv(name, raising=False)
    assert get_self_service_trial_settings().self_service_trial_entry_enabled == "false"


def test_login_page_hides_trial_cta_when_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SELF_SERVICE_TRIAL_ENTRY_ENABLED", raising=False)
    client, _ = _client(monkeypatch, provider=None)

    page = client.get("/admin/login")

    assert page.status_code == 200
    assert 'data-i18n="login.startTrial"' not in page.text
    assert '/api/auth/wecom/third-party/install' not in page.text


def test_login_page_shows_trial_cta_and_fee_boundary_when_flag_is_on(monkeypatch):
    monkeypatch.setenv("SELF_SERVICE_TRIAL_ENTRY_ENABLED", "true")
    client, _ = _client(monkeypatch, provider=None)

    page = client.get("/admin/login")

    assert page.status_code == 200
    assert 'data-i18n="login.startTrial"' in page.text
    assert 'href="/api/auth/wecom/third-party/install"' in page.text
    assert 'data-i18n="login.trialFeeDisclaimer"' in page.text
    assert "另计" in page.text


def test_trial_cta_flag_does_not_gate_the_underlying_authorization_endpoints(monkeypatch):
    """RND-396 only controls CTA discoverability; RND-346/347/348's install
    and callback endpoints must keep working exactly as before regardless of
    this flag, since RND-353 owns their own separate production rollout."""
    monkeypatch.delenv("SELF_SERVICE_TRIAL_ENTRY_ENABLED", raising=False)
    provider = FakeProvider()
    client, factory = _client(monkeypatch, provider)

    started = client.get("/api/auth/wecom/third-party/install", follow_redirects=False)
    assert started.status_code == 302
    assert provider.state and provider.state in started.headers["location"]

    finished = client.get(
        "/api/auth/wecom/third-party/callback",
        params={"code": "one-use-code", "state": provider.state},
        follow_redirects=False,
    )
    assert finished.status_code == 302
    assert finished.headers["location"] == "/admin/login?error=organization_not_found"
    with factory() as db:
        assert db.query(WecomOrganizationClaim).one().state == "pending"


def test_organization_confirm_page_weaves_trial_status_into_localized_copy(monkeypatch):
    provider = FakeProvider()
    client, _ = _client(monkeypatch, provider)
    client.get("/api/auth/wecom/third-party/install", follow_redirects=False)
    client.get(
        "/api/auth/wecom/third-party/callback",
        params={"code": "one-use-code", "state": provider.state},
        follow_redirects=False,
    )

    for language, expected in (
        ("zh-CN", "15 天免费试用"),
        ("en", "15-day free trial"),
        ("ja", "15日間の無料トライアル"),
    ):
        page = client.get("/admin/organization/confirm", headers={"accept-language": language})
        assert page.status_code == 200
        assert expected in page.text
        assert "另计" in page.text or "official fees" in page.text or "公式料金" in page.text
        for secret in ("ww-private-corp", "private-admin-subject", "private-permanent-code"):
            assert secret not in page.text
