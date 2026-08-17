#!/usr/bin/env python3
"""Delete raw Product Analytics events older than the v1 retention period.

Run from ``backend/`` under the deployment scheduler, for example:

    DATABASE_URL=... python scripts/purge_product_analytics_events_once.py

The job only deletes ``product_analytics_events``.  It never changes audit
logs, archive messages, media, sessions, or tenant records.
"""

from __future__ import annotations

import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.product_analytics import purge_expired_events  # noqa: E402


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] DATABASE_URL is required", flush=True)
        raise SystemExit(1)
    try:
        with Session(create_engine(database_url)) as db:
            deleted = purge_expired_events(db)
            db.commit()
    except Exception as error:
        print(f"[FAIL] product analytics cleanup failed: {type(error).__name__}", flush=True)
        raise SystemExit(1)
    print(f"[PASS] purged {deleted} expired product analytics events", flush=True)


if __name__ == "__main__":
    main()
