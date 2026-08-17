"""Internal notification boundary for RND-161 (T6) feedback.

A single function every submission path calls through, so the eventual
real channel (email, webhook, internal task queue — RND-161's own AC
leaves this open, "通过统一 abstraction 处理，避免前端直接绑定某个第三方平台")
can be swapped in later without touching the router. v1 logs only
non-content metadata: the feedback body is free-text the user wrote for a
human to read in the product, not something to duplicate into
infrastructure logs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def notify_new_feedback(*, feedback_id: str, tenant_id: str, feedback_type: str) -> None:
    logger.info(
        "ai_feedback submitted id=%s tenant_id=%s type=%s", feedback_id, tenant_id, feedback_type
    )
