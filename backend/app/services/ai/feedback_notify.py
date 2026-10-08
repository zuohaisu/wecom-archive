"""Internal notification boundary for RND-161 (T6) feedback.

A single function every submission path calls through. The real channel is
email to the product owner's inbox (Haisu request); the submission API must
stay available even when delivery fails, so the send is one bounded
best-effort attempt whose outcome is logged, never raised. v1 logs only
non-content metadata: the feedback body is free-text the user wrote for a
human to read in the product, so it travels in the email body only — never
in the subject, headers, or infrastructure logs.
"""

from __future__ import annotations

import logging

from app.email import FEEDBACK_INBOX_EMAIL, send_feedback_email

logger = logging.getLogger(__name__)

_TYPE_LABELS = {
    "bug": "Bug / 异常",
    "question": "使用问题",
    "suggestion": "产品建议",
    "other": "其他",
}


def notify_new_feedback(
    *,
    feedback_id: str,
    tenant_id: str,
    feedback_type: str,
    body: str,
    contact: str | None = None,
    submitter: str | None = None,
    page_id: str | None = None,
    product_version: str | None = None,
) -> bool:
    logger.info(
        "ai_feedback submitted id=%s tenant_id=%s type=%s", feedback_id, tenant_id, feedback_type
    )
    lines = [
        f"反馈类型：{_TYPE_LABELS.get(feedback_type, feedback_type)}",
        "反馈内容：",
        body,
        f"联系方式：{contact or '未填写'}",
        f"提交人：{submitter or '未知的控制台账号'}",
        f"来源页面：{page_id or '未记录'}",
        f"产品版本：{product_version or '未附带'}",
        f"租户：{tenant_id}",
        f"反馈编号：{feedback_id}",
    ]
    delivered = send_feedback_email(
        f"产品反馈 · {_TYPE_LABELS.get(feedback_type, feedback_type)} · {feedback_id[:8]}",
        "\n".join(lines),
        operation_id=f"feedback/{feedback_id}",
    )
    if not delivered:
        logger.warning(
            "ai_feedback email outcome=undelivered id=%s inbox=%s",
            feedback_id,
            FEEDBACK_INBOX_EMAIL,
        )
    return delivered
