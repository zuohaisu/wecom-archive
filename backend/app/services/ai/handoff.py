"""Redacted handoff-summary generation (RND-357 / T3).

Builds the text a user previews and can edit before confirming a
transfer-to-human request. RND-359 (T5) owns the full triage pipeline
(confidence-based auto-escalation rules, resolution taxonomy) and extends
ai_handoff's schema; this module only produces the summary text itself,
sourced strictly from the requesting session's own chat turns — never
archived customer chat content, never a field outside what the user already
saw on screen.
"""

from __future__ import annotations

from app.db.models import AiChatMessage

_MAX_QA_TURNS = 6


def build_redacted_summary(messages: list[AiChatMessage]) -> str:
    """Summarize the most recent turns of one AI support session into a
    plain-text block for human handoff. Only message role/content/citation
    titles are used — no tenant config values, no tool-invocation payloads,
    no archived chat content ever passes through this session in the first
    place (see app.services.ai.answer_service's out-of-scope constraints)."""
    lines: list[str] = ["【AI 客服转人工摘要 — 以下内容均来自本次 AI 客服对话，不含归档聊天内容】", ""]

    recent = messages[-(_MAX_QA_TURNS * 2) :]
    if not recent:
        lines.append("（本次转人工发起前用户尚未提问）")
        return "\n".join(lines)

    for message in recent:
        if message.role == "user":
            lines.append(f"用户提问：{message.content}")
        elif message.role == "assistant":
            status_label = {
                "answered": "已回答",
                "insufficient_evidence": "证据不足，未能确定回答",
                "error": "AI 服务出错",
                "disabled": "AI 客服未启用",
            }.get(message.response_status or "", message.response_status or "未知")
            lines.append(f"AI 回答（{status_label}）：{message.content}")
            citations = message.citations or []
            if citations:
                titles = "、".join(c.get("title", "") for c in citations if isinstance(c, dict))
                if titles:
                    lines.append(f"引用文档：{titles}")
        lines.append("")

    return "\n".join(lines).strip()
