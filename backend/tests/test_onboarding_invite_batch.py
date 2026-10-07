"""Offline coverage for RND-303 onboarding batch invitations."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db.base import Base
from app.db.models import AdminUser, Tenant
from app.db.session import get_db


@pytest.fixture()
def invitation_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Tenant.__table__, AdminUser.__table__])
    db = Session(engine)
    tenant = Tenant(id="session-tenant", slug="default", name="Default")
    inviter = AdminUser(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        wecom_user_id="inviter",
        email="inviter@example.test",
        role="admin",
        status="active",
    )
    db.add_all([tenant, inviter])
    db.commit()

    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: (inviter, tenant.id)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, db, tenant.id
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_batch_creates_each_pending_user_sends_email_and_uses_session_tenant(
    invitation_client,
) -> None:
    client, db, tenant_id = invitation_client
    with patch("app.email.send_invite_email") as send_email:
        response = client.post(
            "/api/admin/users/invite-batch",
            json={
                "tenant_id": "attacker-tenant",
                "invites": [
                    {"email": "compliance@example.test", "name": "Compliance", "role": "compliance"},
                    {"email": "legal@example.test", "role": "legal"},
                    {"email": "audit@example.test", "role": "readonlyaudit"},
                ],
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"email": "compliance@example.test", "ok": True},
            {"email": "legal@example.test", "ok": True},
            {"email": "audit@example.test", "ok": True},
        ]
    }
    users = db.query(AdminUser).filter(AdminUser.wecom_user_id.like("invited:%")).all()
    assert len(users) == 3
    assert all(user.tenant_id == tenant_id for user in users)
    assert all(user.status == "disabled" and user.invite_status == "pending" for user in users)
    assert send_email.call_count == 3


def test_batch_isolates_invalid_role_and_continues(invitation_client) -> None:
    client, db, _ = invitation_client
    with patch("app.email.send_invite_email") as send_email:
        response = client.post(
            "/api/admin/users/invite-batch",
            json={
                "invites": [
                    {"email": "valid-before@example.test", "role": "compliance"},
                    {"email": "invalid@example.test", "role": "not-a-role"},
                    {"email": "valid-after@example.test", "role": "legal"},
                ]
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"email": "valid-before@example.test", "ok": True},
            {"email": "invalid@example.test", "ok": False, "error": "invalid_role"},
            {"email": "valid-after@example.test", "ok": True},
        ]
    }
    assert db.query(AdminUser).filter(AdminUser.wecom_user_id.like("invited:%")).count() == 2
    assert send_email.call_count == 2


def test_batch_rejects_more_than_twenty_invitations(invitation_client) -> None:
    client, db, _ = invitation_client
    response = client.post(
        "/api/admin/users/invite-batch",
        json={"invites": [{"email": f"user-{index}@example.test", "role": "compliance"} for index in range(21)]},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invite_batch_limit_exceeded"}
    assert db.query(AdminUser).filter(AdminUser.wecom_user_id.like("invited:%")).count() == 0


def test_batch_requires_the_existing_session_authentication() -> None:
    from app.main import app

    def deny() -> None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    app.dependency_overrides[get_current_user] = deny
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/admin/users/invite-batch",
                json={"invites": [{"email": "person@example.test", "role": "compliance"}]},
            )
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()
