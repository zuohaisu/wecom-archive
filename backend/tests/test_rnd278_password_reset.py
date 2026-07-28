"""Focused offline coverage for RND-278 password reset."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import consume_password_reset_token, create_password_reset_token
from app.db.base import Base
from app.db.models import AdminUser, PasswordResetToken, Tenant


def _session_with_user() -> tuple[Session, AdminUser]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, AdminUser.__table__, PasswordResetToken.__table__],
    )
    db = Session(engine)
    tenant = Tenant(id="tenant-1", slug="default", name="Default", is_active=True)
    user = AdminUser(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        wecom_user_id="rnd278-user",
        email="person@example.com",
        password_hash="old-hash",
        role="admin",
        status="active",
    )
    db.add_all([tenant, user])
    db.commit()
    return db, user


def test_reset_tokens_persist_only_sha256_and_are_single_use() -> None:
    db, user = _session_with_user()
    try:
        raw = create_password_reset_token(db, user)
        db.commit()
        row = db.query(PasswordResetToken).one()
        assert row.token != raw
        assert len(row.token) == 64
        assert consume_password_reset_token(db, raw) == (user, row)

        row.used = True
        db.commit()
        assert consume_password_reset_token(db, raw) is None
    finally:
        db.close()


def test_new_reset_request_invalidates_prior_unused_token() -> None:
    db, user = _session_with_user()
    try:
        first = create_password_reset_token(db, user)
        second = create_password_reset_token(db, user)
        db.commit()
        assert consume_password_reset_token(db, first) is None
        assert consume_password_reset_token(db, second) is not None
    finally:
        db.close()


def test_expired_reset_token_is_rejected_and_marked_used() -> None:
    db, user = _session_with_user()
    try:
        raw = create_password_reset_token(db, user)
        row = db.query(PasswordResetToken).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert consume_password_reset_token(db, raw) is None
        assert row.used is True
    finally:
        db.close()


def test_reset_endpoint_hashes_password_and_never_returns_token() -> None:
    from app.routers.auth import _ResetBody, password_reset

    user = MagicMock(password_hash="old")
    row = MagicMock(used=False)
    db = MagicMock()
    with patch("app.auth.consume_password_reset_token", return_value=(user, row)), patch(
        "app.auth.hash_password", return_value="new-hash"
    ):
        response = password_reset(_ResetBody(token="secret-token", password="new-password"), db)
    assert response.body == b'{"ok":true}'
    assert user.password_hash == "new-hash"
    assert row.used is True
    db.commit.assert_called_once()
    assert b"secret-token" not in response.body


def test_console_email_transport_does_not_log_reset_link(caplog) -> None:
    from app.email import send_password_reset_email

    caplog.set_level(logging.WARNING, logger="app.email")
    with patch("app.email.get_email_settings", return_value=MagicMock(smtp_host="", smtp_from="")):
        assert send_password_reset_email("person@example.com", "https://example.test/?token=secret")
    assert "person@example.com" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("path, marker", [
    ("/admin/forgot-password", 'id="forgot-form"'),
    ("/admin/reset-password", 'id="reset-form"'),
])
def test_password_recovery_pages_are_public_ssr_pages(path: str, marker: str) -> None:
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert marker in response.text
    assert "/web/static/styles.css" in response.text


def test_email_settings_use_password_reset_environment_names(monkeypatch) -> None:
    from app.settings import get_email_settings

    monkeypatch.setenv("PASSWORD_RESET_BASE_URL", "https://archive.example.test")
    monkeypatch.setenv("PASSWORD_RESET_TOKEN_TTL_HOURS", "2")
    settings = get_email_settings()
    assert settings.reset_base_url == "https://archive.example.test"
    assert settings.reset_token_ttl_hours == "2"
