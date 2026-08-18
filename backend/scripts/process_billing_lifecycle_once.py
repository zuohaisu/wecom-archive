#!/usr/bin/env python3
"""Advance one bounded batch of commercial tenants' billing lifecycle.

Scans only tenants that already have a Subscription (legacy/self-host
tenants are never touched), reconciles each tenant's authoritative
Subscription / Tenant service projection in its own row-locked
transaction with bounded retries, and prints an identifier-free summary.
Replay-safe: running twice applies no duplicate transitions.

Usage (from backend/):
    python scripts/process_billing_lifecycle_once.py

Required environment variables:
    DATABASE_URL

Optional environment variables:
    BILLING_LIFECYCLE_BATCH_LIMIT   Max tenants scanned per run (default 50)
    BILLING_LIFECYCLE_BATCH_RETRIES Bounded per-tenant retry count (default 2)

Exit codes:
    0  Batch completed (failed tenants are counted, never fatal)
    1  Fatal failure (missing configuration or unhandled batch error)

Safety:
    - Output contains aggregate counts only — never tenant identifiers,
      subscription values, or message content.
    - One tenant's failure never aborts the scan of the others.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.billing_lifecycle_batch import (  # noqa: E402
    DEFAULT_BATCH_RETRIES,
    batch_summary_line,
    run_lifecycle_batch_once,
)


def _bounded_int_env(name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(os.environ.get(name, str(default)))))
    except ValueError:
        return default


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] billing_lifecycle configuration_missing", flush=True)
        return 1
    limit = _bounded_int_env("BILLING_LIFECYCLE_BATCH_LIMIT", 50, 500)
    retries = _bounded_int_env(
        "BILLING_LIFECYCLE_BATCH_RETRIES", DEFAULT_BATCH_RETRIES, 5
    )
    try:
        with Session(create_engine(database_url)) as db:
            summary = run_lifecycle_batch_once(db, limit=limit, retries=retries)
    except Exception:  # noqa: BLE001 - never expose URLs or identifiers
        print("[FAIL] billing_lifecycle batch_failed", flush=True)
        return 1
    print(f"[INFO] billing_lifecycle batch_completed {batch_summary_line(summary)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
