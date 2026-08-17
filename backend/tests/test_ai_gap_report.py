"""RND-359 (T5) — gap-report aggregation and Markdown rendering."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.ai.gap_report import collect_gap_report_data, render_gap_report_markdown

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")


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
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Gap report tenant', :slug)"),
        {"id": tenant_id, "slug": f"gap-report-{tenant_id[:8]}"},
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


def test_collect_counts_unresolved_queries_and_feedback(db: Session, tenant_and_user) -> None:
    # A unique-per-run query text: this repo's shared disposable test DB
    # (docs/agent-test-database.md) is legitimately reused across many
    # runs, so a fixed literal string would accumulate a growing count
    # across reruns instead of staying at exactly 2.
    unresolved_query = f"同一个未解决的问题-{uuid.uuid4().hex[:8]}"
    tenant_id, user_id = tenant_and_user
    db.execute(
        text(
            "INSERT INTO ai_query_audit_logs "
            "(tenant_id, admin_user_id, query_text, retrieved_chunk_ids, response_status) "
            "VALUES (:t, :u, :q, '[]', 'insufficient_evidence')"
        ),
        {"t": tenant_id, "u": user_id, "q": unresolved_query},
    )
    db.execute(
        text(
            "INSERT INTO ai_query_audit_logs "
            "(tenant_id, admin_user_id, query_text, retrieved_chunk_ids, response_status) "
            "VALUES (:t, :u, :q, '[]', 'insufficient_evidence')"
        ),
        {"t": tenant_id, "u": user_id, "q": unresolved_query},
    )
    db.execute(
        text(
            "INSERT INTO ai_feedback (id, tenant_id, admin_user_id, feedback_type, body, status) "
            "VALUES (:id, :t, :u, 'bug', '导出按钮点击无反应', 'new')"
        ),
        {"id": str(uuid.uuid4()), "t": tenant_id, "u": user_id},
    )
    db.commit()

    # top_n large enough that this test's own entry can't be crowded out
    # by whatever accumulated noise the shared disposable test DB has from
    # earlier runs of this and other test files.
    data = collect_gap_report_data(db, top_n=10_000)

    assert data.unresolved_query_count >= 2
    assert (unresolved_query, 2) in data.top_unresolved_queries
    assert data.feedback_type_counts.get("bug", 0) >= 1


def test_collect_counts_resolved_handoffs_by_category(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    db.execute(
        text(
            "INSERT INTO ai_handoff (id, tenant_id, admin_user_id, redacted_summary, status, resolution_category, resolved_at) "
            "VALUES (:id, :t, :u, '摘要', 'resolved', 'doc_missing', now())"
        ),
        {"id": str(uuid.uuid4()), "t": tenant_id, "u": user_id},
    )
    db.execute(
        text(
            "INSERT INTO ai_handoff (id, tenant_id, admin_user_id, redacted_summary, status) "
            "VALUES (:id, :t, :u, '摘要', 'pending')"
        ),
        {"id": str(uuid.uuid4()), "t": tenant_id, "u": user_id},
    )
    db.commit()

    data = collect_gap_report_data(db)

    assert data.handoff_resolution_counts.get("doc_missing", 0) >= 1
    assert data.pending_handoff_count >= 1


def test_render_markdown_includes_key_sections_and_no_auto_ticket_language() -> None:
    from app.services.ai.gap_report import GapReportData

    data = GapReportData(
        unresolved_query_count=2,
        top_unresolved_queries=[("怎么设置存储配额但文档没覆盖", 2)],
        unhelpful_message_count=1,
        handoff_resolution_counts={"doc_missing": 1, "product_bug": 1},
        pending_handoff_count=1,
        feedback_type_counts={"bug": 1},
    )

    markdown = render_gap_report_markdown(data, generated_at="2026-08-17 00:00 UTC")

    assert "高频未解决问题" in markdown
    assert "怎么设置存储配额但文档没覆盖" in markdown
    assert "不自动创建、修改或关闭 Linear issue" in markdown
    assert "候选改进任务草稿" in markdown
    assert "文档改进" in markdown
    assert "产品缺陷" in markdown
