"""End-to-end configuration-centre journey for RND-254."""

from __future__ import annotations

from typing import Generator
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import resolver
from app.db.base import Base
from app.db.models import AdminSession, AdminUser, AppConfigStore, Tenant
from app.db.session import get_db
from app.main import create_app


@pytest.fixture()
def fresh_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, Mock], None, None]:
    """Provide an initially unconfigured password-mode deployment."""
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    for name in (
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD_HASH",
        "WECOM_CORP_ID",
        "WECOM_AGENT_ID",
        "WECOM_OAUTH_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            AppConfigStore.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    seed_db = factory()
    seed_db.add(Tenant(id="tenant-default", name="Default", slug="default", is_active=True))
    seed_db.commit()
    seed_db.close()

    def override_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    self_check = Mock(return_value=(True, None))
    app = create_app()
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr("app.routers.auth.write_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.routers.settings.check_wecom", self_check)
    resolver.invalidate()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, self_check
    app.dependency_overrides.clear()
    resolver.invalidate()
    engine.dispose()


def _settings_fields(response_json: dict) -> dict[str, dict]:
    return {
        item["key"]: item
        for group in response_json["groups"].values()
        for item in group
    }


def test_fresh_deployment_bootstrap_login_settings_update_and_self_check(
    fresh_deployment: tuple[TestClient, Mock],
) -> None:
    """Exercise one user journey across bootstrap, auth, settings, and checking."""
    client, self_check = fresh_deployment
    admin_username = "rnd254-admin"
    admin_password = "rnd254-password"
    corp_id = "ww-rnd254"
    original_agent_id = "1000254"
    updated_agent_id = "1001254"
    oauth_secret = "rnd254-oauth-secret"

    assert client.get("/api/admin/settings/bootstrap-status").json() == {"initialized": False}

    bootstrap = client.post(
        "/api/admin/settings/bootstrap",
        json={
            "admin_username": admin_username,
            "admin_password": admin_password,
            "wecom_corp_id": corp_id,
            "wecom_agent_id": original_agent_id,
            "wecom_oauth_secret": oauth_secret,
        },
    )
    assert bootstrap.status_code == 200
    assert bootstrap.json() == {"ok": True}
    assert client.get("/api/admin/settings/bootstrap-status").json() == {"initialized": True}

    login = client.post(
        "/api/auth/password/login",
        json={"username": admin_username, "password": admin_password},
    )
    assert login.status_code == 200
    assert login.json() == {"logged_in": True}

    settings_page = client.get("/admin/settings")
    assert settings_page.status_code == 200
    assert "/web/static/settings.js?v=" in settings_page.text

    loaded = client.get("/api/admin/settings")
    assert loaded.status_code == 200
    fields = _settings_fields(loaded.json())
    assert fields["wecom_corp_id"] == {
        "key": "wecom_corp_id",
        "value": corp_id,
        "source": "db",
        "requires_restart": False,
    }
    assert fields["wecom_agent_id"] == {
        "key": "wecom_agent_id",
        "value": original_agent_id,
        "source": "db",
        "requires_restart": False,
    }
    assert fields["wecom_oauth_secret"]["source"] == "db"
    assert fields["wecom_oauth_secret"]["value"] != oauth_secret
    assert oauth_secret not in loaded.text

    saved = client.put("/api/admin/settings", json={"updates": {"wecom_agent_id": updated_agent_id}})
    assert saved.status_code == 200
    assert saved.json() == {"ok": True, "restart_required_keys": []}
    assert _settings_fields(client.get("/api/admin/settings").json())["wecom_agent_id"]["value"] == updated_agent_id

    connection_check = client.post("/api/admin/settings/test-connection", json={"target": "wecom"})
    assert connection_check.status_code == 200
    assert connection_check.json() == {"ok": True, "reason": None}
    self_check.assert_called_once_with(corp_id, oauth_secret)
