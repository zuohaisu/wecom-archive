"""
Tests for RND-201 round 4 QA fix — scripts/check_message_revocations_
integrity.py.

Round 3 QA found two false-negative gaps in the checker: a "linked" row
whose original_message_id pointed at a non-existent archive_messages row
was reported as clean, and a row with an unsupported status value (e.g.
"bogus") was reported as clean. Both together produced a false PASS
(exit code 0) on a database that actually contained blocking corruption.

These tests exercise the checker's REAL entry point (scripts/check_
message_revocations_integrity.py invoked as a subprocess, exactly as an
operator would run it) against a real, disposable Postgres database
migrated to revision 0010 -- the schema state the checker is meant to
validate BEFORE migration 0011 runs, and the same convention used by
test_message_revocations_tenant_integrity.py's own migration-behavior
tests (imported from here rather than duplicated -- see the imports
below, matching this repo's established cross-file fixture-import
convention).

Fixture construction note: migration 0009 already added single-column
foreign keys for revoke_event_message_id/original_message_id, and
migration 0010 already added the status CHECK constraint -- both, still
present at revision 0010, block several of the corrupted fixtures these
tests need to construct directly via INSERT. Where that happens, the
specific 0009/0010-era constraint is temporarily dropped, the fixture
row(s) inserted, and (where PostgreSQL supports it -- CHECK and FOREIGN
KEY, but not UNIQUE) the constraint is re-added as NOT VALID immediately
after, all within a single disposable scratch database created fresh
per test and destroyed at teardown -- no shared or production database
is ever touched, matching the "clearly isolates and restores" bar this
ticket sets. This models realistic pre-migration corruption: data
written before 0009/0010 existed, restored from an old backup, or
produced by direct SQL/tooling outside the ORM entirely.

Run (from backend/):
    pytest tests/test_check_message_revocations_integrity.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from tests.test_message_revocations_tenant_integrity import (  # noqa: F401 -- reused fixtures/helpers, established cross-file convention
    _drop_database,
    _fresh_database,
    _postgres_reachable,
    _run_alembic,
    _seed_tenants_and_messages,
    pytestmark,
)

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_CHECKER_SCRIPT = "scripts/check_message_revocations_integrity.py"


def _run_checker(env: dict, *args: str):
    return subprocess.run(
        [sys.executable, _CHECKER_SCRIPT, *args],
        cwd=str(_BACKEND_DIR), env=env, capture_output=True, text=True, timeout=60,
    )


def _drop_constraint(engine, name: str) -> None:
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text(f"ALTER TABLE message_revocations DROP CONSTRAINT {name}"))


def _restore_fk_not_valid(engine, name: str, column: str) -> None:
    """NOT VALID re-attaches the constraint for all FUTURE writes without
    re-validating the rows already in the table (which -- deliberately --
    still violate it; that's the fixture). Postgres supports NOT VALID
    for FOREIGN KEY and CHECK constraints, not UNIQUE -- see
    _seed_duplicate_without_restoring_unique() for why the one duplicate-
    association test doesn't attempt to restore its dropped constraint."""
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text(
                    f"ALTER TABLE message_revocations ADD CONSTRAINT {name} "
                    f"FOREIGN KEY ({column}) REFERENCES archive_messages(id) NOT VALID"
                )
            )


def _restore_status_check_not_valid(engine) -> None:
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text(
                    "ALTER TABLE message_revocations ADD CONSTRAINT ck_message_revocations_status_valid "
                    "CHECK (status IN ('pending','linked','malformed')) NOT VALID"
                )
            )


def _insert_row(engine, **kwargs) -> None:
    defaults = {"tenant_id": "tenant-a", "revoke_event_msgid": "ev", "status": "pending", "original_message_id": None}
    defaults.update(kwargs)
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text(
                    "INSERT INTO message_revocations "
                    "(tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
                    "VALUES (:tenant_id, :revoke_event_message_id, :revoke_event_msgid, :status, :original_message_id)"
                ),
                defaults,
            )


def _setup(db_name: str):
    """Fresh scratch DB, migrated to 0010, with two tenant-a archive
    messages (ids "id0"/"id1") and one tenant-b message ("id2") seeded --
    covers every fixture these tests need without re-deriving ids
    per-test. Returns (engine, env, ids)."""
    target_dsn = _fresh_database(db_name)
    env = dict(os.environ, DATABASE_URL=target_dsn)
    assert _run_alembic(_BACKEND_DIR, env, "upgrade", "0010").returncode == 0

    engine = create_engine(target_dsn)
    with engine.connect() as conn:
        with conn.begin():
            ids = _seed_tenants_and_messages(conn, ["tenant-a", "tenant-a", "tenant-b"])
    return engine, env, ids


