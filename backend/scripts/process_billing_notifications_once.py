#!/usr/bin/env python3
"""Plan and deliver one bounded batch of SaaS billing notifications."""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.billing_notifications import run_billing_notifications_once


def _batch_size() -> int:
    try:
        return max(
            1,
            min(100, int(os.environ.get("BILLING_NOTIFICATION_BATCH_SIZE", "20"))),
        )
    except ValueError:
        return 20


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] billing_notifications configuration_missing", flush=True)
        return 1
    try:
        with Session(create_engine(database_url)) as db:
            summary = run_billing_notifications_once(db, limit=_batch_size())
    except Exception:  # noqa: BLE001 - never expose URLs or billing identifiers
        print("[FAIL] billing_notifications processing_failed", flush=True)
        return 1
    print(
        "[INFO] billing_notifications processing_completed "
        f"scheduled={summary.scheduled} canceled={summary.canceled} "
        f"sent={summary.sent} retried={summary.retried} "
        f"failed={summary.failed} deferred={summary.deferred}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
