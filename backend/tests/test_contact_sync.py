"""
Tests for RND-130 — contact display-name sync/enrichment.

Validates:
  - upsert_contact_display_name: create, update, blank no-op, tenant isolation.
  - wecom_contacts metadata client: member + external contact lookups, with
    mocked HTTP responses (no live WeCom API required).
  - sync_contact_display_names_once._resolve_display_name: member-first
    precedence and safe handling of unresolved metadata.
  - /api/contacts still requires auth and returns display_name from
    contacts.name (falling back to the raw ID).

Run (from backend/):
    pytest tests/test_contact_sync.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.contacts import upsert_contact_display_name
from app.db.models import Contact


# ---------------------------------------------------------------------------
# upsert_contact_display_name — sqlite-backed, no DATABASE_URL required
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Contact.__table__.create(engine)
    with Session(engine) as session:
        yield session


def test_upsert_creates_contact_with_tenant_id(db_session) -> None:
    contact = upsert_contact_display_name(db_session, "tenant-a", "zhangsan", "张三")
    db_session.commit()

    assert contact is not None
    assert contact.tenant_id == "tenant-a"
    assert contact.wecom_userid == "zhangsan"
    assert contact.name == "张三"

    row = (
        db_session.query(Contact)
        .filter(Contact.tenant_id == "tenant-a", Contact.wecom_userid == "zhangsan")
        .first()
    )
    assert row is not None
    assert row.name == "张三"


def test_upsert_updates_name_when_changed(db_session) -> None:
    upsert_contact_display_name(db_session, "tenant-a", "lisi", "Old Name")
    db_session.commit()

    upsert_contact_display_name(db_session, "tenant-a", "lisi", "New Name")
    db_session.commit()

    row = (
        db_session.query(Contact)
        .filter(Contact.tenant_id == "tenant-a", Contact.wecom_userid == "lisi")
        .first()
    )
    assert row.name == "New Name"


def test_upsert_does_not_overwrite_existing_name_with_blank(db_session) -> None:
    upsert_contact_display_name(db_session, "tenant-a", "wangwu", "王五")
    db_session.commit()

    upsert_contact_display_name(db_session, "tenant-a", "wangwu", "")
    upsert_contact_display_name(db_session, "tenant-a", "wangwu", None)
    db_session.commit()

    row = (
        db_session.query(Contact)
        .filter(Contact.tenant_id == "tenant-a", Contact.wecom_userid == "wangwu")
        .first()
    )
    assert row.name == "王五"


def test_upsert_blank_name_does_not_create_a_row(db_session) -> None:
    result = upsert_contact_display_name(db_session, "tenant-a", "ghost", "")
    db_session.commit()

    assert result is None
    row = (
        db_session.query(Contact)
        .filter(Contact.tenant_id == "tenant-a", Contact.wecom_userid == "ghost")
        .first()
    )
    assert row is None


def test_upsert_is_tenant_isolated(db_session) -> None:
    upsert_contact_display_name(db_session, "tenant-a", "shared_id", "Tenant A Name")
    upsert_contact_display_name(db_session, "tenant-b", "shared_id", "Tenant B Name")
    db_session.commit()

    row_a = (
        db_session.query(Contact)
        .filter(Contact.tenant_id == "tenant-a", Contact.wecom_userid == "shared_id")
        .first()
    )
    row_b = (
        db_session.query(Contact)
        .filter(Contact.tenant_id == "tenant-b", Contact.wecom_userid == "shared_id")
        .first()
    )
    assert row_a is not None and row_b is not None
    assert row_a.name == "Tenant A Name"
    assert row_b.name == "Tenant B Name"
    assert row_a.id != row_b.id


# ---------------------------------------------------------------------------
# wecom_contacts metadata client — mocked HTTP, no live WeCom API
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def get(self, url, params=None) -> _FakeResponse:
        return _FakeResponse(self._payload)


def _patch_httpx_client(monkeypatch, payload: dict) -> None:
    from app import wecom_contacts

    monkeypatch.setattr(
        wecom_contacts.httpx, "Client", lambda timeout=None: _FakeClient(payload)
    )


def test_fetch_member_display_name_success(monkeypatch) -> None:
    from app.wecom_contacts import fetch_member_display_name

    _patch_httpx_client(monkeypatch, {"errcode": 0, "name": "张三"})
    assert fetch_member_display_name("token", "zhangsan") == "张三"


def test_fetch_member_display_name_not_found_returns_none(monkeypatch) -> None:
    from app.wecom_contacts import fetch_member_display_name

    _patch_httpx_client(monkeypatch, {"errcode": 60111, "errmsg": "userid not found"})
    assert fetch_member_display_name("token", "not_a_member") is None


def test_fetch_member_display_name_handles_request_failure(monkeypatch) -> None:
    from app import wecom_contacts

    class _RaisingClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(wecom_contacts.httpx, "Client", lambda timeout=None: _RaisingClient())
    assert wecom_contacts.fetch_member_display_name("token", "zhangsan") is None


def test_fetch_external_contact_display_name_prefers_remark(monkeypatch) -> None:
    from app.wecom_contacts import fetch_external_contact_display_name

    _patch_httpx_client(
        monkeypatch,
        {
            "errcode": 0,
            "external_contact": {"name": "Nickname"},
            "follow_user": [{"remark": "备注名"}],
        },
    )
    assert fetch_external_contact_display_name("token", "wm_ext_001") == "备注名"


def test_fetch_external_contact_display_name_falls_back_to_nickname(monkeypatch) -> None:
    from app.wecom_contacts import fetch_external_contact_display_name

    _patch_httpx_client(
        monkeypatch,
        {
            "errcode": 0,
            "external_contact": {"name": "Nickname"},
            "follow_user": [{"remark": ""}],
        },
    )
    assert fetch_external_contact_display_name("token", "wm_ext_001") == "Nickname"


def test_fetch_external_contact_display_name_handles_missing_metadata(monkeypatch) -> None:
    from app.wecom_contacts import fetch_external_contact_display_name

    _patch_httpx_client(monkeypatch, {"errcode": 40096, "errmsg": "not found"})
    assert fetch_external_contact_display_name("token", "unknown") is None


# ---------------------------------------------------------------------------
# sync_contact_display_names_once._resolve_display_name
# ---------------------------------------------------------------------------


def _load_sync_script_module():
    scripts_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import sync_contact_display_names_once as mod

    return mod


def test_resolve_display_name_prefers_member_over_external(monkeypatch) -> None:
    mod = _load_sync_script_module()
    monkeypatch.setattr(mod, "fetch_member_display_name", lambda token, uid: "内部员工")
    monkeypatch.setattr(
        mod, "fetch_external_contact_display_name", lambda token, uid: "不应使用"
    )

    name, source = mod._resolve_display_name("some_id", "member_token", "external_token")
    assert (name, source) == ("内部员工", "member")


def test_resolve_display_name_falls_back_to_external(monkeypatch) -> None:
    mod = _load_sync_script_module()
    monkeypatch.setattr(mod, "fetch_member_display_name", lambda token, uid: None)
    monkeypatch.setattr(mod, "fetch_external_contact_display_name", lambda token, uid: "外部客户")

    name, source = mod._resolve_display_name("some_id", "member_token", "external_token")
    assert (name, source) == ("外部客户", "external")


def test_resolve_display_name_skips_external_lookup_when_token_absent(monkeypatch) -> None:
    mod = _load_sync_script_module()
    monkeypatch.setattr(mod, "fetch_member_display_name", lambda token, uid: None)

    def _fail_if_called(token, uid):
        raise AssertionError("external lookup must not run without a token")

    monkeypatch.setattr(mod, "fetch_external_contact_display_name", _fail_if_called)

    name, source = mod._resolve_display_name("some_id", "member_token", None)
    assert (name, source) == (None, "unresolved")


def test_resolve_display_name_handles_missing_metadata_safely(monkeypatch) -> None:
    """Neither API has a name for this ID — must resolve safely, not raise."""
    mod = _load_sync_script_module()
    monkeypatch.setattr(mod, "fetch_member_display_name", lambda token, uid: None)
    monkeypatch.setattr(mod, "fetch_external_contact_display_name", lambda token, uid: None)

    name, source = mod._resolve_display_name("ghost_id", "member_token", "external_token")
    assert (name, source) == (None, "unresolved")


def test_collect_participant_ids_skips_blank_and_dedupes() -> None:
    mod = _load_sync_script_module()

    from app.db.models import ArchiveMessage, ArchiveMessageRecipient

    session = MagicMock()

    sender_q = MagicMock()
    sender_q.filter.return_value = sender_q
    sender_q.distinct.return_value = sender_q
    sender_q.all.return_value = [("zhangsan",), ("  ",), ("staff_yingzi",)]

    recipient_q = MagicMock()
    recipient_q.filter.return_value = recipient_q
    recipient_q.distinct.return_value = recipient_q
    recipient_q.all.return_value = [("zhangsan",), ("staff_yingzi",), (None,)]

    def _query(col):
        key = getattr(col, "key", None)
        if key == "sender":
            return sender_q
        if key == "receiver_userid":
            return recipient_q
        raise AssertionError(f"unexpected query target: {col}")

    session.query.side_effect = _query

    ids = mod._collect_participant_ids(session, "tenant-a")
    assert ids == ["staff_yingzi", "zhangsan"]


# ---------------------------------------------------------------------------
# /api/contacts — auth still required; display_name resolved from Contact.name
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_contacts_still_requires_auth(client) -> None:
    from app.db.session import get_db
    from app.main import app

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[get_db] = lambda: iter([mock_db])
    try:
        resp = client.get("/api/contacts")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_get_contacts_returns_display_name_from_contact_name(client) -> None:
    from app.auth import get_current_user
    from app.db.models import AdminUser, ArchiveMessage, ArchiveMessageRecipient, Contact
    from app.db.session import get_db
    from app.main import app

    contact_row = MagicMock()
    contact_row.wecom_userid = "contact_zhangsan"
    contact_row.name = "张三"

    def _override_db():
        mock = MagicMock()

        sender_q = MagicMock()
        sender_q.filter.return_value = sender_q
        sender_q.distinct.return_value = sender_q
        sender_q.all.return_value = [("contact_zhangsan",)]

        recipient_q = MagicMock()
        recipient_q.filter.return_value = recipient_q
        recipient_q.distinct.return_value = recipient_q
        recipient_q.all.return_value = [("contact_wangwu",)]

        contact_q = MagicMock()
        contact_q.filter.return_value = contact_q
        contact_q.all.return_value = [contact_row]

        # No admin_users rows for this tenant — _collect_staff_ids() falls
        # back to the "staff_" prefix signal alone (see RND-132), which
        # finds nothing among these fixture IDs.
        admin_user_q = MagicMock()
        admin_user_q.filter.return_value = admin_user_q
        admin_user_q.distinct.return_value = admin_user_q
        admin_user_q.all.return_value = []

        def _query(target):
            key = getattr(target, "key", None)
            if key == "sender":
                return sender_q
            if key == "receiver_userid":
                return recipient_q
            if target is Contact:
                return contact_q
            if key == "wecom_user_id" or target is AdminUser:
                return admin_user_q
            raise AssertionError(f"unexpected query target: {target}")

        mock.query.side_effect = _query
        yield mock

    mock_user = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: (mock_user, "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    try:
        resp = client.get("/api/contacts")
        assert resp.status_code == 200
        by_id = {row["contact_id"]: row["display_name"] for row in resp.json()}
        # Known contact resolves to the stored name.
        assert by_id["contact_zhangsan"] == "张三"
        # Unknown contact falls back to the raw ID.
        assert by_id["contact_wangwu"] == "contact_wangwu"
    finally:
        app.dependency_overrides.clear()
