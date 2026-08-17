"""RND-359 (T5) — scripts/run_ai_retention_sweep_once.py entrypoint."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_ai_retention_sweep_once  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session:
        yield session


@pytest.fixture()
def tenant_and_user(db: Session):
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Retention tenant', :slug)"),
        {"id": tenant_id, "slug": f"retention-{tenant_id[:8]}"},
    )
    db.execute(
        text(
            "INSERT INTO admin_users (id, tenant_id, wecom_user_id, role) "
            "VALUES (:id, :tenant_id, :wecom_user_id, 'admin')"
        ),
        {"id": user_id, "tenant_id": tenant_id, "wecom_user_id": f"wecom-{user_id[:8]}"},
    )
    db.commit()
    return tenant_id, user_id


def test_main_fails_closed_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert run_ai_retention_sweep_once.main() == 1


def test_sweep_deletes_expired_session_and_its_messages(
    db: Session, tenant_and_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, user_id = tenant_and_user
    monkeypatch.setenv("AI_RETENTION_DAYS", "1")
    expired_session_id = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO ai_chat_sessions (id, tenant_id, admin_user_id, status, created_at) "
            "VALUES (:id, :t, :u, 'active', now() - interval '10 days')"
        ),
        {"id": expired_session_id, "t": tenant_id, "u": user_id},
    )
    db.execute(
        text(
            "INSERT INTO ai_chat_messages (session_id, tenant_id, role, content) "
            "VALUES (:s, :t, 'user', '过期会话的消息')"
        ),
        {"s": expired_session_id, "t": tenant_id},
    )
    db.commit()

    assert run_ai_retention_sweep_once.main() == 0

    session_row = db.execute(
        text("SELECT 1 FROM ai_chat_sessions WHERE id = :id"), {"id": expired_session_id}
    ).fetchone()
    message_rows = db.execute(
        text("SELECT 1 FROM ai_chat_messages WHERE session_id = :id"), {"id": expired_session_id}
    ).fetchall()
    assert session_row is None
    assert message_rows == []


def test_sweep_keeps_recent_session(db: Session, tenant_and_user, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id, user_id = tenant_and_user
    monkeypatch.setenv("AI_RETENTION_DAYS", "90")
    recent_session_id = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO ai_chat_sessions (id, tenant_id, admin_user_id, status) "
            "VALUES (:id, :t, :u, 'active')"
        ),
        {"id": recent_session_id, "t": tenant_id, "u": user_id},
    )
    db.commit()

    assert run_ai_retention_sweep_once.main() == 0

    row = db.execute(text("SELECT 1 FROM ai_chat_sessions WHERE id = :id"), {"id": recent_session_id}).fetchone()
    assert row is not None
