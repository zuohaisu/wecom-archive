#!/usr/bin/env python3
"""
Read-only pre-deployment integrity check for message_revocations
(RND-201, B4; round 4 QA fix).

Independently detects every data condition that would block Alembic
migration 0011 (alembic/versions/0011_message_revocations_tenant_
integrity.py) from applying tenant_id NOT NULL, one-row-per-revoke-event
uniqueness, and tenant-consistent composite foreign keys -- but this
script makes NO data changes and NEVER writes anything, so it is safe to
run against a live production database ahead of an actual migration run,
BEFORE 0011's constraints exist, to know in advance whether manual
remediation is needed first.

Deliberately does NOT depend on 0011's constraints already being
present: every check below is a plain SELECT/JOIN/GROUP BY against
columns that have existed since migration 0009, so this script gives an
identical, correct report whether the database is currently at revision
0010 or already at 0011 (a database already at 0011 simply reports all
zeros, since the constraints make every one of these conditions
impossible to write). Verified against both revisions -- see
tests/test_check_message_revocations_integrity.py.

Round 3 QA (B4) found two false-negative gaps, both fixed here and
covered by permanent regression tests:

  - A "linked" row whose original_message_id pointed at an archive_
    messages row that did not exist was reported as clean (only
    revoke_event_message_id was ever checked for a missing reference,
    never original_message_id). Fixed by find_missing_original_message_
    references(), a dedicated LEFT JOIN check independent of the
    tenant-mismatch check (an INNER JOIN there would silently exclude a
    missing row from BOTH checks -- see "Query correctness" below).

  - A row with status="bogus" (any value outside the three the
    database ever persists) was not detected at all -- the old
    "invalid status/original combination" check only compared against
    literal 'linked', so an unrecognized status with a NULL
    original_message_id satisfied that comparison vacuously. Fixed by
    find_unsupported_status_values(), a dedicated check against
    app.revoke_reconciliation.PERSISTED_STATUSES (the single
    authoritative source of the three real values -- pending, linked,
    malformed -- also used by that module itself and mirroring
    migration 0010's CHECK constraint; NOT redefined independently
    here, so it cannot silently drift out of sync). "missing" /
    "original_missing" is confirmed NOT a persisted value -- it is a
    request-time-only display label app.revoke_reconciliation.
    display_status() derives from an aged "pending" row (see that
    function) -- inserting status="missing" is therefore itself an
    unsupported-status violation, exactly like any other unrecognized
    string, and is covered by the same check and the same regression
    test class.

Query correctness (round 4 QA fix, "false negative" root cause pattern):
every "does the referenced row exist" check uses a LEFT JOIN + IS NULL
(never an INNER JOIN) so a missing referenced row is detected rather
than silently excluded from the result set entirely. Every "does the
referenced row's tenant match" check uses an INNER JOIN deliberately --
a mismatch check is only meaningful when a row to compare against
actually exists; a missing reference is reported once, by the dedicated
missing-reference check, not duplicated into the mismatch check as an
implicit NULL comparison.

Categories checked, all read-only (see main() for the exact print/exit
mapping):
  1. null_tenants                        -- message_revocations.tenant_id IS NULL,
                                             split into auto-recoverable (migration 0011
                                             will silently repair it) vs unrecoverable (blocking)
  2. duplicate_revoke_events              -- more than one row per revoke_event_message_id
  3. revoke_event_tenant_mismatches       -- tenant_id disagrees with an EXISTING revoke-event archive_messages row's tenant
  4. original_message_tenant_mismatches   -- tenant_id disagrees with an EXISTING original archive_messages row's tenant
  5. missing_revoke_event_references      -- revoke_event_message_id has no matching archive_messages row (round 1-3 check, retained)
  6. missing_original_message_references  -- original_message_id IS NOT NULL but has no matching archive_messages row (round 4 fix)
  7. unsupported_status_values            -- status is not one of app.revoke_reconciliation.PERSISTED_STATUSES (round 4 fix)
  8. invalid_status_original_combinations -- for a row whose status IS one of PERSISTED_STATUSES: linked
                                             requires original_message_id set; pending/malformed require it NULL
                                             (mirrors migration 0010's ck_message_revocations_linked_consistency)

A single row CAN legitimately appear in more than one category (e.g. a
"pending" row with a non-NULL original_message_id that also doesn't
exist is both an invalid_status_original_combination AND a missing_
original_message_reference -- both facts are useful for remediation).
total_blocking (the number that decides the exit code) is therefore the
SUM of category counts, not a count of distinct violating rows -- a row
violating two rules is counted twice. This is intentional and does not
change the PASS/FAIL outcome (any non-zero category already forces a
non-zero exit code); it only affects the printed total's magnitude,
which is documented here and in main()'s own output so it is never
misread as "N distinct corrupt rows."

Never prints decrypted message content, structured_content, raw
payloads, encryption keys, WeCom/Qiniu secrets, access tokens, signed
media URLs, local media paths, or any other sensitive field -- every
query below SELECTs only message_revocations.id/tenant_id/status/
revoke_event_message_id/original_message_id and archive_messages.
tenant_id, which is exactly the same non-sensitive identifying
information app.revoke_reconciliation and the timeline API already
expose to authorized tenant admins. DATABASE_URL (which may embed a
password) is never printed, including in the execution-failure path.

Strictly read-only: every query runs inside one `with engine.connect()
as conn:` block that is never committed -- only SELECT statements are
ever issued (plus the driver's own implicit BEGIN, never a write), so
closing the connection at the end of the `with` block discards
everything via the default auto-rollback-on-close behavior. No table,
index, or temp object is ever created; no INSERT/UPDATE/DELETE is ever
issued; migration 0011 is never invoked.

Usage (from backend/):
    python scripts/check_message_revocations_integrity.py               # all tenants
    python scripts/check_message_revocations_integrity.py --tenant-id X # one tenant

Required environment variables:
    DATABASE_URL   PostgreSQL connection string

Exit codes:
    0  No blocking violations found -- migration 0011 can safely proceed.
    1  Fatal initialisation failure (DATABASE_URL missing).
    2  One or more blocking violations found -- see the report for
       exactly which rows/categories, and migration 0011's own module
       docstring for the repair rules it will and will not apply
       automatically. Migration 0011 must NOT be run until this is 0.
    3  Unexpected execution error (DB connection refused, query failed,
       permissions error, or any other exception while querying) --
       distinct from exit code 2 so a genuine execution failure can
       never be mistaken for "checked and found clean." Never exits 0
       after a query failure.
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from app.revoke_reconciliation import PERSISTED_STATUSES


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Missing required environment variable: {name}", flush=True)
        sys.exit(1)
    return value


def _tenant_clause(alias: str, tenant_id: "str | None") -> tuple:
    """Returns (sql_fragment, params) -- an optional extra WHERE fragment
    scoping a query to one tenant, matched against message_revocations'
    OWN tenant_id column under the given alias (never against
    archive_messages -- a NULL-tenant row must still be found regardless
    of what tenant it is eventually attributed to)."""
    if tenant_id is None:
        return "", {}
    return f" AND {alias}.tenant_id = :scope_tenant_id", {"scope_tenant_id": tenant_id}


def find_null_tenant_rows(conn, tenant_id: "str | None" = None) -> "tuple[list, list]":
    """Mirrors migration 0011's Step 1 exactly: a NULL-tenant row is
    "recoverable" (the migration silently fixes it, informational only,
    never blocking) when its revoke_event_message_id resolves to a real
    archive_messages row that itself has a non-NULL tenant_id;
    everything else is "unrecoverable" (blocking -- the migration will
    abort on it). --tenant-id cannot scope this query by mr.tenant_id
    (it's NULL by definition here); a NULL-tenant row is always reported
    regardless of --tenant-id since its eventual tenant is exactly what's
    unknown.
    """
    rows = conn.execute(
        text(
            "SELECT mr.id, mr.revoke_event_message_id, mr.status, "
            "am.tenant_id AS recoverable_tenant_id "
            "FROM message_revocations mr "
            "LEFT JOIN archive_messages am ON am.id = mr.revoke_event_message_id "
            "WHERE mr.tenant_id IS NULL "
            "ORDER BY mr.id"
        )
    ).fetchall()
    recoverable = [dict(r._mapping) for r in rows if r._mapping["recoverable_tenant_id"] is not None]
    unrecoverable = [dict(r._mapping) for r in rows if r._mapping["recoverable_tenant_id"] is None]
    return recoverable, unrecoverable


def find_revoke_event_tenant_mismatches(conn, tenant_id: "str | None" = None) -> list:
    """INNER JOIN is deliberate here, not a bug: this check only asks
    "does an EXISTING revoke-event row's tenant disagree with ours" --
    a revoke_event_message_id with no matching archive_messages row at
    all is find_missing_revoke_event_references()'s responsibility
    alone, so it must not silently surface here as a side effect of a
    failed join (that would report the same corruption twice under two
    different, confusing labels).

    Also deliberately a plain "!=" comparison, not "IS DISTINCT FROM":
    a NULL mr.tenant_id makes "!=" evaluate to NULL (excluded by WHERE),
    which is intentional -- a NULL-tenant row is find_null_tenant_rows()'s
    responsibility alone (it is an absence to recover, not a
    disagreement to report), and double-reporting it here as well would
    inflate the blocking count for a row migration 0011 will actually
    auto-repair successfully.
    """
    clause, params = _tenant_clause("mr", tenant_id)
    rows = conn.execute(
        text(
            "SELECT mr.id, mr.tenant_id AS declared_tenant_id, mr.status, "
            "am.tenant_id AS revoke_event_tenant_id, mr.revoke_event_message_id "
            "FROM message_revocations mr "
            "JOIN archive_messages am ON am.id = mr.revoke_event_message_id "
            f"WHERE mr.tenant_id != am.tenant_id{clause} "
            "ORDER BY mr.id"
        ),
        params,
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def find_original_message_tenant_mismatches(conn, tenant_id: "str | None" = None) -> list:
    """INNER JOIN + plain "!=" for the same reasons as
    find_revoke_event_tenant_mismatches() above: a missing
    original_message_id reference is find_missing_original_message_
    references()'s responsibility alone, not this function's."""
    clause, params = _tenant_clause("mr", tenant_id)
    rows = conn.execute(
        text(
            "SELECT mr.id, mr.tenant_id AS declared_tenant_id, mr.status, "
            "am.tenant_id AS original_tenant_id, mr.original_message_id "
            "FROM message_revocations mr "
            "JOIN archive_messages am ON am.id = mr.original_message_id "
            f"WHERE mr.original_message_id IS NOT NULL "
            f"AND mr.tenant_id != am.tenant_id{clause} "
            "ORDER BY mr.id"
        ),
        params,
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def find_duplicate_revoke_event_associations(conn, tenant_id: "str | None" = None) -> list:
    """Groups by revoke_event_message_id -- the globally unique
    identifier migration 0011's UNIQUE(revoke_event_message_id)
    constraint enforces (NOT the composite (tenant_id,
    revoke_event_message_id) migration 0009 originally shipped, which a
    same-event-different-tenant duplicate could otherwise slip past)."""
    clause, params = _tenant_clause("mr", tenant_id)
    rows = conn.execute(
        text(
            "SELECT mr.revoke_event_message_id, COUNT(*) AS row_count, "
            "array_agg(mr.id ORDER BY mr.id) AS message_revocation_ids, "
            "array_agg(DISTINCT mr.tenant_id) AS distinct_tenant_ids "
            "FROM message_revocations mr "
            f"WHERE 1=1{clause} "
            "GROUP BY mr.revoke_event_message_id "
            "HAVING COUNT(*) > 1 "
            "ORDER BY mr.revoke_event_message_id"
        ),
        params,
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def find_missing_revoke_event_references(conn, tenant_id: "str | None" = None) -> list:
    """LEFT JOIN + IS NULL (never INNER JOIN) -- a revoke_event_
    message_id with no matching archive_messages row must be detected,
    not silently excluded from the result set by a failed join."""
    clause, params = _tenant_clause("mr", tenant_id)
    rows = conn.execute(
        text(
            "SELECT mr.id, mr.tenant_id, mr.status, mr.revoke_event_message_id "
            "FROM message_revocations mr "
            "LEFT JOIN archive_messages am ON am.id = mr.revoke_event_message_id "
            f"WHERE am.id IS NULL{clause} "
            "ORDER BY mr.id"
        ),
        params,
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def find_missing_original_message_references(conn, tenant_id: "str | None" = None) -> list:
    """RND-201 round 4 QA fix (B4 false negative #1): the exact check
    that was previously missing entirely. LEFT JOIN + IS NULL, scoped to
    rows that actually declare an original_message_id (NULL is a valid,
    unresolved state for pending/malformed rows, never itself a
    violation of this check) -- a "linked" row (or any row) whose
    original_message_id points at a non-existent archive_messages row is
    reported here, independent of find_original_message_tenant_
    mismatches() above, which would otherwise silently miss it entirely
    (an INNER JOIN produces no row at all when the reference is
    missing, which is exactly how this was missed in round 3)."""
    clause, params = _tenant_clause("mr", tenant_id)
    rows = conn.execute(
        text(
            "SELECT mr.id, mr.tenant_id, mr.status, mr.original_message_id "
            "FROM message_revocations mr "
            "LEFT JOIN archive_messages am ON am.id = mr.original_message_id "
            f"WHERE mr.original_message_id IS NOT NULL AND am.id IS NULL{clause} "
            "ORDER BY mr.id"
        ),
        params,
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def find_unsupported_status_values(conn, tenant_id: "str | None" = None) -> list:
    """RND-201 round 4 QA fix (B4 false negative #2): the exact check
    that was previously missing entirely. Checks against
    app.revoke_reconciliation.PERSISTED_STATUSES -- the single
    authoritative set of values the status column is ever persisted as
    -- rather than a second, independently-maintained list that could
    silently drift out of sync with that module or migration 0010's
    CHECK constraint. Catches any value outside {"pending", "linked",
    "malformed"}, including "missing"/"original_missing" (confirmed
    display-only, never persisted -- see app.revoke_reconciliation.
    display_status()) and an empty string (the status column is NOT
    NULL from migration 0009 onward, but that does not forbid '')."""
    clause, params = _tenant_clause("mr", tenant_id)
    status_params = {f"status_{i}": s for i, s in enumerate(sorted(PERSISTED_STATUSES))}
    placeholders = ", ".join(f":{name}" for name in status_params)
    rows = conn.execute(
        text(
            "SELECT mr.id, mr.tenant_id, mr.status, mr.original_message_id "
            "FROM message_revocations mr "
            f"WHERE mr.status NOT IN ({placeholders}){clause} "
            "ORDER BY mr.id"
        ),
        {**status_params, **params},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def find_invalid_status_original_combinations(conn, tenant_id: "str | None" = None) -> list:
    """Restricted to rows whose status IS one of PERSISTED_STATUSES --
    an unsupported status is find_unsupported_status_values()'s
    responsibility alone (round 4 QA fix: the previous version's
    (status = 'linked') != (original_message_id IS NOT NULL) formula
    only ever compared against the literal 'linked', so a row with an
    unrecognized status and a NULL original_message_id satisfied it
    vacuously and was never reported by ANY check -- this is exactly
    the round 3 false-negative). Mirrors migration 0010's
    ck_message_revocations_linked_consistency CHECK constraint via three
    explicit per-status rules rather than one compressed boolean
    formula, so each rule is independently traceable to this module's
    docstring and to MessageRevocation's own status documentation:
      linked    requires original_message_id IS NOT NULL
      pending   requires original_message_id IS NULL
      malformed requires original_message_id IS NULL
    """
    clause, params = _tenant_clause("mr", tenant_id)
    status_params = {f"status_{i}": s for i, s in enumerate(sorted(PERSISTED_STATUSES))}
    placeholders = ", ".join(f":{name}" for name in status_params)
    rows = conn.execute(
        text(
            "SELECT mr.id, mr.tenant_id, mr.status, mr.original_message_id "
            "FROM message_revocations mr "
            f"WHERE mr.status IN ({placeholders}) "
            "AND ("
            "  (mr.status = 'linked' AND mr.original_message_id IS NULL)"
            "  OR (mr.status = 'pending' AND mr.original_message_id IS NOT NULL)"
            "  OR (mr.status = 'malformed' AND mr.original_message_id IS NOT NULL)"
            f")"
            f"{clause} "
            "ORDER BY mr.id"
        ),
        {**status_params, **params},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def run_integrity_check(conn, tenant_id: "str | None" = None) -> dict:
    """Runs every check and returns the complete result as plain data
    (no printing, no exit calls) -- the single place main() and tests
    both call, so a test exercises exactly the same code path the CLI
    uses, not a reconstructed approximation of it."""
    null_tenant_recoverable, null_tenant_unrecoverable = find_null_tenant_rows(conn, tenant_id)
    return {
        "null_tenant_recoverable": null_tenant_recoverable,
        "null_tenant_unrecoverable": null_tenant_unrecoverable,
        "duplicate_revoke_events": find_duplicate_revoke_event_associations(conn, tenant_id),
        "revoke_event_tenant_mismatches": find_revoke_event_tenant_mismatches(conn, tenant_id),
        "original_message_tenant_mismatches": find_original_message_tenant_mismatches(conn, tenant_id),
        "missing_revoke_event_references": find_missing_revoke_event_references(conn, tenant_id),
        "missing_original_message_references": find_missing_original_message_references(conn, tenant_id),
        "unsupported_status_values": find_unsupported_status_values(conn, tenant_id),
        "invalid_status_original_combinations": find_invalid_status_original_combinations(conn, tenant_id),
    }


# Category key -> (blocking?, human label). null_tenant_recoverable is
# the only category that is informational-only, never blocking (see
# find_null_tenant_rows()'s docstring) -- every other category always
# contributes to total_blocking.
_REPORT_CATEGORIES = (
    ("null_tenant_recoverable", False, "null_tenant_recoverable"),
    ("null_tenant_unrecoverable", True, "null_tenants"),
    ("duplicate_revoke_events", True, "duplicate_revoke_events"),
    ("revoke_event_tenant_mismatches", True, "revoke_event_tenant_mismatches"),
    ("original_message_tenant_mismatches", True, "original_message_tenant_mismatches"),
    ("missing_revoke_event_references", True, "missing_revoke_event_references"),
    ("missing_original_message_references", True, "missing_original_message_references"),
    ("unsupported_status_values", True, "unsupported_status_values"),
    ("invalid_status_original_combinations", True, "invalid_status_original_combinations"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="Scope the report to a single tenant (default: every tenant)",
    )
    args = parser.parse_args()

    database_url = _require_env("DATABASE_URL")

    try:
        engine = create_engine(database_url)
        # Strictly read-only: this connection is never committed -- only
        # SELECT statements are issued below, so closing it at the end of
        # this `with` block discards everything via the driver's default
        # rollback-on-close behavior. No table/index/temp object is ever
        # created; migration 0011 is never invoked.
        with engine.connect() as conn:
            results = run_integrity_check(conn, args.tenant_id)
        engine.dispose()
    except SystemExit:
        raise
    except Exception as exc:
        # Deliberately does not print database_url (which may embed a
        # password) or the raw exception's full repr if it could echo
        # connection parameters -- just the exception type/message,
        # which for SQLAlchemy/psycopg2 errors is the server's own
        # diagnostic text, not a client-side credential dump.
        print(f"[FAIL] integrity_check execution error: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(3)

    print(f"[INFO] integrity_check tenant_id: {args.tenant_id or 'ALL'}", flush=True)

    total_blocking = 0
    for key, blocking, label in _REPORT_CATEGORIES:
        rows = results[key]
        suffix = "" if blocking else " (migration 0011 will silently fix these -- informational only, never blocking)"
        print(f"[INFO] integrity_check {label}: {len(rows)}{suffix}", flush=True)
        for row in rows[:20]:
            print(f"[DETAIL] {label}: {row}", flush=True)
        if blocking:
            total_blocking += len(rows)

    print(
        f"[INFO] integrity_check total_blocking: {total_blocking} "
        "(sum of blocking category counts above -- a single row violating "
        "more than one rule is counted once per rule, not once overall; "
        "see this script's module docstring for why)",
        flush=True,
    )

    if total_blocking:
        print(
            f"[FAIL] integrity_check found {total_blocking} blocking violation(s) across the "
            "categories above -- migration 0011 will abort with a matching diagnostic if run "
            "as-is. Resolve manually before migrating (see this script's module docstring for "
            "the exact repair rules migration 0011 itself will and will not apply automatically).",
            flush=True,
        )
        sys.exit(2)

    print("[PASS] check_message_revocations_integrity found no blocking violations", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
