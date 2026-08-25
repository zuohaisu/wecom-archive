#!/usr/bin/env python3
"""One-shot async bulk message-cleanup worker (RND-370).

Usage (from ``backend/``)::

    DATABASE_URL=... python scripts/process_message_cleanup_once.py

Processes queued/running cleanup tasks page by page (bounded per tenant and
per run). Reruns continue where a previous run left off: the candidate query
excludes rows already soft-deleted, so progress is idempotent and a crash
between pages never double-processes. Deployment-side scheduling (cron or
the shipped systemd timer) invokes this script; no application scheduler is
involved.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.models import MessageCleanupTask  # noqa: E402
from app.services.message_cleanup import (  # noqa: E402
    process_cleanup_task_once,
    purge_expired_previews,
)


def _current_time() -> datetime:
    return datetime.now(timezone.utc)


def process_message_cleanup_once(
    db: Session,
    *,
    per_task_pages: int = 4,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Advance cleanup tasks; returns per-status tallies of processed tasks."""
    run_at = now if now is not None else _current_time()
    purge_expired_previews(db, now=run_at)
    summary = {"tasks_touched": 0, "pages": 0, "succeeded_rows": 0, "skipped_rows": 0}
    tasks = db.scalars(
        select(MessageCleanupTask)
        .where(MessageCleanupTask.status.in_(("queued", "running")))
        .order_by(MessageCleanupTask.created_at.asc())
        .limit(20)
    ).all()
    for task in tasks:
        for _ in range(per_task_pages):
            if task.status in ("completed", "failed", "canceled"):
                break
            previous_succeeded = task.succeeded
            status = process_cleanup_task_once(db, task, now=run_at)
            summary["tasks_touched"] += 1
            summary["pages"] += 1
            summary["succeeded_rows"] += max(task.succeeded - previous_succeeded, 0)
            db.commit()
            if status in ("completed", "failed", "canceled"):
                break
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Process queued bulk cleanup tasks")
    parser.add_argument("--pages", type=int, default=4, help="Max pages per task per run")
    args = parser.parse_args()

    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 1
    engine = create_engine(url)
    with Session(engine) as db:
        summary = process_message_cleanup_once(db, per_task_pages=args.pages)
        print(
            "message_cleanup_worker: "
            f"tasks_touched={summary['tasks_touched']} pages={summary['pages']} "
            f"succeeded_rows={summary['succeeded_rows']} skipped_rows={summary['skipped_rows']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
