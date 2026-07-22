#!/usr/bin/env python3
"""
Non-interactive Alembic revision verification gate (RND-227, P0-B).

`alembic upgrade head` exiting 0 is necessary but not sufficient proof
that the database is actually on the repository's migration head — this
script makes that comparison explicit and machine-checkable so
scripts/deploy_server.sh can gate the service restart on it rather than
trusting the upgrade command's exit code alone.

Covers:
  - the common single-head case
  - a deliberate multi-head branch (db revision set must equal the full
    repo head set, not just intersect it)
  - a database with no alembic_version row at all (never migrated)
  - the database being unreachable (network, auth, wrong DATABASE_URL)

Never prints a connection string or full exception text — only revision
ids (already public in git) and, on connection failure, the exception's
type name.

Usage (from backend/, with DATABASE_URL set and the venv active):
    python scripts/verify_alembic_head.py

Exit codes:
    0  database current revision(s) == repository head revision(s)
    1  mismatch, no revision applied, DATABASE_URL unset, or DB unreachable
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402

from app.db.schema_check import check_revision  # noqa: E402


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 1

    try:
        engine = create_engine(database_url)
        with engine.connect() as connection:
            ok, message, _db_revs, _repo_revs = check_revision(connection)
    except Exception as exc:
        print(
            f"ERROR: could not verify database revision ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1

    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
