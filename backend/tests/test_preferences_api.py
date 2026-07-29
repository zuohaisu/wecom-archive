"""RND-297 API coverage for persisted current-user UI preferences."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE
from app.db.models import AdminSession, AdminUser, Base, Tenant
from app.db.session import get_db
from app.main import app


@pytest.fixture()
def preferences_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, AdminUser.__table__, AdminSession.__table__],
    )
    db = Session(engine)
    try:
        tenant = Tenant(id="prefs-tenant", name="Preferences tenant", slug="prefs")
        user = AdminUser(
            id="prefs-user-a",
            tenant_id=tenant.id,
            wecom_user_id="prefs-user-a",
            name="User A",
        )
        other_user = AdminUser(
            id="prefs-user-b",
            tenant_id=tenant.id,
            wecom_user_id="prefs-user-b",
            name="User B",
        )
        db.add_all([tenant, user, other_user])
        db.add(
            AdminSession(
                id="prefs-session-a",
                tenant_id=tenant.id,
                admin_user_id=user.id,
                wecom_user_id=user.wecom_user_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        db.commit()

        def override_db():
            yield db

        old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = override_db
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, db
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_preferences_update_requires_a_valid_session() -> None:
    from unittest.mock import MagicMock

    old_overrides = app.dependency_overrides.copy()

    def no_session_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = no_session_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.put("/api/auth/me/preferences", json={"theme": "dark"})
        assert response.status_code == 401
        assert response.json() == {"detail": "not_authenticated"}
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)


def test_preferences_persist_and_are_returned_by_auth_me(preferences_client) -> None:
    client, db = preferences_client
    cookies = {SESSION_COOKIE: "prefs-session-a"}

    response = client.put(
        "/api/auth/me/preferences",
        cookies=cookies,
        json={"theme": "dark", "locale": "en"},
    )
    assert response.status_code == 200
    assert response.json() == {"theme": "dark", "locale": "en"}

    db.expire_all()
    user = db.get(AdminUser, "prefs-user-a")
    assert (user.ui_theme, user.ui_locale) == ("dark", "en")

    me = client.get("/api/auth/me", cookies=cookies)
    assert me.status_code == 200
    assert me.json()["theme"] == "dark"
    assert me.json()["locale"] == "en"


def test_preferences_partial_updates_and_rejects_invalid_or_foreign_fields(preferences_client) -> None:
    client, db = preferences_client
    cookies = {SESSION_COOKIE: "prefs-session-a"}

    partial = client.put(
        "/api/auth/me/preferences", cookies=cookies, json={"theme": "dark"}
    )
    assert partial.status_code == 200
    assert partial.json() == {"theme": "dark", "locale": "zh-CN"}

    for payload in (
        {"theme": "system"},
        {"locale": "fr"},
        {"theme": "light", "user_id": "prefs-user-b"},
    ):
        assert client.put("/api/auth/me/preferences", cookies=cookies, json=payload).status_code == 422

    db.expire_all()
    other_user = db.get(AdminUser, "prefs-user-b")
    assert (other_user.ui_theme, other_user.ui_locale) == ("light", "zh-CN")
