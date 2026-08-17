"""Deterministic escalate-to-human rules (RND-359 / T5).

Every rule here is a plain, auditable check over already-computed
signals (retrieved chunks, response_status, response text) — never a
second LLM call asked "should I escalate?", which would just move the
hallucination risk instead of removing it. First matching rule wins;
order encodes priority (structural failures before content heuristics).
"""

from __future__ import annotations

from typing import Optional

from app.services.ai.retriever import ChunkResult

# Below this best-match score, retrieval found *something* but not
# confidently enough to trust as the sole basis for an answer. Distinct
# from "no chunks at all" (that's the no_evidence / model-reported path,
# handled deterministically upstream in answer_service before an LLM call
# even happens).
_LOW_CONFIDENCE_RANK_THRESHOLD = 0.2

# Deliberately conservative, high-precision keyword lists: a false
# positive here just means one extra human review, which is the safe
# direction to err in. A false negative lets a risky/privacy-sensitive
# question get an unreviewed AI answer, which is not.
_HIGH_RISK_KEYWORDS = (
    "删除租户", "删除数据", "重置密码", "退款", "修改计费", "变更套餐",
    "生产环境", "运行命令", "sql", "drop table", "rm -rf",
)
_PRIVACY_KEYWORDS = (
    "身份证", "手机号", "导出用户数据", "导出所有数据", "查看其他租户",
    "其他公司", "别的公司", "个人信息", "隐私",
)


def _contains_any(text: str, keywords: tuple) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def should_escalate(
    *, retrieved_chunks: list[ChunkResult], response_status: str, response_text: str, query: str = ""
) -> Optional[str]:
    """Return an escalation reason code, or None. Reason codes are stored
    verbatim in AiHandoff.reason and must stay stable — they are read by
    the gap-report aggregation (run_ai_kb_gap_report.py)."""
    if response_status == "insufficient_evidence":
        return "no_evidence" if not retrieved_chunks else "model_reported_insufficient_evidence"

    if response_status != "answered":
        return None  # disabled/error paths already show their own clear state; not a "human review" case

    if query and _contains_any(query, _HIGH_RISK_KEYWORDS):
        return "high_risk_operation"
    if query and _contains_any(query, _PRIVACY_KEYWORDS):
        return "privacy_concern"

    if not retrieved_chunks:
        return "no_citation"  # defensive: answered with zero chunks should never happen by construction

    distinct_topics = {c.topic_id for c in retrieved_chunks}
    top_rank = max(c.rank for c in retrieved_chunks)
    if len(distinct_topics) > 1 and top_rank < _LOW_CONFIDENCE_RANK_THRESHOLD:
        return "conflicting_sources"
    if top_rank < _LOW_CONFIDENCE_RANK_THRESHOLD:
        return "low_confidence"

    return None
