"""Contracts for the tenant-scoped external-contact detail API (RND-289)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact, ExternalContact


@pytest.fixture()
def db() -> Session:
    from tests.test_http_contract import _make_session

    session = _make_session()
    session.execute(
        text(
            "CREATE TABLE external_contacts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, external_userid TEXT NOT NULL, "
            "name TEXT, company TEXT, tags TEXT, source TEXT, "
            "owner_wecom_userid TEXT, last_interaction_at TEXT, message_count INTEGER, "
            "tenant_id TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
    )
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def client(db: Session):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    def override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (
        SimpleNamespace(role="admin"),
        "tenant-a",
    )
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def _external_contact(
    db: Session, external_userid: str, *, tenant_id: str = "tenant-a"
) -> ExternalContact:
    contact = ExternalContact(
        tenant_id=tenant_id,
        external_userid=external_userid,
        name="Example Customer",
        company="Example Co",
        tags=json.dumps(["VIP", "priority"]),
        source="campaign",
        owner_wecom_userid="staff-owner",
        message_count=3,
    )
    db.add(contact)
    db.flush()
    return contact


def _message(
    db: Session,
    *,
    msgid: str,
    sender: str,
    msgtime: int,
    roomid: str | None = None,
    tenant_id: str = "tenant-a",
) -> ArchiveMessage:
    message = ArchiveMessage(
        msgid=msgid,
        seq=1,
        publickey_ver=1,
        encrypt_random_key="test-key",
        encrypt_chat_msg="test-message",
        decrypt_status="success",
        msgtype="text",
        sender=sender,
        roomid=roomid,
        msgtime=msgtime,
        content_text="fixed test message",
        tenant_id=tenant_id,
    )
    db.add(message)
    db.flush()
    return message


def _recipient(
    db: Session, message: ArchiveMessage, userid: str, *, tenant_id: str = "tenant-a"
) -> None:
    db.add(
        ArchiveMessageRecipient(
            message_id=message.id,
            receiver_userid=userid,
            tenant_id=tenant_id,
        )
    )
    db.flush()


def test_detail_returns_profile_and_all_conversations(client: TestClient, db: Session) -> None:
    db.add(Contact(tenant_id="tenant-a", wecom_userid="staff-owner", name="Owner Name"))
    _external_contact(db, "external-customer")
    direct = _message(
        db, msgid="direct-message", sender="staff-owner", msgtime=100
    )
    _recipient(db, direct, "external-customer")
    group = _message(
        db,
        msgid="group-message",
        sender="staff-other",
        roomid="customer-group",
        msgtime=200,
    )
    _recipient(db, group, "external-customer")
    db.commit()

    response = client.get("/api/admin/external-contacts/external-customer")

    assert response.status_code == 200
    body = response.json()
    assert body["external_userid"] == "external-customer"
    assert body["name"] == "Example Customer"
    assert body["company"] == "Example Co"
    assert body["tags"] == ["VIP", "priority"]
    assert body["owner_wecom_userid"] == "staff-owner"
    assert body["owner_display_name"] == "Owner Name"
    assert body["owner_display_name"] != "tenant-a"
    assert body["message_count"] == 3
    assert "messages" not in body
    assert [conversation["conversation_id"] for conversation in body["conversations"]] == [
        "customer-group",
        "direct__external-customer___staff-owner",
    ]
    assert [conversation["last_message_time"] for conversation in body["conversations"]] == [
        200,
        100,
    ]


def test_detail_returns_empty_conversations_for_contact_without_messages(
    client: TestClient, db: Session
) -> None:
    _external_contact(db, "external-no-messages")
    db.commit()

    response = client.get("/api/admin/external-contacts/external-no-messages")

    assert response.status_code == 200
    assert response.json()["conversations"] == []


def test_detail_hides_missing_and_other_tenant_contacts(client: TestClient, db: Session) -> None:
    _external_contact(db, "external-other-tenant", tenant_id="tenant-b")
    db.commit()

    missing = client.get("/api/admin/external-contacts/does-not-exist")
    cross_tenant = client.get("/api/admin/external-contacts/external-other-tenant")

    assert missing.status_code == 404
    assert cross_tenant.status_code == 404
    assert cross_tenant.json() == missing.json() == {"detail": "External contact not found"}
