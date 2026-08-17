"""RND-408 — scripts/run_ai_public_retention_sweep_once.py entrypoint."""

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
import run_ai_public_retention_sweep_once  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session:
        yield session


def test_main_fails_closed_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert run_ai_public_retention_sweep_once.main() == 1


def test_sweep_deletes_expired_public_session_and_its_messages(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PUBLIC_RETENTION_DAYS", "1")
    visitor_id = "test-visitor-" + uuid.uuid4().hex
    expired_session_id = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO ai_public_chat_sessions (id, visitor_id, status, created_at) "
            "VALUES (:id, :visitor_id, 'active', now() - interval '10 days')"
        ),
        {"id": expired_session_id, "visitor_id": visitor_id},
    )
    db.execute(
        text(
            "INSERT INTO ai_public_chat_messages (session_id, visitor_id, role, content) "
            "VALUES (:s, :visitor_id, 'user', '过期会话的消息')"
        ),
        {"s": expired_session_id, "visitor_id": visitor_id},
    )
    db.commit()

    assert run_ai_public_retention_sweep_once.main() == 0

    session_row = db.execute(
        text("SELECT 1 FROM ai_public_chat_sessions WHERE id = :id"), {"id": expired_session_id}
    ).fetchone()
    message_rows = db.execute(
        text("SELECT 1 FROM ai_public_chat_messages WHERE session_id = :id"), {"id": expired_session_id}
    ).fetchall()
    assert session_row is None
    assert message_rows == []


def test_sweep_keeps_recent_public_session(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PUBLIC_RETENTION_DAYS", "90")
    visitor_id = "test-visitor-" + uuid.uuid4().hex
    recent_session_id = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO ai_public_chat_sessions (id, visitor_id, status) "
            "VALUES (:id, :visitor_id, 'active')"
        ),
        {"id": recent_session_id, "visitor_id": visitor_id},
    )
    db.commit()

    assert run_ai_public_retention_sweep_once.main() == 0

    session_row = db.execute(
        text("SELECT 1 FROM ai_public_chat_sessions WHERE id = :id"), {"id": recent_session_id}
    ).fetchone()
    assert session_row is not None
