"""
Tests for RND-159 — Search API: contacts and messages.

Validates:
  - Contact search by name and wecom_userid
  - Message search by content_text
  - Tenant isolation for both search endpoints
  - Pagination for message search
  - Message search skips non-text, failed-decrypt, and revoked messages

Reuses the sqlite-backed schema/fixtures from test_reachability_audit.py.

Run (from backend/):
    pytest tests/test_search_api.py -v

# flake8: noqa: F811
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.db.models import Contact

from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    _insert_recipient,
    db,  # noqa: F401 — pytest fixture, must be imported to be discovered
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_app_overrides():
    """Guard against cross-test leakage of the module-level `app` singleton's
    dependency_overrides. Some tests override get_db/get_current_user and,
    depending on ordering, a leaked override can corrupt an unrelated test's
    request handling (the cause of the intermittent full-suite failure in
    test_search_messages_pagination). Clear before AND after every test."""
    from app.main import app

    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def _authed(app, db_session, tenant_id):
    """Override get_current_user + get_db to simulate an authenticated
    session for tenant_id against the real sqlite-backed db_session."""
    from app.auth import get_current_user
    from app.db.session import get_db

    def _db_gen():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), tenant_id)
    app.dependency_overrides[get_db] = _db_gen


def _insert_contact(db, wecom_userid: str, name: str, tenant_id: str = _TENANT_A) -> Contact:
    c = Contact(wecom_userid=wecom_userid, name=name, tenant_id=tenant_id)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ---------------------------------------------------------------------------
# Contact search
# ---------------------------------------------------------------------------


def test_search_contacts_by_name(client, db) -> None:
    from app.main import app

    _insert_contact(db, "zhangsan", "张三", _TENANT_A)
    _insert_contact(db, "lisi", "李四", _TENANT_A)
    _insert_contact(db, "wangwu", "王五", _TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=三")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["wecom_userid"] == "zhangsan"
    assert data[0]["display_name"] == "张三"
    assert data[0]["match_field"] == "name"


def test_search_contacts_by_wecom_userid(client, db) -> None:
    from app.main import app

    _insert_contact(db, "zhangsan_001", None, _TENANT_A)
    _insert_contact(db, "lisi_002", None, _TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=zhangsan")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    # Wecom userid match, display_name falls back to userid
    assert data[0]["match_field"] == "wecom_userid"


def test_search_contacts_name_ranked_first(client, db) -> None:
    """Name matches should appear before wecom_userid matches."""
    from app.main import app

    _insert_contact(db, "zhangsan", "张三", _TENANT_A)
    _insert_contact(db, "san", "三郎", _TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=san")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    # First result should be name match ("三郎"), then userid match ("zhangsan")
    assert data[0]["match_field"] == "name" or data[0]["display_name"] == "三郎"


def test_search_contacts_no_match(client, db) -> None:
    from app.main import app

    _insert_contact(db, "zhangsan", "张三", _TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=不存在的关键词")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data == []


def test_search_contacts_empty_query_returns_422(client, db) -> None:
    from app.main import app

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422


def test_search_contacts_tenant_isolation(client, db) -> None:
    from app.main import app

    _insert_contact(db, "zhangsan_tenant_a", "张三", _TENANT_A)
    _insert_contact(db, "lisi_tenant_b", "李四", _TENANT_B)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=李四")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data == []


# ---------------------------------------------------------------------------
# Message search
# ---------------------------------------------------------------------------


def test_search_messages_by_content(client, db) -> None:
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="今天天气很好", msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=天气")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["content_snippet"] == "今天天气很好"
    assert body["results"][0]["sender_display_name"] == "staff_a"


def test_search_messages_no_match(client, db) -> None:
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello world", msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=nonexistent")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 0


def test_search_messages_empty_query_returns_422(client, db) -> None:
    from app.main import app

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422


def test_search_messages_skips_non_text_types(client, db) -> None:
    from app.main import app

    msg_text = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello world", msgtime=100,
    )
    _insert_recipient(db, msg_text.id, "contact_a", tenant_id=_TENANT_A)

    # image message with the same keyword should not appear in results
    msg_img = _insert_message(
        db, msgtype="image", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello world", msgtime=200,
    )
    _insert_recipient(db, msg_img.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=hello")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    # Only text message should appear in results


def test_search_messages_skips_not_decrypted(client, db) -> None:
    from app.main import app

    msg_ok = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello world", msgtime=100,
        decrypt_status="success",
    )
    _insert_recipient(db, msg_ok.id, "contact_a", tenant_id=_TENANT_A)

    msg_pending = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello world", msgtime=200,
        decrypt_status="pending",
    )
    _insert_recipient(db, msg_pending.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=hello")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1


def test_search_messages_skips_revoked(client, db) -> None:
    from app.main import app

    msg_ok = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello world", msgtime=100,
    )
    _insert_recipient(db, msg_ok.id, "contact_a", tenant_id=_TENANT_A)

    msg_revoked = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello world", msgtime=200,
        is_revoked=True,
    )
    _insert_recipient(db, msg_revoked.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=hello")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1


def test_search_messages_pagination(client, db) -> None:
    import uuid

    from app.main import app

    # Defensive: no leaked app state from a prior test (the autouse fixture
    # also clears; this guards the intermittent full-suite-only failure).
    app.dependency_overrides.clear()

    # Unique keyword so this test can never match messages left behind by
    # another code path — isolates the query to exactly the rows we insert.
    kw = f"keyword_{uuid.uuid4().hex}"

    # Insert 5 messages with descending msgtime
    for i in range(5):
        msg = _insert_message(
            db, msgtype="text", sender="staff_a",
            tenant_id=_TENANT_A,
            content_text=f"message number {i} {kw}",
            msgtime=1000 - i,
        )
        _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    # Walk every page via the cursor and assert: exact total of 5, distinct
    # ids, and never any overlap between pages. This turns the previous
    # "occasional overlap" into a deterministic, fully-covered assertion.
    all_ids: list = []
    before = None
    _authed(app, db, _TENANT_A)
    try:
        for _ in range(5):
            url = f"/api/search/messages?q={kw}&limit=2"
            if before is not None:
                url += f"&before={before}"
            resp = client.get(url)
            assert resp.status_code == 200
            body = resp.json()
            page_ids = [r["msgid"] for r in body["results"]]
            assert not (set(page_ids) & set(all_ids)), (
                "pagination returned overlapping results"
            )
            all_ids.extend(page_ids)
            before = body["pagination"]["next_before"]
            if not body["pagination"]["has_older"]:
                break
    finally:
        app.dependency_overrides.clear()

    assert len(all_ids) == 5
    assert len(set(all_ids)) == 5


def test_search_messages_tenant_isolation(client, db) -> None:
    from app.main import app

    msg_a = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="tenant a secret", msgtime=100,
    )
    _insert_recipient(db, msg_a.id, "contact_a", tenant_id=_TENANT_A)

    msg_b = _insert_message(
        db, msgtype="text", sender="staff_b",
        tenant_id=_TENANT_B, content_text="tenant a secret", msgtime=100,
    )
    _insert_recipient(db, msg_b.id, "contact_b", tenant_id=_TENANT_B)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=secret")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    # Only tenant A's message should be visible
    assert len(body["results"]) == 1


def test_search_messages_snippet_with_context(client, db) -> None:
    from app.main import app

    # Message with content much longer than snippet window
    # Prefix needs >80 chars for start truncation; suffix needs >80 for end truncation
    long_text = "前缀" * 50 + "关键词" + "后缀" * 50
    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text=long_text, msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=关键词")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    snippet = body["results"][0]["content_snippet"]
    assert "关键词" in snippet
    # Should have ellipsis if truncated
    assert snippet.startswith("…") or not snippet.startswith("前")


def test_search_messages_malformed_cursor_returns_400(client, db) -> None:
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello world", msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=hello&before=invalid-cursor")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400


def test_search_messages_sender_display_name_resolution(client, db) -> None:
    """Verify that sender_display_name uses the contacts.name when available."""
    from app.main import app

    _insert_contact(db, "staff_a", "Alice", _TENANT_A)

    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello keyword", msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["sender_display_name"] == "Alice"


def test_search_messages_entity_navigation_fields(client, db) -> None:
    """Verify message search results include entity_id and entity_type."""
    from app.main import app
    from app.db.models import AdminUser

    # Create an admin user so staff_a is recognized as staff
    admin_user = AdminUser(
        id="admin-1", tenant_id=_TENANT_A, wecom_user_id="staff_a", name="Admin"
    )
    db.add(admin_user)
    db.commit()

    msg = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="hello keyword", msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    r = body["results"][0]
    assert r["entity_id"] == "staff_a"
    assert r["entity_type"] == "staff"


def test_search_messages_entity_navigation_fallback_contact(client, db) -> None:
    """When no staff participant, entity navigation falls back to contact."""
    from app.main import app

    msg = _insert_message(
        db, msgtype="text", sender="contact_x",
        tenant_id=_TENANT_A, content_text="hello keyword", msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_y", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    r = body["results"][0]
    assert r["entity_id"] is not None
    assert r["entity_type"] == "contact"


def test_search_contacts_includes_admin_user_names(client, db) -> None:
    """Verify contact search also finds AdminUser names."""
    from app.main import app
    from app.db.models import AdminUser

    _insert_contact(db, "contractor_a", None, _TENANT_A)

    admin_user = AdminUser(
        id="admin-search-1", tenant_id=_TENANT_A,
        wecom_user_id="staff_wangfang", name="员工王芳",
    )
    db.add(admin_user)
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=王芳")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["wecom_userid"] == "staff_wangfang"
    assert data[0]["display_name"] == "员工王芳"


def test_search_contacts_prevents_duplicates_with_admin_user(client, db) -> None:
    """If same userid appears in both Contact and AdminUser, deduplicate."""
    from app.main import app
    from app.db.models import AdminUser

    _insert_contact(db, "staff_duplicate", "原始名称", _TENANT_A)

    admin_user = AdminUser(
        id="admin-dedup-1", tenant_id=_TENANT_A,
        wecom_user_id="staff_duplicate", name="管理员名称",
    )
    db.add(admin_user)
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=staff_duplicate")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    # Should appear only once
    ids = [d["wecom_userid"] for d in data]
    assert ids.count("staff_duplicate") == 1


def test_search_messages_ilike_wildcard_percent_is_escaped(client, db) -> None:
    """ILIKE % wildcard in query should match literal %, not any string."""
    from app.main import app

    msg_matching = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="discount 50% off", msgtime=100,
    )
    _insert_recipient(db, msg_matching.id, "contact_a", tenant_id=_TENANT_A)

    msg_all = _insert_message(
        db, msgtype="text", sender="staff_a",
        tenant_id=_TENANT_A, content_text="match everything", msgtime=200,
    )
    _insert_recipient(db, msg_all.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        # Searching for "50%" should NOT match "match everything"
        resp = client.get("/api/search/messages?q=50%25")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert "50%" in body["results"][0]["content_snippet"]
