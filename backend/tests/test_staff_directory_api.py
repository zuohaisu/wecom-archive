"""Internal-staff directory API coverage (Haisu split request + follow-up).

Staff identity is the complement of the authoritative external registry:
every archive participant is internal staff unless WeCom's external-contact
sync registered them in ``external_contacts`` — the prefix/seat heuristics
no longer gate the listing. Counts mirror the RND-284 sent-only rule. Uses
a SQLite-compatible hand schema like test_dashboard_api.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

_SCHEMA_SQL = """
CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT, slug TEXT);
CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, wecom_userid TEXT NOT NULL, name TEXT, tenant_id TEXT, avatar_storage_backend TEXT, avatar_storage_ref TEXT, avatar_content_type TEXT, avatar_source TEXT, avatar_synced_at TEXT, avatar_status TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE admin_users (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, wecom_user_id TEXT NOT NULL, name TEXT, avatar_url TEXT, last_login_at TEXT, password_hash TEXT, role TEXT NOT NULL DEFAULT 'admin', status TEXT NOT NULL DEFAULT 'active', email TEXT, phone TEXT, department TEXT, last_active_at TEXT, invite_token TEXT, invited_by TEXT, invite_status TEXT, ui_theme TEXT NOT NULL DEFAULT 'light', ui_locale TEXT NOT NULL DEFAULT 'zh-CN', created_at TEXT, updated_at TEXT);
CREATE TABLE archive_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, msgid TEXT NOT NULL, seq INTEGER NOT NULL, publickey_ver INTEGER NOT NULL, encrypt_random_key TEXT NOT NULL, encrypt_chat_msg TEXT NOT NULL, decrypt_status TEXT NOT NULL, content_text TEXT, msgtype TEXT, sender TEXT, roomid TEXT, msgtime INTEGER, tenant_id TEXT, created_at TEXT);
CREATE TABLE archive_message_recipients (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, receiver_userid TEXT NOT NULL, receiver_type TEXT, tenant_id TEXT, created_at TEXT);
CREATE TABLE external_contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, external_userid TEXT NOT NULL, tenant_id TEXT, name TEXT, position TEXT, corporation_name TEXT, created_at TEXT, updated_at TEXT);
"""
TENANT_A, TENANT_B = "tenant-a", "tenant-b"


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as conn:
        for statement in _SCHEMA_SQL.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
    return Session(engine)


@pytest.fixture()
def db():
    session = _session()
    yield session
    session.close()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _authenticated_app(db: Session, tenant_id: str = TENANT_A):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    user = SimpleNamespace(role="admin")
    app.dependency_overrides[get_current_user] = lambda: (user, tenant_id)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return app


def _message(db: Session, sender: str, tenant_id: str, days_ago: int, msgid: str):
    db.execute(
        text(
            "INSERT INTO archive_messages (msgid, seq, publickey_ver, encrypt_random_key,"
            " encrypt_chat_msg, decrypt_status, sender, msgtime, tenant_id)"
            " VALUES (:msgid, 1, 1, 'k', 'c', 'success', :sender, :msgtime, :tenant)"
        ),
        {
            "msgid": msgid,
            "sender": sender,
            "msgtime": int(
                (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()
                * 1000
            ),
            "tenant": tenant_id,
        },
    )


@pytest.fixture()
def seeded(db: Session):
    db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES ('tenant-a','A','a'), ('tenant-b','B','b')")
    )
    # guest_carol is the only externally registered participant: WeCom's
    # external-contact sync recorded her, so she stays off the staff page.
    db.execute(
        text("INSERT INTO external_contacts (external_userid, tenant_id, name) VALUES ('guest_carol', 'tenant-a', 'Carol')")
    )
    for userid, name, tenant in (
        ("staff_alice", "Alice", TENANT_A),
        ("staff_bob", "Bob", TENANT_A),
        ("guest_carol", "Carol", TENANT_A),
        ("staff_zoe", "Zoe", TENANT_B),
    ):
        db.execute(
            text("INSERT INTO contacts (wecom_userid, name, tenant_id) VALUES (:u, :n, :t)"),
            {"u": userid, "n": name, "t": tenant},
        )
    db.execute(
        text(
            "INSERT INTO admin_users (id, tenant_id, wecom_user_id, name, role, department)"
            " VALUES ('seat-1', 'tenant-a', 'staff_alice', 'Alice', 'admin', '客服部')"
        )
    )
    _message(db, "staff_alice", TENANT_A, 1, "a-1")
    _message(db, "staff_alice", TENANT_A, 2, "a-2")
    _message(db, "staff_alice", TENANT_A, 40, "a-3")
    _message(db, "staff_bob", TENANT_A, 3, "a-4")
    _message(db, "guest_carol", TENANT_A, 1, "a-5")
    _message(db, "staff_zoe", TENANT_B, 1, "b-1")
    db.commit()
    return db


def test_staff_listing_returns_staff_only_with_readonly_stats(client, seeded) -> None:
    app = _authenticated_app(seeded)
    try:
        response = client.get("/api/admin/staff")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert [item["wecom_userid"] for item in data["items"]] == ["staff_alice", "staff_bob"]

    alice, bob = data["items"]
    assert alice["name"] == "Alice"
    assert alice["department"] == "客服部"
    assert alice["has_seat"] is True
    assert alice["msg_count_30d"] == 2
    assert alice["msg_count_total"] == 3
    assert bob["has_seat"] is False
    assert bob["department"] is None
    assert bob["msg_count_30d"] == 1
    assert bob["msg_count_total"] == 1
    # guest_carol appears in the archive but is a registered external contact.
    assert all(item["wecom_userid"] != "guest_carol" for item in data["items"])
    assert "content_text" not in response.text


def test_staff_listing_searches_name_and_userid(client, seeded) -> None:
    app = _authenticated_app(seeded)
    try:
        by_name = client.get("/api/admin/staff?q=Alice").json()
        by_id = client.get("/api/admin/staff?q=bob").json()
        miss = client.get("/api/admin/staff?q=carol").json()
    finally:
        app.dependency_overrides.clear()

    assert [item["wecom_userid"] for item in by_name["items"]] == ["staff_alice"]
    assert [item["wecom_userid"] for item in by_id["items"]] == ["staff_bob"]
    assert miss["items"] == [] and miss["total"] == 0


def test_staff_listing_is_tenant_scoped(client, seeded) -> None:
    app = _authenticated_app(seeded, TENANT_B)
    try:
        response = client.get("/api/admin/staff")
    finally:
        app.dependency_overrides.clear()

    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["wecom_userid"] == "staff_zoe"
    assert data["items"][0]["msg_count_30d"] == 1


def test_staff_listing_requires_auth(client, seeded) -> None:
    from app.db.session import get_db
    from app.main import app

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    try:
        assert client.get("/api/admin/staff").status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_staff_listing_paginates(client, seeded) -> None:
    app = _authenticated_app(seeded)
    try:
        page1 = client.get("/api/admin/staff?page=1&per_page=1").json()
        page2 = client.get("/api/admin/staff?page=2&per_page=1").json()
    finally:
        app.dependency_overrides.clear()

    assert page1["total"] == 2 and len(page1["items"]) == 1
    assert page1["items"][0]["wecom_userid"] == "staff_alice"
    assert page2["items"][0]["wecom_userid"] == "staff_bob"


def test_staff_listing_includes_unregistered_non_prefix_participants(client, seeded) -> None:
    """Haisu follow-up: EVERY internal staff member who appeared belongs on
    the page — the staff_ prefix and seat linkage must not gate inclusion.
    An unregistered participant is internal by default."""
    _message(seeded, "ops_dave", TENANT_A, 1, "a-dave-1")
    seeded.commit()
    app = _authenticated_app(seeded)
    try:
        response = client.get("/api/admin/staff")
    finally:
        app.dependency_overrides.clear()

    data = response.json()
    ids = [item["wecom_userid"] for item in data["items"]]
    assert "ops_dave" in ids
    dave = next(item for item in data["items"] if item["wecom_userid"] == "ops_dave")
    assert dave["has_seat"] is False
    assert dave["msg_count_30d"] == 1
    # 注册过的外部联系人依然被排除。
    assert "guest_carol" not in ids
