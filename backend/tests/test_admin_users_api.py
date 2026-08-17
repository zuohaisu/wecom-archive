"""RND-284 contracts for the tenant-scoped admin-user list API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Contact


_NOW = datetime.now(timezone.utc)
_RECENT_MS = int((_NOW - timedelta(days=29)).timestamp() * 1000)
_OLD_MS = int((_NOW - timedelta(days=31)).timestamp() * 1000)


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE admin_users (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    wecom_user_id TEXT NOT NULL,
                    name TEXT,
                    avatar_url TEXT,
                    last_login_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME,
                    password_hash TEXT,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    email TEXT,
                    phone TEXT,
                    department TEXT,
                    last_active_at DATETIME,
                    invite_token TEXT,
                    invited_by TEXT,
                    invite_status TEXT,
                    ui_theme TEXT NOT NULL DEFAULT 'light',
                    ui_locale TEXT NOT NULL DEFAULT 'zh-CN'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE archive_messages (
                    id INTEGER PRIMARY KEY,
                    sender TEXT,
                    tenant_id TEXT,
                    msgtime INTEGER
                )
                """
            )
        )
    Contact.__table__.create(engine)
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture()
def client(db: Session):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    def override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (object(), "tenant-a")
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _add_user(
    db: Session,
    *,
    user_id: str,
    tenant_id: str = "tenant-a",
    wecom_user_id: str | None = None,
    name: str | None = None,
    role: str = "admin",
    status: str = "active",
    department: str | None = None,
    last_active_at: datetime | None = _NOW,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO admin_users
            (id, tenant_id, wecom_user_id, name, role, status, department, last_active_at)
            VALUES (:id, :tenant_id, :wecom_user_id, :name, :role, :status,
                    :department, :last_active_at)
            """
        ),
        {
            "id": user_id,
            "tenant_id": tenant_id,
            "wecom_user_id": wecom_user_id or user_id,
            "name": name,
            "role": role,
            "status": status,
            "department": department,
            "last_active_at": last_active_at,
        },
    )


def _add_message(db: Session, *, message_id: int, sender: str, tenant_id: str, msgtime: int) -> None:
    db.execute(
        text(
            "INSERT INTO archive_messages (id, sender, tenant_id, msgtime) "
            "VALUES (:id, :sender, :tenant_id, :msgtime)"
        ),
        {"id": message_id, "sender": sender, "tenant_id": tenant_id, "msgtime": msgtime},
    )


def test_list_admin_users_is_tenant_scoped_and_counts_sent_messages(client: TestClient, db: Session) -> None:
    _add_user(db, user_id="alice", wecom_user_id="alice-id", name="Alice", department="Legal")
    _add_user(db, user_id="bob", wecom_user_id="bob-id", name="Bob", tenant_id="tenant-b")
    _add_message(db, message_id=1, sender="alice-id", tenant_id="tenant-a", msgtime=_RECENT_MS + 1)
    _add_message(db, message_id=2, sender="alice-id", tenant_id="tenant-a", msgtime=_RECENT_MS + 2)
    _add_message(db, message_id=3, sender="alice-id", tenant_id="tenant-a", msgtime=_OLD_MS - 1)
    _add_message(db, message_id=4, sender="alice-id", tenant_id="tenant-b", msgtime=_RECENT_MS + 3)
    db.commit()

    response = client.get("/api/admin/users")

    from app.db.models import ArchiveMessage

    expected_count = (
        db.query(func.count(ArchiveMessage.id))
        .filter(
            ArchiveMessage.tenant_id == "tenant-a",
            ArchiveMessage.sender == "alice-id",
            ArchiveMessage.msgtime >= int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000),
        )
        .scalar()
    )

    assert response.status_code == 200
    assert expected_count == 2
    assert response.json() == {
        "items": [
            {
                "id": "alice",
                "name": "Alice",
                "wecom_user_id": "alice-id",
                "email": None,
                "department": "Legal",
                "avatar_url": None,
                "avatar_status": "missing",
                "role": "admin",
                "status": "active",
                "last_active_at": _NOW.isoformat(),
                "msg_count_30d": 2,
            }
        ],
        "total": 1,
        "page": 1,
        "per_page": 20,
    }


def test_list_admin_users_filters_and_paginates(client: TestClient, db: Session) -> None:
    _add_user(
        db,
        user_id="active-legal",
        name="Alice Legal",
        role="legal",
        department="Legal",
        last_active_at=_NOW - timedelta(days=10),
    )
    _add_user(
        db,
        user_id="disabled-admin",
        name="Bob Admin",
        role="admin",
        status="disabled",
        last_active_at=_NOW - timedelta(days=1),
    )
    _add_user(
        db,
        user_id="silent-owner",
        name="Carol Owner",
        role="owner",
        last_active_at=_NOW - timedelta(days=40),
    )
    db.commit()

    filtered = client.get("/api/admin/users", params={"role": "legal", "q": "legal"})
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == ["active-legal"]

    silent = client.get("/api/admin/users", params={"silent_days": 30})
    assert silent.status_code == 200
    assert [item["id"] for item in silent.json()["items"]] == ["silent-owner"]

    page = client.get("/api/admin/users", params={"status": "active", "per_page": 1, "page": 1})
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["page"] == 1
    assert page.json()["per_page"] == 1
    assert [item["id"] for item in page.json()["items"]] == ["active-legal"]

    out_of_range = client.get("/api/admin/users", params={"page": 9})
    assert out_of_range.status_code == 200
    assert out_of_range.json()["items"] == []
