#!/usr/bin/env python3
"""Record retention locks for messages past configured tenant cutoffs.

Usage (from ``backend/``)::

    DATABASE_URL=... python scripts/apply_retention_lock_once.py

Only tenants with a ``retention_configs`` row are considered. Each newly
expired message receives one row in ``retention_locks``; reruns exclude rows
already recorded there. Deployment-side scheduling (for example, cron)
invokes this script; no application scheduler is involved.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import create_engine, exists, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import write_audit  # noqa: E402
from app.db.models import ArchiveMessage, RetentionConfig, RetentionLock  # noqa: E402


def _current_time() -> datetime:
    return datetime.now(timezone.utc)


def _epoch_milliseconds(value: datetime) -> int:
    """Convert a retention cutoff to the archive's epoch-millisecond format."""
    return int(value.timestamp() * 1000)


def run_retention_lock_once(
    db: Session, *, now: Optional[datetime] = None
) -> dict[str, int]:
    """Record newly expired messages for every configured tenant and commit.

    Every returned count is the number of locks inserted during this run, not
    the tenant's cumulative number of locks.
    """
    run_at = now if now is not None else _current_time()
    summary: dict[str, int] = {}

    for config in db.scalars(select(RetentionConfig)):
        cutoff = run_at - timedelta(days=config.retention_days)
        lock_exists = exists(
            select(RetentionLock.id).where(
                RetentionLock.archive_message_id == ArchiveMessage.id
            )
        )
        message_ids = db.scalars(
            select(ArchiveMessage.id).where(
                ArchiveMessage.tenant_id == config.tenant_id,
                ArchiveMessage.msgtime < _epoch_milliseconds(cutoff),
                ~lock_exists,
            )
        ).all()
        locked_count = len(message_ids)
        summary[config.tenant_id] = locked_count

        if locked_count > 0:
            db.add_all(
                RetentionLock(
                    id=str(uuid.uuid4()),
                    tenant_id=config.tenant_id,
                    archive_message_id=message_id,
                    locked_at=run_at,
                )
                for message_id in message_ids
            )
            write_audit(
                db,
                tenant_id=config.tenant_id,
                action="retention.messages_locked",
                object_type="tenant",
                detail={"locked_count": locked_count, "cutoff": cutoff.isoformat()},
            )

    db.commit()
    return summary


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable not set or empty: {name}")
    return value


def main() -> None:
    try:
        database_url = _require_env("DATABASE_URL")
        with Session(create_engine(database_url)) as db:
            summary = run_retention_lock_once(db)
    except RuntimeError as exc:
        print(f"[FAIL] {exc}", flush=True)
        raise SystemExit(1)

    print(f"[INFO] retention lock summary: {summary}", flush=True)
    print("[PASS] apply_retention_lock_once completed", flush=True)


if __name__ == "__main__":
    main()