# ---------------------------------------------------------------------------
# 1. Clean database
# ---------------------------------------------------------------------------


def test_clean_database_passes_with_exit_zero():
    db_name = "rnd201_checker_clean"
    engine, env, ids = _setup(db_name)
    try:
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-clean", status="pending")
        result = _run_checker(env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "[PASS]" in result.stdout
        assert "total_blocking: 0" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# 2. Missing revoke-event reference
# ---------------------------------------------------------------------------


def test_missing_revoke_event_reference_detected():
    db_name = "rnd201_checker_missing_event_ref"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "message_revocations_revoke_event_message_id_fkey")
        _insert_row(engine, revoke_event_message_id=999999, revoke_event_msgid="ev-ghost", status="pending")
        _restore_fk_not_valid(engine, "message_revocations_revoke_event_message_id_fkey", "revoke_event_message_id")

        result = _run_checker(env)
        assert result.returncode != 0
        assert "missing_revoke_event_references: 1" in result.stdout
        assert "999999" in result.stdout  # the identifier, for remediation
        assert "content_text" not in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# 3. Missing original-message reference (the exact round 3 regression)
# ---------------------------------------------------------------------------


def test_missing_original_message_reference_detected():
    """This exact scenario (status=linked, original_message_id pointing
    at a non-existent archive_messages row) was missed entirely in round
    3 and must never regress."""
    db_name = "rnd201_checker_missing_original_ref"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "message_revocations_original_message_id_fkey")
        _insert_row(
            engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-missing-original",
            status="linked", original_message_id=999999,
        )
        _restore_fk_not_valid(engine, "message_revocations_original_message_id_fkey", "original_message_id")

        result = _run_checker(env)
        assert result.returncode != 0
        assert "missing_original_message_references: 1" in result.stdout
        assert "999999" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# 4. Unsupported status (the other exact round 3 regression)
# ---------------------------------------------------------------------------


def test_unsupported_status_value_detected():
    db_name = "rnd201_checker_unsupported_status"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "ck_message_revocations_status_valid")
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-bogus", status="bogus")
        _restore_status_check_not_valid(engine)

        result = _run_checker(env)
        assert result.returncode != 0
        assert "unsupported_status_values: 1" in result.stdout
        assert "bogus" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# 5. Display-only "missing" status is not a persisted value
# ---------------------------------------------------------------------------


def test_display_only_missing_status_is_flagged_as_unsupported():
    """Confirms repository semantics directly: "missing"/"original_
    missing" is never a persisted status -- app.revoke_reconciliation.
    display_status() derives it at request time from an aged "pending"
    row (see that function). Inserting status="missing" is therefore an
    unsupported-status violation like any other unrecognized string."""
    from app.revoke_reconciliation import PERSISTED_STATUSES

    assert "missing" not in PERSISTED_STATUSES
    assert "original_missing" not in PERSISTED_STATUSES

    db_name = "rnd201_checker_display_only_missing"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "ck_message_revocations_status_valid")
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-missing-status", status="missing")
        _restore_status_check_not_valid(engine)

        result = _run_checker(env)
        assert result.returncode != 0
        assert "unsupported_status_values: 1" in result.stdout
        assert "'status': 'missing'" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# 6-8. Invalid status/original combinations
# ---------------------------------------------------------------------------


def test_linked_without_original_detected():
    db_name = "rnd201_checker_linked_no_original"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "ck_message_revocations_linked_consistency")
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-linked-no-orig", status="linked", original_message_id=None)
        with engine.connect() as conn:
            with conn.begin():
                conn.execute(text(
                    "ALTER TABLE message_revocations ADD CONSTRAINT ck_message_revocations_linked_consistency "
                    "CHECK ((status = 'linked') = (original_message_id IS NOT NULL)) NOT VALID"
                ))

        result = _run_checker(env)
        assert result.returncode != 0
        assert "invalid_status_original_combinations: 1" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


def test_pending_with_original_detected():
    db_name = "rnd201_checker_pending_with_original"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "ck_message_revocations_linked_consistency")
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-pending-with-orig", status="pending", original_message_id=ids["id1"])
        with engine.connect() as conn:
            with conn.begin():
                conn.execute(text(
                    "ALTER TABLE message_revocations ADD CONSTRAINT ck_message_revocations_linked_consistency "
                    "CHECK ((status = 'linked') = (original_message_id IS NOT NULL)) NOT VALID"
                ))

        result = _run_checker(env)
        assert result.returncode != 0
        assert "invalid_status_original_combinations: 1" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


