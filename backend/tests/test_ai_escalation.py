"""RND-359 (T5) — deterministic escalate-to-human rules (no DB required)."""

from __future__ import annotations

from app.services.ai.escalation import should_escalate
from app.services.ai.retriever import ChunkResult


def _chunk(topic_id: str = "topic-a", rank: float = 0.5) -> ChunkResult:
    return ChunkResult(
        chunk_id=1,
        source_id=f"{topic_id}-zh-cn",
        topic_id=topic_id,
        title="Doc",
        heading_path="Doc",
        doc_path="doc.md",
        doc_version="1.0.0",
        content_text="content",
        access_level="customer",
        locale="zh-CN",
        rank=rank,
    )


def test_no_chunks_and_insufficient_evidence_is_no_evidence() -> None:
    reason = should_escalate(retrieved_chunks=[], response_status="insufficient_evidence", response_text="")
    assert reason == "no_evidence"


def test_chunks_present_but_model_reported_insufficient_evidence() -> None:
    reason = should_escalate(
        retrieved_chunks=[_chunk()], response_status="insufficient_evidence", response_text=""
    )
    assert reason == "model_reported_insufficient_evidence"


def test_disabled_status_does_not_escalate() -> None:
    assert should_escalate(retrieved_chunks=[], response_status="disabled", response_text="") is None


def test_error_status_does_not_escalate() -> None:
    assert should_escalate(retrieved_chunks=[], response_status="error", response_text="") is None


def test_high_risk_keyword_escalates_even_when_answered() -> None:
    reason = should_escalate(
        retrieved_chunks=[_chunk(rank=0.9)],
        response_status="answered",
        response_text="操作步骤如下",
        query="怎么删除租户数据",
    )
    assert reason == "high_risk_operation"


def test_privacy_keyword_escalates() -> None:
    reason = should_escalate(
        retrieved_chunks=[_chunk(rank=0.9)],
        response_status="answered",
        response_text="回答",
        query="能不能查看其他租户的手机号",
    )
    assert reason == "privacy_concern"


def test_high_risk_takes_priority_over_privacy_when_both_present() -> None:
    reason = should_escalate(
        retrieved_chunks=[_chunk(rank=0.9)],
        response_status="answered",
        response_text="回答",
        query="删除租户数据并导出所有用户数据",
    )
    assert reason == "high_risk_operation"


def test_answered_with_zero_chunks_is_no_citation_defensive_case() -> None:
    reason = should_escalate(retrieved_chunks=[], response_status="answered", response_text="回答", query="正常问题")
    assert reason == "no_citation"


def test_low_confidence_single_topic_below_threshold() -> None:
    reason = should_escalate(
        retrieved_chunks=[_chunk(topic_id="a", rank=0.05)],
        response_status="answered",
        response_text="回答",
        query="正常问题",
    )
    assert reason == "low_confidence"


def test_conflicting_sources_multi_topic_below_threshold() -> None:
    reason = should_escalate(
        retrieved_chunks=[_chunk(topic_id="a", rank=0.1), _chunk(topic_id="b", rank=0.05)],
        response_status="answered",
        response_text="回答",
        query="正常问题",
    )
    assert reason == "conflicting_sources"


def test_confident_single_topic_answer_does_not_escalate() -> None:
    reason = should_escalate(
        retrieved_chunks=[_chunk(topic_id="a", rank=0.6)],
        response_status="answered",
        response_text="回答",
        query="怎么设置存储配额",
    )
    assert reason is None


def test_confident_multi_topic_answer_does_not_escalate() -> None:
    """High confidence across multiple topics is fine — conflicting_sources
    only fires when confidence is ALSO low; two well-matched docs from
    different topics is normal (e.g. a question spanning FAQ + guide)."""
    reason = should_escalate(
        retrieved_chunks=[_chunk(topic_id="a", rank=0.6), _chunk(topic_id="b", rank=0.55)],
        response_status="answered",
        response_text="回答",
        query="正常问题",
    )
    assert reason is None
