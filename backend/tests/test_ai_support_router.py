"""RND-357 (T3) — /admin/support page + /api/ai/support/* router:
auth gates, tenant isolation, streaming answer format, rate limiting,
feedback, handoff preview/submit, and session deletion.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth import get_current_user, require_html_session
from app.db.session import get_db

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")


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
def tenant_and_user(db: Session):
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Router test tenant', :slug)"),
        {"id": tenant_id, "slug": f"ai-router-test-{tenant_id[:8]}"},
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


def _make_app_client(db: Session, tenant_id: str, user_id: str) -> TestClient:
    from app.main import app

    fake_user = SimpleNamespace(id=user_id, role="admin")
    app.dependency_overrides[get_current_user] = lambda: (fake_user, tenant_id)
    app.dependency_overrides[require_html_session] = lambda: tenant_id
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def client(db: Session, tenant_and_user):
    tenant_id, user_id = tenant_and_user
    from app.main import app

    c = _make_app_client(db, tenant_id, user_id)
    yield c, tenant_id, user_id
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    from app.routers import ai_support

    ai_support._rate_limit_state.clear()
    yield
    ai_support._rate_limit_state.clear()


def _parse_sse(body: str) -> list[tuple[str, dict | str]]:
    events = []
    for part in body.split("\n\n"):
        if not part.strip():
            continue
        event_type = "message"
        data = None
        for line in part.split("\n"):
            if line.startswith("event: "):
                event_type = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if data is not None:
            try:
                events.append((event_type, json.loads(data)))
            except json.JSONDecodeError:
                events.append((event_type, data))
    return events


def test_page_redirects_when_unauthenticated() -> None:
    from app.main import app

    app.dependency_overrides[require_html_session] = lambda: None
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/admin/support", follow_redirects=False
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"


def test_page_renders_with_sidenav_when_authenticated(client) -> None:
    c, _tenant_id, _user_id = client
    response = c.get("/admin/support")
    assert response.status_code == 200
    assert '<nav class="side-nav">' in response.text
    assert not re.search(r"__[A-Z0-9_]+__", response.text)


def test_status_endpoint_reflects_disabled_by_default(client) -> None:
    c, _tenant_id, _user_id = client
    response = c.get("/api/ai/support/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": False}


def test_create_session_is_tenant_scoped(client, db: Session) -> None:
    c, tenant_id, _user_id = client
    response = c.post("/api/ai/support/sessions")
    assert response.status_code == 201
    session_id = response.json()["id"]

    row = db.execute(
        text("SELECT tenant_id FROM ai_chat_sessions WHERE id = :id"), {"id": session_id}
    ).fetchone()
    assert row.tenant_id == tenant_id


def test_send_message_disabled_returns_disabled_event(client) -> None:
    c, _tenant_id, _user_id = client
    session_id = c.post("/api/ai/support/sessions").json()["id"]

    response = c.post(f"/api/ai/support/sessions/{session_id}/messages", json={"message": "你好"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    done_events = [e for e in events if e[0] == "done"]
    assert len(done_events) == 1
    assert done_events[0][1]["response_status"] == "disabled"


def test_send_message_persists_both_turns(client, db: Session) -> None:
    c, _tenant_id, _user_id = client
    session_id = c.post("/api/ai/support/sessions").json()["id"]
    c.post(f"/api/ai/support/sessions/{session_id}/messages", json={"message": "你好"})

    rows = db.execute(
        text("SELECT role, content FROM ai_chat_messages WHERE session_id = :s ORDER BY id"),
        {"s": session_id},
    ).fetchall()
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[0].content == "你好"


def test_send_message_rejects_other_tenants_session(client, db: Session) -> None:
    c, _tenant_id, _user_id = client
    other_tenant_id = str(uuid.uuid4())
    other_user_id = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Other tenant', :slug)"),
        {"id": other_tenant_id, "slug": f"other-{other_tenant_id[:8]}"},
    )
    db.execute(
        text(
            "INSERT INTO admin_users (id, tenant_id, wecom_user_id, role) "
            "VALUES (:id, :tenant_id, :wecom_user_id, 'admin')"
        ),
        {"id": other_user_id, "tenant_id": other_tenant_id, "wecom_user_id": f"wecom-{other_user_id[:8]}"},
    )
    other_session = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO ai_chat_sessions (id, tenant_id, admin_user_id, status) "
            "VALUES (:id, :tenant_id, :user_id, 'active')"
        ),
        {"id": other_session, "tenant_id": other_tenant_id, "user_id": other_user_id},
    )
    db.commit()

    response = c.post(f"/api/ai/support/sessions/{other_session}/messages", json={"message": "hi"})
    assert response.status_code == 404


def test_rate_limit_returns_429_after_threshold(client) -> None:
    c, _tenant_id, _user_id = client
    session_id = c.post("/api/ai/support/sessions").json()["id"]

    last_status = None
    for _ in range(21):
        last_status = c.post(
            f"/api/ai/support/sessions/{session_id}/messages", json={"message": "hi"}
        ).status_code
    assert last_status == 429


def test_feedback_updates_own_message(client, db: Session) -> None:
    c, tenant_id, _user_id = client
    session_id = c.post("/api/ai/support/sessions").json()["id"]
    c.post(f"/api/ai/support/sessions/{session_id}/messages", json={"message": "你好"})
    message_id = db.execute(
        text(
            "SELECT id FROM ai_chat_messages WHERE session_id = :s AND role = 'assistant' "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"s": session_id},
    ).scalar()

    response = c.post(f"/api/ai/support/messages/{message_id}/feedback", json={"helpful": True, "note": "good"})
    assert response.status_code == 200

    row = db.execute(
        text("SELECT helpful, feedback_note FROM ai_chat_messages WHERE id = :id"), {"id": message_id}
    ).fetchone()
    assert row.helpful is True
    assert row.feedback_note == "good"


def test_feedback_on_unknown_message_is_404(client) -> None:
    c, _tenant_id, _user_id = client
    response = c.post("/api/ai/support/messages/999999999/feedback", json={"helpful": False})
    assert response.status_code == 404


def test_handoff_preview_reflects_session_question(client) -> None:
    c, _tenant_id, _user_id = client
    session_id = c.post("/api/ai/support/sessions").json()["id"]
    c.post(f"/api/ai/support/sessions/{session_id}/messages", json={"message": "存储配额怎么设置"})

    response = c.post(f"/api/ai/support/sessions/{session_id}/handoff/preview")
    assert response.status_code == 200
    summary = response.json()["summary"]
    assert "存储配额怎么设置" in summary
    assert "归档聊天内容" in summary


def test_handoff_submit_persists_user_edited_summary(client, db: Session) -> None:
    c, tenant_id, _user_id = client
    session_id = c.post("/api/ai/support/sessions").json()["id"]

    response = c.post(
        f"/api/ai/support/sessions/{session_id}/handoff",
        json={"summary": "用户编辑后的摘要", "contact": "test@example.com"},
    )
    assert response.status_code == 201
    handoff_id = response.json()["id"]
    assert response.json()["status"] == "pending"

    row = db.execute(
        text("SELECT tenant_id, redacted_summary, contact FROM ai_handoff WHERE id = :id"),
        {"id": handoff_id},
    ).fetchone()
    assert row.tenant_id == tenant_id
    assert row.redacted_summary == "用户编辑后的摘要"
    assert row.contact == "test@example.com"


def test_delete_session_removes_session_and_messages(client, db: Session) -> None:
    c, _tenant_id, _user_id = client
    session_id = c.post("/api/ai/support/sessions").json()["id"]
    c.post(f"/api/ai/support/sessions/{session_id}/messages", json={"message": "你好"})

    response = c.delete(f"/api/ai/support/sessions/{session_id}")
    assert response.status_code == 204

    session_row = db.execute(
        text("SELECT 1 FROM ai_chat_sessions WHERE id = :id"), {"id": session_id}
    ).fetchone()
    message_rows = db.execute(
        text("SELECT 1 FROM ai_chat_messages WHERE session_id = :id"), {"id": session_id}
    ).fetchall()
    assert session_row is None
    assert message_rows == []


def test_submit_feedback_without_prior_ai_conversation(client, db: Session) -> None:
    """RND-161 AC: feedback never requires an existing chat/session."""
    c, tenant_id, user_id = client
    response = c.post(
        "/api/ai/support/feedback",
        json={"feedback_type": "bug", "body": "导出按钮点击无反应", "contact": "a@b.com"},
    )
    assert response.status_code == 201
    feedback_id = response.json()["id"]
    assert response.json()["status"] == "new"

    row = db.execute(
        text(
            "SELECT tenant_id, admin_user_id, feedback_type, body, contact "
            "FROM ai_feedback WHERE id = :id"
        ),
        {"id": feedback_id},
    ).fetchone()
    assert row.tenant_id == tenant_id
    assert row.admin_user_id == user_id
    assert row.feedback_type == "bug"
    assert row.body == "导出按钮点击无反应"
    assert row.contact == "a@b.com"


def test_submit_feedback_rejects_invalid_type(client) -> None:
    c, _tenant_id, _user_id = client
    response = c.post(
        "/api/ai/support/feedback", json={"feedback_type": "not_a_real_type", "body": "x"}
    )
    assert response.status_code == 422


def test_submit_feedback_page_id_is_allowlist_validated(client, db: Session) -> None:
    c, _tenant_id, _user_id = client
    response = c.post(
        "/api/ai/support/feedback",
        json={"feedback_type": "question", "body": "问题", "page_id": "../../etc/passwd"},
    )
    assert response.status_code == 201
    feedback_id = response.json()["id"]
    row = db.execute(text("SELECT page_id FROM ai_feedback WHERE id = :id"), {"id": feedback_id}).fetchone()
    assert row.page_id == "unknown"


def test_submit_feedback_without_diagnostics_omits_product_version(client, db: Session) -> None:
    c, _tenant_id, _user_id = client
    response = c.post(
        "/api/ai/support/feedback",
        json={"feedback_type": "suggestion", "body": "建议", "include_diagnostics": False},
    )
    feedback_id = response.json()["id"]
    row = db.execute(
        text("SELECT product_version FROM ai_feedback WHERE id = :id"), {"id": feedback_id}
    ).fetchone()
    assert row.product_version is None


def test_submit_feedback_with_diagnostics_includes_product_version(client, db: Session) -> None:
    c, _tenant_id, _user_id = client
    response = c.post(
        "/api/ai/support/feedback",
        json={"feedback_type": "suggestion", "body": "建议", "include_diagnostics": True},
    )
    feedback_id = response.json()["id"]
    row = db.execute(
        text("SELECT product_version FROM ai_feedback WHERE id = :id"), {"id": feedback_id}
    ).fetchone()
    assert row.product_version is not None


def test_submit_feedback_never_stores_archived_chat_fields(client, db: Session) -> None:
    """No column on ai_feedback can hold chat content — this is a schema-
    shape guard, not a behavioral one, but it is the cheapest possible
    regression check against RND-161's "默认不采集归档聊天正文" constraint."""
    columns = {
        row.column_name
        for row in db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'ai_feedback'")
        ).fetchall()
    }
    assert columns == {
        "id",
        "tenant_id",
        "admin_user_id",
        "feedback_type",
        "body",
        "contact",
        "product_version",
        "page_id",
        "browser_info",
        "status",
        "created_at",
    }


def test_delete_other_tenants_session_is_404(client, db: Session) -> None:
    c, _tenant_id, _user_id = client
    other_tenant_id = str(uuid.uuid4())
    other_user_id = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Other tenant 2', :slug)"),
        {"id": other_tenant_id, "slug": f"other2-{other_tenant_id[:8]}"},
    )
    db.execute(
        text(
            "INSERT INTO admin_users (id, tenant_id, wecom_user_id, role) "
            "VALUES (:id, :tenant_id, :wecom_user_id, 'admin')"
        ),
        {"id": other_user_id, "tenant_id": other_tenant_id, "wecom_user_id": f"wecom-{other_user_id[:8]}"},
    )
    other_session = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO ai_chat_sessions (id, tenant_id, admin_user_id, status) "
            "VALUES (:id, :tenant_id, :user_id, 'active')"
        ),
        {"id": other_session, "tenant_id": other_tenant_id, "user_id": other_user_id},
    )
    db.commit()

    response = c.delete(f"/api/ai/support/sessions/{other_session}")
    assert response.status_code == 404
