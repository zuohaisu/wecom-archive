#!/usr/bin/env python3
"""Delete anonymous public AI chat sessions (and their messages) past the
configured retention window (RND-408). Public handoffs are structured
product-improvement records retained under a separate policy, not swept
here. Public audit logs referencing a deleted session survive via
ON DELETE SET NULL (see migration 0063)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.models import AiPublicChatMessage, AiPublicChatSession

_DEFAULT_RETENTION_DAYS = 7


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("AI_PUBLIC_RETENTION_DAYS", str(_DEFAULT_RETENTION_DAYS))))
    except ValueError:
        return _DEFAULT_RETENTION_DAYS


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] ai_public_retention_sweep configuration_missing", flush=True)
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(days=_retention_days())
    try:
        with Session(create_engine(database_url)) as db:
            expired_ids = db.execute(
                select(AiPublicChatSession.id).where(AiPublicChatSession.created_at < cutoff)
            ).scalars().all()
            for session_id in expired_ids:
                db.query(AiPublicChatMessage).filter(AiPublicChatMessage.session_id == session_id).delete()
                db.query(AiPublicChatSession).filter(AiPublicChatSession.id == session_id).delete()
            db.commit()
    except Exception:  # noqa: BLE001 - never expose DB URLs or chat content
        print("[FAIL] ai_public_retention_sweep processing_failed", flush=True)
        return 1

    print(f"[INFO] ai_public_retention_sweep completed deleted_sessions={len(expired_ids)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
