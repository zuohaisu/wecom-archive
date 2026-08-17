"""RND-408 — /public/support page + /api/ai/public/support/* router:
no auth, visitor cookie, independent session/message/audit tables,
rate limiting, handoff, and fail-closed behavior.
"""

from __future__ import annotations

import json
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_db

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")

_VISITOR_COOKIE = "visitor_id"


@pytest.fixture()
def db():
    engine = create_engine(os.environ["DATABASE_URL"])
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db: Session):
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    from app.routers import public_ai_support

    public_ai_support._rate_limit_state.clear()
    yield
    public_ai_support._rate_limit_state.clear()


def _parse_sse(body: str) -> list[tuple[str, dict | str]]:
    events = []
    for part in body.split("\n\n"):
        if not part.strip():
            continue
        event_type = "message"
        data = None
        for line in part.split("\n"):
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if data is not None:
            try:
                events.append((event_type, json.loads(data)))
            except json.JSONDecodeError:
                events.append((event_type, data))
    return events


def test_public_support_page_renders_and_sets_visitor_cookie(client) -> None:
    response = client.get("/public/support")
    assert response.status_code == 200
    assert "AI 售前咨询" in response.text
    assert _VISITOR_COOKIE in response.cookies
    assert len(response.cookies[_VISITOR_COOKIE]) >= 32
    assert not re.search(r"__[A-Z0-9_]+__", response.text)


def test_public_status_endpoint_reflects_disabled_by_default(client) -> None:
    response = client.get("/api/ai/public/support/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": False}


def test_create_public_session_is_visitor_scoped(client, db: Session) -> None:
    response = client.post("/api/ai/public/support/sessions")
    assert response.status_code == 201
    session_id = response.json()["id"]
    visitor_id = response.cookies[_VISITOR_COOKIE]

    row = db.execute(
        text("SELECT visitor_id FROM ai_public_chat_sessions WHERE id = :id"), {"id": session_id}
    ).fetchone()
    assert row.visitor_id == visitor_id


def test_send_message_disabled_returns_disabled_event(client, monkeypatch) -> None:
    # Ensure overall AI switch is also off so public stays off.
    monkeypatch.setenv("AI_SUPPORT_ENABLED", "false")
    monkeypatch.setenv("AI_PUBLIC_SUPPORT_ENABLED", "false")

    session_id = client.post("/api/ai/public/support/sessions").json()["id"]
    response = client.post(
        f"/api/ai/public/support/sessions/{session_id}/messages", json={"message": "你好"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    done_events = [e for e in events if e[0] == "done"]
    assert len(done_events) == 1
    assert done_events[0][1]["response_status"] == "disabled"


def test_send_message_persists_both_turns(client, db: Session, monkeypatch) -> None:
    monkeypatch.setenv("AI_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_PUBLIC_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_LLM_PROVIDER", "fake")

    session_id = client.post("/api/ai/public/support/sessions").json()["id"]
    client.post(f"/api/ai/public/support/sessions/{session_id}/messages", json={"message": "你好"})

    rows = db.execute(
        text("SELECT role, content FROM ai_public_chat_messages WHERE session_id = :s ORDER BY id"),
        {"s": session_id},
    ).fetchall()
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[0].content == "你好"


def test_send_message_rejects_other_visitors_session(client, db: Session) -> None:
    other_visitor_id = "other-visitor-" + uuid.uuid4().hex
    other_session = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO ai_public_chat_sessions (id, visitor_id, status) "
            "VALUES (:id, :visitor_id, 'active')"
        ),
        {"id": other_session, "visitor_id": other_visitor_id},
    )
    db.commit()

    response = client.post(f"/api/ai/public/support/sessions/{other_session}/messages", json={"message": "hi"})
    assert response.status_code == 404


