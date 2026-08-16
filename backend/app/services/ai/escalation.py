"""Deterministic escalate-to-human decision (RND-359 / T5 owns the real
rules — low confidence, no citation, conflicting sources, high-risk
operation, privacy question, out-of-scope question, explicit user request).

This module exists now, with a conservative placeholder, purely so
app/services/ai/answer_service.py has a stable call site to depend on
before T5 lands. T5 replaces should_escalate's body in place; the function
signature must not change without updating answer_service's call.
"""

from __future__ import annotations

from typing import Optional

from app.services.ai.retriever import ChunkResult


def should_escalate(
    *, retrieved_chunks: list[ChunkResult], response_status: str, response_text: str
) -> Optional[str]:
    """Return an escalation reason string, or None if no escalation is
    warranted. Placeholder policy (conservative until RND-359 lands): only
    escalate when there is literally no evidence to answer from — every
    other rule (conflicting sources, high-risk operation, privacy,
    out-of-scope, low model-reported confidence) is RND-359's job."""
    if response_status == "insufficient_evidence":
        return "no_evidence"
    return None