def test_malformed_with_original_detected():
    db_name = "rnd201_checker_malformed_with_original"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "ck_message_revocations_linked_consistency")
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-malformed-with-orig", status="malformed", original_message_id=ids["id1"])
        with engine.connect() as conn:
            with conn.begin():
                conn.execute(text(
                    "ALTER TABLE message_revocations ADD CONSTRAINT ck_message_revocations_linked_consistency "
                    "CHECK ((status = 'linked') = (original_message_id IS NOT NULL)) NOT VALID"
                ))

        result = _run_checker(env)
        assert result.returncode != 0
        assert "invalid_status_original_combinations: 1" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# 9-12. Pre-existing detections must continue to work
# ---------------------------------------------------------------------------


def test_null_tenant_still_detected():
    db_name = "rnd201_checker_null_tenant"
    engine, env, ids = _setup(db_name)
    try:
        # Unrecoverable: revoke_event_message_id's own archive_messages
        # row also has NULL tenant_id, so it cannot be auto-repaired.
        with engine.connect() as conn:
            with conn.begin():
                conn.execute(text("UPDATE archive_messages SET tenant_id = NULL WHERE id = :id0"), ids)
        _insert_row(engine, tenant_id=None, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-null-tenant")

        result = _run_checker(env)
        assert result.returncode != 0
        assert "null_tenants: 1" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


def test_duplicate_revoke_event_still_detected():
    """The composite UNIQUE(tenant_id, revoke_event_message_id)
    constraint (still present at 0010) blocks a direct same-tenant
    duplicate insert -- dropped here to construct the fixture, and NOT
    restored afterward (Postgres does not support NOT VALID for UNIQUE
    constraints, and re-adding one over already-duplicate rows would
    fail immediate validation). The scratch database is destroyed at
    teardown regardless, so this never leaves any shared state
    under-constrained."""
    db_name = "rnd201_checker_duplicate_event"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "uq_message_revocations_tenant_revoke_event")
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-dup-1")
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-dup-2")

        result = _run_checker(env)
        assert result.returncode != 0
        assert "duplicate_revoke_events: 1" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


def test_cross_tenant_revoke_event_still_detected():
    """No 0010-era constraint blocks a tenant/revoke-event mismatch
    (that guarantee is exactly what migration 0011 adds) -- constructible
    with a plain insert, no constraint manipulation needed."""
    db_name = "rnd201_checker_cross_tenant_event"
    engine, env, ids = _setup(db_name)
    try:
        _insert_row(engine, tenant_id="tenant-a", revoke_event_message_id=ids["id2"], revoke_event_msgid="ev-cross-tenant")

        result = _run_checker(env)
        assert result.returncode != 0
        assert "revoke_event_tenant_mismatches: 1" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


def test_cross_tenant_original_still_detected():
    db_name = "rnd201_checker_cross_tenant_original"
    engine, env, ids = _setup(db_name)
    try:
        _insert_row(
            engine, tenant_id="tenant-a", revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-cross-orig",
            status="linked", original_message_id=ids["id2"],
        )

        result = _run_checker(env)
        assert result.returncode != 0
        assert "original_message_tenant_mismatches: 1" in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# Combined Round 3 corruption fixture -- reproduces the exact false PASS
# ---------------------------------------------------------------------------


def test_combined_round_3_corruption_no_longer_false_passes():
    """The exact combination round 3 QA reported as a false PASS: one
    missing-original-reference row and one unsupported-status row,
    checked in a single checker invocation. Before the round 4 fix,
    total_blocking was 0 and exit code was 0 for this fixture -- see the
    round 4 development report for reconstructed "before" evidence
    against the pre-fix query logic (not re-tested here, since that code
    no longer exists to invoke)."""
    db_name = "rnd201_checker_combined_r3"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "message_revocations_original_message_id_fkey")
        _drop_constraint(engine, "ck_message_revocations_status_valid")
        _insert_row(
            engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-missing-original",
            status="linked", original_message_id=999999,
        )
        _insert_row(engine, revoke_event_message_id=ids["id1"], revoke_event_msgid="ev-bogus-status", status="bogus")
        _restore_fk_not_valid(engine, "message_revocations_original_message_id_fkey", "original_message_id")
        _restore_status_check_not_valid(engine)

        result = _run_checker(env)
        assert result.returncode != 0, "combined round 3 fixture must not false-pass"
        assert "missing_original_message_references: 1" in result.stdout
        assert "unsupported_status_values: 1" in result.stdout
        assert "total_blocking: 2" in result.stdout
        assert "[FAIL]" in result.stdout
        assert "[PASS]" not in result.stdout
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# Read-only regression
# ---------------------------------------------------------------------------