def test_rate_limit_returns_429_after_threshold(client, monkeypatch) -> None:
    monkeypatch.setenv("AI_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_PUBLIC_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_LLM_PROVIDER", "fake")

    session_id = client.post("/api/ai/public/support/sessions").json()["id"]

    last_status = None
    for _ in range(21):
        last_status = client.post(
            f"/api/ai/public/support/sessions/{session_id}/messages", json={"message": "hi"}
        ).status_code
    assert last_status == 429


def test_handoff_preview_and_submit_persists_visitor_summary(client, db: Session, monkeypatch) -> None:
    monkeypatch.setenv("AI_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_PUBLIC_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_LLM_PROVIDER", "fake")

    session_id = client.post("/api/ai/public/support/sessions").json()["id"]
    client.post(f"/api/ai/public/support/sessions/{session_id}/messages", json={"message": "年度套餐多少钱"})

    preview = client.post(f"/api/ai/public/support/sessions/{session_id}/handoff/preview")
    assert preview.status_code == 200
    summary = preview.json()["summary"]
    assert "年度套餐多少钱" in summary

    response = client.post(
        f"/api/ai/public/support/sessions/{session_id}/handoff",
        json={"summary": "访客编辑后的摘要", "contact": "test@example.com"},
    )
    assert response.status_code == 201
    handoff_id = response.json()["id"]
    assert response.json()["status"] == "pending"

    row = db.execute(
        text("SELECT visitor_id, redacted_summary, contact, reason FROM ai_public_handoff WHERE id = :id"),
        {"id": handoff_id},
    ).fetchone()
    assert row.redacted_summary == "访客编辑后的摘要"
    assert row.contact == "test@example.com"
    assert row.reason == "user_requested"


def test_delete_public_session_removes_session_and_messages(client, db: Session, monkeypatch) -> None:
    monkeypatch.setenv("AI_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_PUBLIC_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_LLM_PROVIDER", "fake")

    session_id = client.post("/api/ai/public/support/sessions").json()["id"]
    client.post(f"/api/ai/public/support/sessions/{session_id}/messages", json={"message": "你好"})

    response = client.delete(f"/api/ai/public/support/sessions/{session_id}")
    assert response.status_code == 204

    session_row = db.execute(
        text("SELECT 1 FROM ai_public_chat_sessions WHERE id = :id"), {"id": session_id}
    ).fetchone()
    message_rows = db.execute(
        text("SELECT 1 FROM ai_public_chat_messages WHERE session_id = :id"), {"id": session_id}
    ).fetchall()
    assert session_row is None
    assert message_rows == []


def test_public_session_cannot_access_admin_support_endpoints(client) -> None:
    response = client.get("/admin/support", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"


def test_public_answer_service_uses_only_public_chunks(client, db: Session, monkeypatch) -> None:
    """The public surface must retrieve only public-level chunks; customer
    and internal chunks are out of scope for anonymous visitors."""
    monkeypatch.setenv("AI_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_PUBLIC_SUPPORT_ENABLED", "true")
    monkeypatch.setenv("AI_LLM_PROVIDER", "fake")

    # Build an index with public, customer, and internal chunks.
    from app.services.ai.ingestion import build_index

    build_index(db, triggered_by="test_public_only")

    session_id = client.post("/api/ai/public/support/sessions").json()["id"]
    response = client.post(
        f"/api/ai/public/support/sessions/{session_id}/messages",
        json={"message": "年度套餐价格"},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    done = [e for e in events if e[0] == "done"][0][1]
    assert done["response_status"] in ("answered", "insufficient_evidence")

    audit = db.execute(
        text(
            "SELECT retrieved_chunk_ids FROM ai_public_query_audit_logs "
            "WHERE session_id = :s ORDER BY id DESC LIMIT 1"
        ),
        {"s": session_id},
    ).fetchone()
    assert audit is not None
    chunk_ids = audit.retrieved_chunk_ids or []
    if chunk_ids:
        chunk_rows = db.execute(
            text("SELECT access_level FROM kb_document_chunks WHERE id = ANY(:ids)"),
            {"ids": chunk_ids},
        ).fetchall()
        for row in chunk_rows:
            assert row.access_level == "public"
