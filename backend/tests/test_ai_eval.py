"""RND-359 (T5) — offline retrieval evaluation harness."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.ai.eval import (
    ABSTENTION_RATE_THRESHOLD,
    HIT_RATE_THRESHOLD,
    EvalCase,
    load_eval_dataset,
    run_eval,
)
from app.services.ai.retriever import ChunkResult

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")

_DATASET_PATH = Path(__file__).resolve().parent / "fixtures" / "ai_eval_dataset.json"


@pytest.fixture()
def db():
    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session:
        yield session


def _make_version(db: Session) -> int:
    row = db.execute(
        text("INSERT INTO kb_index_versions (status, triggered_by) VALUES ('active', 'test') RETURNING id")
    ).fetchone()
    db.commit()
    return row.id


def _insert_chunk(db: Session, *, index_version_id: int, source_id: str, topic_id: str, content_text: str, access_level: str = "customer") -> None:
    db.execute(
        text(
            """
            INSERT INTO kb_document_chunks
                (index_version_id, source_id, topic_id, title, heading_path,
                 chunk_index, content_text, access_level, locale, doc_version,
                 doc_path, content_hash)
            VALUES
                (:index_version_id, :source_id, :topic_id, :source_id, :source_id,
                 0, :content_text, :access_level, 'zh-CN', '1.0.0', 'doc.md', 'deadbeef')
            """
        ),
        {
            "index_version_id": index_version_id,
            "source_id": source_id,
            "topic_id": topic_id,
            "content_text": content_text,
            "access_level": access_level,
        },
    )
    db.commit()


def test_load_eval_dataset_parses_real_fixture() -> None:
    version, cases = load_eval_dataset(_DATASET_PATH)
    assert version
    assert len(cases) >= 10
    assert any(c.expected_topic_ids == [] for c in cases)  # at least one out-of-scope case
    assert any(c.expected_topic_ids for c in cases)  # at least one in-scope case


def test_in_scope_hit_when_expected_topic_retrieved(db: Session) -> None:
    version_id = _make_version(db)
    _insert_chunk(db, index_version_id=version_id, source_id="doc-a", topic_id="topic-a", content_text="存储配额如何设置")
    cases = [EvalCase(id="c1", category="storage", query="存储配额", expected_topic_ids=["topic-a"])]

    summary = run_eval(db, "test-v1", cases, index_version_id=version_id)

    assert summary.hit_rate == 1.0
    assert summary.case_results[0].hit is True


def test_in_scope_miss_when_wrong_topic_retrieved(db: Session) -> None:
    version_id = _make_version(db)
    _insert_chunk(db, index_version_id=version_id, source_id="doc-b", topic_id="topic-b", content_text="企业微信登录授权流程")
    cases = [EvalCase(id="c1", category="storage", query="企业微信登录授权", expected_topic_ids=["topic-a"])]

    summary = run_eval(db, "test-v1", cases, index_version_id=version_id)

    assert summary.hit_rate == 0.0


def test_out_of_scope_hit_when_nothing_retrieved(db: Session) -> None:
    version_id = _make_version(db)
    _insert_chunk(db, index_version_id=version_id, source_id="doc-a", topic_id="topic-a", content_text="存储配额如何设置")
    cases = [EvalCase(id="oos1", category="out_of_scope", query="今天天气怎么样啊完全不相关", expected_topic_ids=[])]

    summary = run_eval(db, "test-v1", cases, index_version_id=version_id)

    assert summary.abstention_rate == 1.0


class _LeakyRetriever:
    """Simulates a hypothetically buggy retriever that ignores its own
    access_levels filter, to prove eval.py's over_permission detection
    actually fires — the real PostgresFtsRetriever cannot be coaxed into
    this because its SQL WHERE clause is the enforcement point itself
    (see test_ai_retriever.py's access-level isolation tests for that)."""

    def search(self, query, *, access_levels, locale, index_version_id, limit=5):
        return [
            ChunkResult(
                chunk_id=1, source_id="doc-internal", topic_id="topic-x", title="t",
                heading_path="t", doc_path="doc.md", doc_version="1.0.0",
                content_text="存储配额内部运维说明", access_level="internal", locale="zh-CN", rank=0.5,
            )
        ]


def test_over_permission_detected_when_internal_chunk_leaks(db: Session) -> None:
    version_id = _make_version(db)
    cases = [EvalCase(id="c1", category="storage", query="存储配额", expected_topic_ids=["topic-x"])]

    summary = run_eval(
        db, "test-v1", cases, index_version_id=version_id,
        retriever=_LeakyRetriever(), access_levels=["customer"],
    )

    assert summary.over_permission_count == 1
    assert not summary.passed
    assert any("over_permission" in b for b in summary.blockers)


def test_over_permission_is_zero_when_access_levels_correctly_scoped(db: Session) -> None:
    version_id = _make_version(db)
    _insert_chunk(
        db, index_version_id=version_id, source_id="doc-internal", topic_id="topic-x",
        content_text="存储配额内部运维说明", access_level="internal",
    )
    cases = [EvalCase(id="c1", category="storage", query="存储配额", expected_topic_ids=["topic-x"])]

    summary = run_eval(db, "test-v1", cases, index_version_id=version_id, access_levels=["customer"])

    assert summary.over_permission_count == 0


def test_real_dataset_never_regresses_below_recorded_baseline(db: Session) -> None:
    """Regression guard for the real doc corpus + real dataset: the
    over-permission and abstention safety metrics must stay perfect, and
    hit_rate must not silently regress below the documented threshold."""
    from app.services.ai.ingestion import build_index

    result = build_index(db, triggered_by="test")
    version, cases = load_eval_dataset(_DATASET_PATH)

    summary = run_eval(db, version, cases, index_version_id=result.index_version_id)

    assert summary.over_permission_count == 0
    assert summary.abstention_rate >= ABSTENTION_RATE_THRESHOLD
    assert summary.hit_rate >= HIT_RATE_THRESHOLD
