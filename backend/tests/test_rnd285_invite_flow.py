"""Focused offline coverage for RND-285 invitation flow."""

from __future__ import annotations

import logging
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import get_current_user, verify_password
from app.db.base import Base
from app.db.models import AdminUser, Tenant
from app.db.session import get_db
from app.routers.auth import _AcceptBody, _InviteBody, accept_invite, invite_user


def _session_with_inviter() -> tuple[Session, AdminUser, str]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Tenant.__table__, AdminUser.__table__])
    db = Session(engine)
    tenant = Tenant(id="tenant-1", slug="default", name="Default", is_active=True)
    inviter = AdminUser(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        wecom_user_id="inviter",
        email="inviter@example.com",
        role="admin",
        status="active",
    )
    db.add_all([tenant, inviter])
    db.commit()
    return db, inviter, tenant.id


def _invite(db: Session, inviter: AdminUser, tenant_id: str, **kwargs):
    body = _InviteBody(
        wecom_user_id="new-user",
        email="new@example.com",
        name="New User",
        role="compliance",
        **kwargs,
    )
    with patch("app.email.send_invite_email") as send_email:
        response = invite_user(body, (inviter, tenant_id), db)
    return response, send_email


def test_invite_creates_pending_user_and_sends_email() -> None:
    db, inviter, tenant_id = _session_with_inviter()
    try:
        response, send_email = _invite(db, inviter, tenant_id)
        user = db.query(AdminUser).filter(AdminUser.wecom_user_id == "new-user").one()
        assert response.body == b'{"ok":true}'
        assert user.status == "disabled"
        assert user.invite_status == "pending"
        assert user.invite_token
        assert user.invited_by == inviter.id
        assert user.tenant_id == tenant_id
        send_email.assert_called_once()
        assert user.invite_token in send_email.call_args.args[1]
    finally:
        db.close()


def test_duplicate_invite_resends_same_token() -> None:
    db, inviter, tenant_id = _session_with_inviter()
    try:
        _, first_send = _invite(db, inviter, tenant_id)
        first = db.query(AdminUser).filter(AdminUser.wecom_user_id == "new-user").one()
        first_token = first.invite_token
        _, second_send = _invite(db, inviter, tenant_id)
        assert db.query(AdminUser).filter(AdminUser.wecom_user_id == "new-user").count() == 1
        assert first.invite_token == first_token
        assert first_send.call_count == 1
        assert second_send.call_count == 1
        assert first_token in second_send.call_args.args[1]
    finally:
        db.close()


def test_accept_sets_password_and_activates() -> None:
    db, inviter, tenant_id = _session_with_inviter()
    try:
        _invite(db, inviter, tenant_id)
        user = db.query(AdminUser).filter(AdminUser.wecom_user_id == "new-user").one()
        response = accept_invite(
            _AcceptBody(token=user.invite_token, password="longenough", name="Accepted User"), db
        )
        db.refresh(user)
        assert response.body == b'{"ok":true}'
        assert user.status == "active"
        assert user.invite_status == "accepted"
        assert user.name == "Accepted User"
        assert user.password_hash
        assert verify_password("longenough", user.password_hash)
        assert user.invite_token.encode() not in response.body
    finally:
        db.close()


def test_accept_rejects_invalid_token() -> None:
    db, _, _ = _session_with_inviter()
    try:
        with pytest.raises(HTTPException, match="invalid_or_expired_token") as exc_info:
            accept_invite(_AcceptBody(token="bogus", password="longenough"), db)
        assert exc_info.value.status_code == 400
    finally:
        db.close()


def test_accept_rejects_weak_password() -> None:
    db, inviter, tenant_id = _session_with_inviter()
    try:
        _invite(db, inviter, tenant_id)
        user = db.query(AdminUser).filter(AdminUser.wecom_user_id == "new-user").one()
        with pytest.raises(HTTPException, match="weak_password") as exc_info:
            accept_invite(_AcceptBody(token=user.invite_token, password="short"), db)
        assert exc_info.value.status_code == 400
    finally:
        db.close()


def test_invite_requires_auth() -> None:
    from app.main import app
    from fastapi.testclient import TestClient

    db, _, _ = _session_with_inviter()

    def deny() -> None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = deny
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/admin/users/invite",
                json={"wecom_user_id": "new-user", "role": "admin"},
            )
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_unconfigured_email_fails_closed_without_logging_token(caplog) -> None:
    from app.email import send_invite_email
    from app.settings import EmailSettings

    caplog.set_level(logging.WARNING, logger="app.email")
    with patch(
        "app.email.get_email_settings",
        return_value=EmailSettings(resend_api_key=""),
    ):
        assert not send_invite_email("person@example.com", "https://example.test/?token=secret")
    assert "configuration_incomplete" in caplog.text
    assert "person@example.com" not in caplog.text
    assert "secret" not in caplog.text


def test_invite_settings_use_invite_base_url_env(monkeypatch) -> None:
    from app.settings import get_email_settings

    monkeypatch.setenv("INVITE_BASE_URL", "https://archive.example.test")
    assert get_email_settings().invite_base_url == "https://archive.example.test"
