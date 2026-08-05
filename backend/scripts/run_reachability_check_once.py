#!/usr/bin/env python3
"""Run one persistent reachability check addressed only by its public id.

Usage (from backend/):
    python scripts/run_reachability_check_once.py <public-run-id>

The tenant and frozen scope are intentionally resolved from the run row; they
must never be passed on the command line or printed by this script.
"""

from __future__ import annotations

import argparse
import os
import sys

# Direct script execution sets sys.path[0] to backend/scripts rather than
# backend. Keep the production systemd/manual invocation contract while making
# the sibling app package importable, matching the other worker entrypoints.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.reachability_check_service import execute_run


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one reachability check")
    parser.add_argument("public_run_id")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] DATABASE_URL is not set", flush=True)
        return 1
    try:
        with Session(create_engine(database_url)) as db:
            outcome = execute_run(db, args.public_run_id)
    except Exception:
        # Do not emit a database URL, raw exception, or traceback.
        print("[FAIL] reachability check could not run", flush=True)
        return 1
    if outcome != "completed":
        print("[FAIL] reachability check did not complete", flush=True)
        return 1
    print("[PASS] reachability check completed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
