"""Periodic knowledge-gap / product-improvement aggregation (RND-359 / T5).

Produces a Markdown report grouping unresolved AI questions, feedback, and
human-classified handoff outcomes into candidate improvement items. This
module only ever returns text for a human to read and manually act on — it
never calls the Linear API and never writes anywhere but the returned
string (the caller decides where, if anywhere, to persist it).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AiChatMessage, AiFeedback, AiHandoff, AiQueryAuditLog

_RESOLUTION_LABELS = {
    "doc_missing": "文档缺失",
    "doc_stale": "文档过期",
    "product_bug": "产品 Bug",
    "config_issue": "配置问题",
    "external_platform": "外部平台问题",
    "model_issue": "模型问题",
    "user_misunderstanding": "用户误解",
}

_FEEDBACK_TYPE_LABELS = {
    "bug": "Bug / 异常",
    "question": "使用问题",
    "suggestion": "产品建议",
    "other": "其他",
}


@dataclass(frozen=True)
class GapReportData:
    unresolved_query_count: int
    top_unresolved_queries: list[tuple[str, int]]
    unhelpful_message_count: int
    handoff_resolution_counts: dict[str, int]
    pending_handoff_count: int
    feedback_type_counts: dict[str, int]


def collect_gap_report_data(db: Session, *, top_n: int = 10) -> GapReportData:
    unresolved_rows = db.execute(
        select(AiQueryAuditLog.query_text).where(
            AiQueryAuditLog.response_status.in_(["insufficient_evidence", "error"])
        )
    ).scalars().all()
    query_counts = Counter(unresolved_rows)
    top_unresolved = query_counts.most_common(top_n)

    unhelpful_count = db.execute(
        select(AiChatMessage.id).where(AiChatMessage.role == "assistant", AiChatMessage.helpful.is_(False))
    ).scalars().all()

    resolved_categories = db.execute(
        select(AiHandoff.resolution_category).where(AiHandoff.resolution_category.is_not(None))
    ).scalars().all()
    resolution_counts = dict(Counter(resolved_categories))

    pending_count = db.execute(
        select(AiHandoff.id).where(AiHandoff.status != "resolved")
    ).scalars().all()

    feedback_types = db.execute(select(AiFeedback.feedback_type)).scalars().all()
    feedback_counts = dict(Counter(feedback_types))

    return GapReportData(
        unresolved_query_count=len(unresolved_rows),
        top_unresolved_queries=top_unresolved,
        unhelpful_message_count=len(unhelpful_count),
        handoff_resolution_counts=resolution_counts,
        pending_handoff_count=len(pending_count),
        feedback_type_counts=feedback_counts,
    )


def render_gap_report_markdown(data: GapReportData, *, generated_at: str) -> str:
    lines = [
        "# AI 客服知识与产品改进候选清单",
        "",
        f"生成时间：{generated_at}",
        "",
        "本报告只生成候选改进项草稿，不自动创建、修改或关闭 Linear issue，需人工确认后再建票。",
        "",
        "## 1. 高频未解决问题",
        "",
        f"未解决查询总数（insufficient_evidence / error）：{data.unresolved_query_count}",
        "",
    ]
    if data.top_unresolved_queries:
        lines.append("| 问题 | 出现次数 |")
        lines.append("|---|---|")
        for query, count in data.top_unresolved_queries:
            lines.append(f"| {query} | {count} |")
    else:
        lines.append("（暂无未解决查询）")
    lines.append("")

    lines.append("## 2. 用户标记「无帮助」的回答数")
    lines.append("")
    lines.append(f"{data.unhelpful_message_count} 条回答被用户标记为无帮助，建议抽查对应会话。")
    lines.append("")

    lines.append("## 3. 人工处理结果分类（已解决）")
    lines.append("")
    if data.handoff_resolution_counts:
        lines.append("| 分类 | 数量 | 候选改进方向 |")
        lines.append("|---|---|---|")
        for category, count in sorted(data.handoff_resolution_counts.items(), key=lambda kv: -kv[1]):
            label = _RESOLUTION_LABELS.get(category, category)
            lines.append(f"| {label} | {count} | 见下方建议 |")
    else:
        lines.append("（暂无已解决转人工记录）")
    lines.append("")
    lines.append(f"待处理转人工数：{data.pending_handoff_count}")
    lines.append("")

    lines.append("## 4. 用户主动反馈分类")
    lines.append("")
    if data.feedback_type_counts:
        lines.append("| 类型 | 数量 |")
        lines.append("|---|---|")
        for feedback_type, count in sorted(data.feedback_type_counts.items(), key=lambda kv: -kv[1]):
            label = _FEEDBACK_TYPE_LABELS.get(feedback_type, feedback_type)
            lines.append(f"| {label} | {count} |")
    else:
        lines.append("（暂无用户反馈）")
    lines.append("")

    lines.append("## 5. 候选改进任务草稿")
    lines.append("")
    doc_related = data.handoff_resolution_counts.get("doc_missing", 0) + data.handoff_resolution_counts.get(
        "doc_stale", 0
    )
    if doc_related:
        lines.append(f"- 【文档改进】{doc_related} 条转人工记录归因为文档缺失/过期，建议排查 RND-355 manifest 覆盖范围。")
    if data.handoff_resolution_counts.get("product_bug", 0):
        lines.append(
            f"- 【产品缺陷】{data.handoff_resolution_counts['product_bug']} 条转人工记录归因为产品 Bug，建议人工确认后单独建 Linear issue。"
        )
    if data.feedback_type_counts.get("bug", 0):
        lines.append(f"- 【Bug 反馈】{data.feedback_type_counts['bug']} 条用户主动反馈的 Bug，建议逐条复核。")
    if data.top_unresolved_queries:
        lines.append("- 【知识库缺口】高频未解决问题见第 1 节，建议优先补充命中率最低的主题文档。")
    if not any(
        [
            doc_related,
            data.handoff_resolution_counts.get("product_bug", 0),
            data.feedback_type_counts.get("bug", 0),
            data.top_unresolved_queries,
        ]
    ):
        lines.append("（本周期无明显候选改进项）")
    lines.append("")

    return "\n".join(lines)
