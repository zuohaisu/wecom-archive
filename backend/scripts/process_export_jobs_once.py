#!/usr/bin/env python3
"""Generate queued media exports, send notices and delete expired artifacts."""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.export_jobs import run_export_maintenance_once


def _batch_size() -> int:
    try:
        return max(1, min(5, int(os.environ.get("EXPORT_JOB_BATCH_SIZE", "1"))))
    except ValueError:
        return 1


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] export_jobs configuration_missing", flush=True)
        return 1
    try:
        with Session(create_engine(database_url)) as db:
            summary = run_export_maintenance_once(
                db, generation_limit=_batch_size()
            )
    except Exception:  # noqa: BLE001 - never expose URLs, paths or identifiers
        print("[FAIL] export_jobs maintenance_failed", flush=True)
        return 1
    print(
        "[INFO] export_jobs maintenance_completed "
        f"claimed={summary.claimed} ready={summary.ready} "
        f"retried={summary.retried} failed={summary.failed} "
        f"notifications_sent={summary.notifications_sent} "
        f"notifications_failed={summary.notifications_failed} "
        f"expired={summary.expired} cleanup_pending={summary.cleanup_pending} "
        f"blocked={summary.blocked}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
