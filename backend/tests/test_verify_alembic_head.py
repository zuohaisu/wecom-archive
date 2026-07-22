"""
Tests for RND-227 P0-B — deploy-time Alembic revision verification.

Covers both layers:
  - app.db.schema_check.revisions_match(): pure-function unit tests for
    the single-head, multi-head-branch, and no-revision comparison logic
    (no DB needed — this is what makes the branch/edge cases testable
    without mutating real migration history, which this ticket
    explicitly must not do).
  - scripts/verify_alembic_head.py: the real CLI entry point, invoked as
    a subprocess exactly as scripts/deploy_server.sh invokes it, against
    a live disposable Postgres database migrated to real revisions from
    this repo's own alembic/versions/ (same skip-gated convention as
    tests/test_revoke_association_migration.py and friends).

Run (from backend/):
    pytest tests/test_verify_alembic_head.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.db.schema_check import revisions_match

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_VERIFY_SCRIPT = "scripts/verify_alembic_head.py"


# ---------------------------------------------------------------------------
# Pure-function unit tests — no DB required
# ---------------------------------------------------------------------------


def test_single_head_match():
    assert revisions_match(frozenset({"0012"}), frozenset({"0012"})) is True


def test_single_head_mismatch():
    assert revisions_match(frozenset({"0005"}), frozenset({"0012"})) is False


def test_never_migrated_database_is_a_mismatch():
    assert revisions_match(frozenset(), frozenset({"0012"})) is False


def test_multi_head_branch_must_match_as_a_set():
    repo_heads = frozenset({"branch_a_head", "branch_b_head"})
    assert revisions_match(frozenset({"branch_a_head", "branch_b_head"}), repo_heads) is True
    # DB only caught up on one branch -> still a mismatch.
    assert revisions_match(frozenset({"branch_a_head"}), repo_heads) is False
    # DB has an extra/unexpected revision -> still a mismatch.
    assert revisions_match(frozenset({"branch_a_head", "branch_b_head", "stray"}), repo_heads) is False


def test_empty_repository_head_set_never_matches():
    # Degenerate case (no migrations exist at all) — must never report a
    # false-positive match just because both sides happen to be empty.
    assert revisions_match(frozenset(), frozenset()) is False


# ---------------------------------------------------------------------------
# Live-Postgres CLI tests (same convention as the migration test suite)
# ---------------------------------------------------------------------------


def _postgres_admin_dsn() -> str:
    return os.environ.get(
        "RND201_TEST_DATABASE_URL",
        f"postgresql://{os.environ.get('USER', 'postgres')}@/postgres?host=/tmp",
    )


def _with_database_name(dsn: str, db_name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(dsn)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{db_name}", parsed.query, ""))


def _postgres_reachable() -> bool:
    try:
        engine = create_engine(_postgres_admin_dsn())
        with engine.connect():
            pass
        return True
    except Exception:
        return False


def _force_drop_database(conn, db_name: str) -> None:
    conn.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :db AND pid <> pg_backend_pid()"
        ),
        {"db": db_name},
    )
    conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))


def _fresh_database(db_name: str) -> str:
    admin_dsn = _postgres_admin_dsn()
    target_dsn = _with_database_name(admin_dsn, db_name)
    admin_engine = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        _force_drop_database(conn, db_name)
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()
    return target_dsn


def _drop_database(db_name: str) -> None:
    admin_dsn = _postgres_admin_dsn()
    admin_engine = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        _force_drop_database(conn, db_name)
    admin_engine.dispose()


def _run_alembic(env: dict, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_BACKEND_DIR), env=env, capture_output=True, text=True, timeout=60,
    )


def _run_verify_script(env: dict):
    return subprocess.run(
        [sys.executable, _VERIFY_SCRIPT],
        cwd=str(_BACKEND_DIR), env=env, capture_output=True, text=True, timeout=60,
    )


# QA round 1 fix: this used to be a module-level `pytestmark`, which
# pytest applies to EVERY test in the file -- including the pure-function
# tests above, which need no database at all. That silently skipped them
# too whenever no local Postgres was reachable (e.g. CI's SQLite-only
# round), so they never actually ran there despite being written
# specifically to not need a DB. Applied per-test below instead, only on
# the tests that genuinely create scratch Postgres databases.
_requires_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable local Postgres for live migration test"
)


@_requires_postgres
def test_verify_script_passes_when_database_is_at_repository_head():
    db_name = "rnd227_verify_head"
    target_dsn = _fresh_database(db_name)
    try:
        env = dict(os.environ, DATABASE_URL=target_dsn)
        assert _run_alembic(env, "upgrade", "head").returncode == 0

        result = _run_verify_script(env)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
    finally:
        _drop_database(db_name)


@_requires_postgres
def test_verify_script_fails_when_database_is_behind_head():
    db_name = "rnd227_verify_behind"
    target_dsn = _fresh_database(db_name)
    try:
        env = dict(os.environ, DATABASE_URL=target_dsn)
        assert _run_alembic(env, "upgrade", "0005").returncode == 0

        result = _run_verify_script(env)
        assert result.returncode != 0
        assert "MISMATCH" in result.stdout
        assert "0005" in result.stdout
    finally:
        _drop_database(db_name)


@_requires_postgres
def test_verify_script_fails_when_database_was_never_migrated():
    db_name = "rnd227_verify_unmigrated"
    target_dsn = _fresh_database(db_name)
    try:
        env = dict(os.environ, DATABASE_URL=target_dsn)
        # No `alembic upgrade` call at all -- alembic_version doesn't exist.
        result = _run_verify_script(env)
        assert result.returncode != 0
        assert "no revision" in result.stdout
    finally:
        _drop_database(db_name)


def test_verify_script_fails_on_unreachable_database_without_leaking_dsn():
    env = dict(
        os.environ,
        DATABASE_URL="postgresql://mockuser:supersecretpassword@127.0.0.1:1/does_not_exist",
    )
    result = _run_verify_script(env)
    assert result.returncode != 0
    assert "supersecretpassword" not in result.stdout
    assert "supersecretpassword" not in result.stderr


def test_verify_script_fails_when_database_url_unset():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    result = _run_verify_script(env)
    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr
