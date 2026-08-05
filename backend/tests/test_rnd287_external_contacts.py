"""RND-287 external-contact entity, WeCom client, and sync service tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.external_contacts import upsert_external_contact
from app.db.models import (
    ExternalContact,
    ExternalContactFollow,
    ExternalContactNicknameHistory,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    ExternalContact.__table__.create(engine)
    ExternalContactFollow.__table__.create(engine)
    ExternalContactNicknameHistory.__table__.create(engine)
    with Session(engine) as session:
        yield session


def test_external_contact_upsert_is_tenant_scoped_and_idempotent(db_session) -> None:
    first = upsert_external_contact(
        db_session, "tenant-a", "wm-ext", "客户 A", "公司 A", '["重点客户"]',
        "state", "staff-a", None, None,
    )
    upsert_external_contact(
        db_session, "tenant-b", "wm-ext", "客户 B", None, "[]", None, None, None, None,
    )
    db_session.commit()

    updated = upsert_external_contact(
        db_session, "tenant-a", "wm-ext", "", "新公司", '["A类"]', "new-state",
        "staff-a", None, 2,
    )
    db_session.commit()

    assert first.id == updated.id
    assert updated.name == "客户 A"  # blank API name never clobbers a useful one
    assert updated.company == "新公司"
    assert updated.message_count == 2
    assert db_session.query(ExternalContact).count() == 2


def test_external_contact_unique_constraint_is_tenant_scoped(db_session) -> None:
    db_session.add(ExternalContact(tenant_id="tenant-a", external_userid="wm-ext"))
    db_session.commit()
    db_session.add(ExternalContact(tenant_id="tenant-a", external_userid="wm-ext"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_interaction_stats_are_tenant_scoped(db_session) -> None:
    """Only the columns queried by the helper are needed for this SQLite test."""
    from app.services.external_contact_sync import _interaction_stats

    db_session.execute(text("""
        CREATE TABLE archive_messages (
            id INTEGER PRIMARY KEY, tenant_id VARCHAR(36), sender VARCHAR(64), created_at DATETIME
        )
    """))
    db_session.execute(text("""
        CREATE TABLE archive_message_recipients (
            id INTEGER PRIMARY KEY, message_id INTEGER, tenant_id VARCHAR(36), receiver_userid VARCHAR(64)
        )
    """))
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 1, 2, tzinfo=timezone.utc)
    db_session.execute(
        text("INSERT INTO archive_messages VALUES (1, 'tenant-a', 'wm-ext', :at)"),
        {"at": earlier},
    )
    db_session.execute(
        text("INSERT INTO archive_messages VALUES (2, 'tenant-a', 'staff', :at)"),
        {"at": later},
    )
    db_session.execute(
        text("INSERT INTO archive_messages VALUES (3, 'tenant-b', 'wm-ext', :at)"),
        {"at": later},
    )
    db_session.execute(
        text("INSERT INTO archive_message_recipients VALUES (1, 2, 'tenant-a', 'wm-ext')")
    )
    db_session.commit()

    last_interaction_at, message_count = _interaction_stats(db_session, "tenant-a", "wm-ext")
    assert message_count == 2
    assert last_interaction_at is not None
    assert last_interaction_at.replace(tzinfo=timezone.utc) == later


def test_batch_interaction_stats_preserve_sender_recipient_or_semantics(db_session) -> None:
    """A message with the contact on both sides is counted only once."""
    from app.services.external_contact_sync import _interaction_stats_for_external_contacts

    db_session.execute(text("""
        CREATE TABLE archive_messages (
            id INTEGER PRIMARY KEY, tenant_id VARCHAR(36), sender VARCHAR(64), created_at DATETIME
        )
    """))
    db_session.execute(text("""
        CREATE TABLE archive_message_recipients (
            id INTEGER PRIMARY KEY, message_id INTEGER, tenant_id VARCHAR(36), receiver_userid VARCHAR(64)
        )
    """))
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 1, 2, tzinfo=timezone.utc)
    db_session.execute(
        text("INSERT INTO archive_messages VALUES (1, 'tenant-a', 'wm-a', :at)"),
        {"at": earlier},
    )
    db_session.execute(
        text("INSERT INTO archive_messages VALUES (2, 'tenant-a', 'staff', :at)"),
        {"at": later},
    )
    db_session.execute(
        text("INSERT INTO archive_messages VALUES (3, 'tenant-a', 'wm-b', :at)"),
        {"at": later},
    )
    db_session.execute(
        text("INSERT INTO archive_messages VALUES (4, 'tenant-b', 'wm-a', :at)"),
        {"at": later},
    )
    db_session.execute(
        text("INSERT INTO archive_message_recipients VALUES (1, 1, 'tenant-a', 'wm-a')")
    )
    db_session.execute(
        text("INSERT INTO archive_message_recipients VALUES (2, 2, 'tenant-a', 'wm-a')")
    )
    db_session.commit()

    stats = _interaction_stats_for_external_contacts(
        db_session, "tenant-a", {"wm-a", "wm-b", "wm-missing"}
    )

    assert stats["wm-a"][1] == 2
    assert stats["wm-a"][0] is not None
    assert stats["wm-a"][0].replace(tzinfo=timezone.utc) == later
    assert stats["wm-b"][1] == 1
    assert "wm-missing" not in stats


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class _Client:
    def __init__(self, payload: dict, calls: list) -> None:
        self.payload = payload
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        self.calls.append((url, params))
        return _Response(self.payload)


def test_external_contact_client_parses_list_detail_and_tags(monkeypatch) -> None:
    from app import wecom_contacts

    calls: list = []
    payload = {"errcode": 0, "follow_user": ["staff-a"]}
    monkeypatch.setattr(
        wecom_contacts.httpx, "Client", lambda timeout=None: _Client(payload, calls)
    )
    assert wecom_contacts.list_follow_userids("token") == ["staff-a"]
    assert calls[0][0].endswith("externalcontact/get_follow_user_list")

    payload = {"errcode": 0, "external_userid": ["wm-ext"]}
    assert wecom_contacts.list_external_userids_by_user("token", "staff-a") == ["wm-ext"]
    assert calls[-1][1]["userid"] == "staff-a"

    payload = {"errcode": 0, "external_contact": {"name": "客户"}, "follow_user": []}
    assert wecom_contacts.get_external_contact("token", "wm-ext") == payload

    payload = {"errcode": 0, "tag_group": [{"tag": [{"id": "t1", "name": "重点客户"}]}]}
    assert wecom_contacts.get_corp_tag_list("token") == {"t1": "重点客户"}


def test_external_contact_client_failure_is_safe(monkeypatch) -> None:
    from app import wecom_contacts

    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(wecom_contacts.httpx, "Client", lambda timeout=None: _FailingClient())
    assert wecom_contacts.list_follow_userids("token") is None


def test_sync_writes_tag_names_owner_and_is_idempotent(db_session, monkeypatch) -> None:
    from app.services import external_contact_sync as sync

    batched_last_interaction = datetime(2026, 1, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(sync, "get_wecom_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(sync.wecom_contacts, "list_follow_userids", lambda token: ["staff-a"])
    monkeypatch.setattr(
        sync.wecom_contacts, "list_external_userids_by_user", lambda token, owner: ["wm-ext"]
    )
    monkeypatch.setattr(sync.wecom_contacts, "get_corp_tag_list", lambda token: {"t1": "重点客户"})
    monkeypatch.setattr(
        sync.wecom_contacts,
        "get_external_contact",
        lambda token, ext: {
            "external_contact": {"name": "昵称", "corp_name": "客户公司"},
            "follow_user": [{"userid": "staff-a", "remark": "备注名", "state": "source", "tags": ["t1"]}],
        },
    )
    monkeypatch.setattr(
        sync,
        "_interaction_stats_for_external_contacts",
        lambda *args: {"wm-ext": (batched_last_interaction, 3)},
    )
    monkeypatch.setattr(
        sync,
        "_interaction_stats",
        lambda *args: pytest.fail("full sync must use batched interaction stats"),
    )

    first = sync.sync_external_contacts(db_session, "tenant-a", "corp-a", "secret")
    db_session.commit()
    second = sync.sync_external_contacts(db_session, "tenant-a", "corp-a", "secret")
    db_session.commit()

    row = db_session.query(ExternalContact).one()
    assert (first.inserted, first.updated, first.failed) == (1, 0, 0)
    assert (second.inserted, second.updated, second.failed) == (0, 1, 0)
    assert row.tenant_id == "tenant-a"
    # RND-170 keeps the legacy compatibility label (employee remark) apart
    # from the customer's current real nickname and follow relationship.
    assert row.name == "备注名"
    assert row.current_nickname_normalized == "昵称"
    assert row.current_nickname_display == "昵称"
    assert row.message_count == 3
    assert row.last_interaction_at is not None
    assert row.last_interaction_at.replace(tzinfo=timezone.utc) == batched_last_interaction
    relation = db_session.query(ExternalContactFollow).one()
    assert relation.follow_userid == "staff-a"
    assert relation.remark_normalized == "备注名"
    assert row.owner_wecom_userid == "staff-a"
    assert json.loads(row.tags) == ["重点客户"]
