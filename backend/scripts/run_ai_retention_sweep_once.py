#!/usr/bin/env python3
"""Delete AI chat sessions (and their messages) past the configured
retention window (RND-359 / T5). Scoped to ai_chat_sessions only —
ai_feedback and ai_handoff are structured product-improvement records
retained under a separate policy (analogous to AuditLog), not swept here.
ai_query_audit_logs rows referencing a deleted session survive via
ON DELETE SET NULL (see migration 0060)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.models import AiChatMessage, AiChatSession

_DEFAULT_RETENTION_DAYS = 90


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("AI_RETENTION_DAYS", str(_DEFAULT_RETENTION_DAYS))))
    except ValueError:
        return _DEFAULT_RETENTION_DAYS


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] ai_retention_sweep configuration_missing", flush=True)
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(days=_retention_days())
    try:
        with Session(create_engine(database_url)) as db:
            expired_ids = db.execute(
                select(AiChatSession.id).where(AiChatSession.created_at < cutoff)
            ).scalars().all()
            for session_id in expired_ids:
                db.query(AiChatMessage).filter(AiChatMessage.session_id == session_id).delete()
                db.query(AiChatSession).filter(AiChatSession.id == session_id).delete()
            db.commit()
    except Exception:  # noqa: BLE001 - never expose DB URLs or chat content
        print("[FAIL] ai_retention_sweep processing_failed", flush=True)
        return 1

    print(f"[INFO] ai_retention_sweep completed deleted_sessions={len(expired_ids)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
