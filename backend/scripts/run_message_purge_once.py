#!/usr/bin/env python3
"""One-shot 30-day recycle-bin auto purge (RND-364).

Usage (from ``backend/``)::

    DATABASE_URL=... python scripts/run_message_purge_once.py

For every tenant, moves recycle-bin messages whose ``purge_after`` window
has expired into permanent deletion: derived rows are removed, and media
objects are deleted only after the storage provider confirms deletion.
Object-storage failures keep a durable retry state (``media_purge_retries``)
instead of pretending the cleanup succeeded; the next run retries them.

Idempotent and bounded: each run processes at most ``--limit`` messages per
tenant, reruns skip rows already purged, and concurrency is safe because the
candidate predicate (still soft-deleted + purge window elapsed) can never
match a row twice. Deployment-side scheduling (for example cron or the
shipped systemd timer) invokes this script; no application scheduler is
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

from app.db.models import Tenant  # noqa: E402
from app.services.message_deletion import (  # noqa: E402
    purge_expired_messages,
    recycle_bin_metrics,
    retry_media_purges,
)


def _current_time() -> datetime:
    return datetime.now(timezone.utc)


def run_message_purge_once(
    db: Session,
    *,
    limit: int = 200,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Purge expired recycle-bin messages for every tenant and commit."""
    run_at = now if now is not None else _current_time()
    summary = {"tenants": 0, "purged": 0, "media_retry_pending": 0, "media_retries_resolved": 0}

    tenant_ids = db.scalars(select(Tenant.id)).all()
    for tenant_id in tenant_ids:
        resolved = retry_media_purges(db, tenant_id, limit=limit, at=run_at)
        result = purge_expired_messages(db, tenant_id, limit=limit, at=run_at)
        summary["tenants"] += 1
        summary["purged"] += result.purged
        summary["media_retry_pending"] += result.media_retry_pending
        summary["media_retries_resolved"] += resolved
    db.commit()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge expired recycle-bin messages")
    parser.add_argument("--limit", type=int, default=200, help="Max messages per tenant per run")
    args = parser.parse_args()

    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 1
    engine = create_engine(url)
    with Session(engine) as db:
        summary = run_message_purge_once(db, limit=args.limit)
        for tenant_id in db.scalars(select(Tenant.id)).all():
            metrics = recycle_bin_metrics(db, tenant_id)
            print(
                f"tenant={tenant_id} pending_purge={metrics.pending_purge_count} "
                f"storage_retry_pending={metrics.storage_retry_pending} "
                f"oldest_pending_age_days={metrics.oldest_pending_age_days}"
            )
        print(
            "message_purge: "
            f"tenants={summary['tenants']} purged={summary['purged']} "
            f"media_retry_pending={summary['media_retry_pending']} "
            f"media_retries_resolved={summary['media_retries_resolved']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