def test_checker_makes_no_data_changes():
    db_name = "rnd201_checker_readonly"
    engine, env, ids = _setup(db_name)
    try:
        _drop_constraint(engine, "message_revocations_original_message_id_fkey")
        _drop_constraint(engine, "ck_message_revocations_status_valid")
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-missing-original", status="linked", original_message_id=999999)
        _insert_row(engine, revoke_event_message_id=ids["id1"], revoke_event_msgid="ev-bogus-status", status="bogus")
        _insert_row(engine, tenant_id=None, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-null-tenant-2", status="pending")
        _restore_fk_not_valid(engine, "message_revocations_original_message_id_fkey", "original_message_id")
        _restore_status_check_not_valid(engine)

        with engine.connect() as conn:
            before_revocations = conn.execute(
                text("SELECT id, tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id FROM message_revocations ORDER BY id")
            ).fetchall()
            before_archive = conn.execute(
                text("SELECT id, tenant_id, msgid, is_revoked, revoked_at FROM archive_messages ORDER BY id")
            ).fetchall()

        result = _run_checker(env)
        assert result.returncode != 0  # sanity: this fixture is genuinely corrupt

        with engine.connect() as conn:
            after_revocations = conn.execute(
                text("SELECT id, tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id FROM message_revocations ORDER BY id")
            ).fetchall()
            after_archive = conn.execute(
                text("SELECT id, tenant_id, msgid, is_revoked, revoked_at FROM archive_messages ORDER BY id")
            ).fetchall()

        assert before_revocations == after_revocations
        assert before_archive == after_archive
        assert len(after_revocations) == 3  # nothing inserted or deleted
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# Sensitive-output regression
# ---------------------------------------------------------------------------


def test_checker_output_never_contains_sensitive_content():
    db_name = "rnd201_checker_sensitive_output"
    engine, env, ids = _setup(db_name)
    try:
        sentinel_text = "SENTINEL-CONTENT-9f3a1c"
        sentinel_key = "SENTINEL-ENCRYPT-KEY-7b2e"
        sentinel_payload = "SENTINEL-STRUCTURED-PAYLOAD-4d1f"
        sentinel_path = "/sentinel/local/media/path/should-not-leak.jpg"

        with engine.connect() as conn:
            with conn.begin():
                conn.execute(
                    text(
                        "UPDATE archive_messages SET content_text = :t, encrypt_random_key = :k, "
                        "structured_content = :s WHERE id = :id0"
                    ),
                    {"t": sentinel_text, "k": sentinel_key, "s": f'{{"raw": "{sentinel_payload}"}}', **ids},
                )
                conn.execute(text("INSERT INTO media_files (sdkfileid, archive_message_id, local_path) VALUES ('sdk-sentinel', :id0, :path)"), {"path": sentinel_path, **ids})

        _drop_constraint(engine, "message_revocations_original_message_id_fkey")
        _insert_row(
            engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-sensitive",
            status="linked", original_message_id=999999,
        )
        _restore_fk_not_valid(engine, "message_revocations_original_message_id_fkey", "original_message_id")

        result = _run_checker(env)
        assert result.returncode != 0

        combined_output = result.stdout + result.stderr
        assert sentinel_text not in combined_output
        assert sentinel_key not in combined_output
        assert sentinel_payload not in combined_output
        assert sentinel_path not in combined_output
        assert env["DATABASE_URL"] not in combined_output
    finally:
        engine.dispose()
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# Schema-revision compatibility (0010 and 0011)
# ---------------------------------------------------------------------------


def test_clean_database_at_0011_also_exits_zero():
    """Confirms the checker gives an identical, correct report whether
    the database is at 0010 or already at 0011 -- a clean database
    passes at either revision."""
    db_name = "rnd201_checker_clean_0011"
    target_dsn = _fresh_database(db_name)
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(_BACKEND_DIR, env, "upgrade", "0011").returncode == 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            with conn.begin():
                ids = _seed_tenants_and_messages(conn, ["tenant-a"])
        _insert_row(engine, revoke_event_message_id=ids["id0"], revoke_event_msgid="ev-clean-0011", status="pending")
        engine.dispose()

        result = _run_checker(env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "[PASS]" in result.stdout
    finally:
        _drop_database(db_name)


def test_missing_env_var_exits_one_not_zero():
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    result = _run_checker(env)
    assert result.returncode == 1
    assert "[FAIL]" in result.stdout


def test_unreachable_database_exits_three_not_zero():
    """A connection/query failure must never be mistaken for a clean
    result -- distinct exit code from both "clean" (0) and "blocking
    violations found" (2)."""
    env = dict(os.environ, DATABASE_URL="postgresql://nonexistent-host-for-test-only:5432/nope")
    result = _run_checker(env)
    assert result.returncode == 3
    assert "[FAIL]" in result.stdout
    # The DATABASE_URL string itself (which for a real deployment could
    # embed a password) must never be echoed verbatim in the failure output.
    assert env["DATABASE_URL"] not in (result.stdout + result.stderr)
