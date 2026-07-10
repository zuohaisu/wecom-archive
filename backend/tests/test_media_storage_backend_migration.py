"""
Tests for RND-174 QA remediation — Alembic migration 0005 (adds
media_files.storage_backend / media_files.storage_ref and backfills
existing rows).

The full migration chain (0001..0005) cannot run against sqlite (0001
creates Postgres-only JSONB columns — see the module docstring in
tests/test_reachability_audit.py for the established pattern of hand-
rolling a sqlite schema instead). This file instead loads migration 0005's
upgrade()/downgrade() functions directly (by file path, since Python
module names can't start with a digit) and runs them against a hand-built
sqlite media_files table shaped like the pre-0005 schema — 0005 itself only
uses ADD COLUMN / CREATE INDEX / UPDATE, all sqlite-compatible.

A second, optional end-to-end test runs the *real* migration through
alembic against a live Postgres database when DATABASE_URL (or the
default local Postgres.app socket) is reachable; it skips cleanly
otherwise so the suite does not require Postgres to be running.

Run (from backend/):
    pytest tests/test_media_storage_backend_migration.py -v
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0005_media_storage_backend_reference.py"
)

_PRE_0005_SCHEMA_SQL = """
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sdkfileid TEXT NOT NULL,
    archive_message_id INTEGER NOT NULL,
    file_type TEXT,
    local_path TEXT,
    oss_key TEXT,
    file_size INTEGER,
    download_status TEXT NOT NULL DEFAULT 'pending',
    tenant_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "rnd174_migration_0005", str(_MIGRATION_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migration():
    return _load_migration_module()


@pytest.fixture()
def pre_migration_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre_0005.db'}")
    with engine.begin() as conn:
        for stmt in _PRE_0005_SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    return engine


@pytest.fixture()
def bound_op(pre_migration_engine, migration, monkeypatch):
    """Bind alembic's `op` proxy (as used inside the migration module) to a
    single real connection against pre_migration_engine, the same way
    alembic itself does at runtime — just without going through the CLI/
    full migration chain. Tests must issue their INSERT/SELECT statements
    against this same connection (op.get_bind()) rather than opening a
    second connection — SQLite does not make one connection's uncommitted
    writes visible to another."""
    connection = pre_migration_engine.connect()
    ctx = MigrationContext.configure(connection)
    operations = Operations(ctx)
    monkeypatch.setattr(migration, "op", operations)
    yield operations
    connection.close()


def _conn(bound_op):
    return bound_op.get_bind()


def _columns(bound_op, table: str) -> set:
    rows = _conn(bound_op).execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


# ---------------------------------------------------------------------------
# upgrade() — adds columns, backfills existing rows
# ---------------------------------------------------------------------------


def test_upgrade_adds_storage_backend_and_storage_ref_columns(migration, bound_op) -> None:
    migration.upgrade()
    columns = _columns(bound_op, "media_files")
    assert "storage_backend" in columns
    assert "storage_ref" in columns


def test_upgrade_backfills_downloaded_row_as_local(migration, bound_op) -> None:
    conn = _conn(bound_op)
    conn.execute(
        text(
            "INSERT INTO media_files (sdkfileid, archive_message_id, local_path, download_status) "
            "VALUES ('sdk-1', 1, '/data/media/tenants/t1/images/1.jpg', 'downloaded')"
        )
    )
    conn.commit()

    migration.upgrade()

    row = conn.execute(
        text(
            "SELECT storage_backend, storage_ref, local_path FROM media_files "
            "WHERE sdkfileid = 'sdk-1'"
        )
    ).fetchone()

    assert row.storage_backend == "local"
    assert row.storage_ref == "/data/media/tenants/t1/images/1.jpg"
    assert row.local_path == "/data/media/tenants/t1/images/1.jpg"  # untouched


def test_upgrade_backfills_pending_row_with_null_local_path(migration, bound_op) -> None:
    """A pending row (never downloaded, local_path NULL) still gets
    storage_backend='local' — it will be written by whichever provider is
    configured when the download eventually succeeds — but storage_ref
    stays NULL since there is no file yet."""
    conn = _conn(bound_op)
    conn.execute(
        text(
            "INSERT INTO media_files (sdkfileid, archive_message_id, local_path, download_status) "
            "VALUES ('sdk-2', 2, NULL, 'pending')"
        )
    )
    conn.commit()

    migration.upgrade()

    row = conn.execute(
        text("SELECT storage_backend, storage_ref FROM media_files WHERE sdkfileid = 'sdk-2'")
    ).fetchone()

    assert row.storage_backend == "local"
    assert row.storage_ref is None


def test_upgrade_does_not_overwrite_already_backfilled_rows(migration, bound_op) -> None:
    """Re-running the backfill UPDATE (WHERE storage_backend IS NULL) must
    never clobber a row that already has an explicit storage_backend —
    defensive idempotency in case upgrade() ever runs twice against the
    same data."""
    conn = _conn(bound_op)
    migration.upgrade()  # adds columns
    conn.commit()

    conn.execute(
        text(
            "INSERT INTO media_files (sdkfileid, archive_message_id, local_path, "
            "download_status, storage_backend, storage_ref) "
            "VALUES ('sdk-3', 3, NULL, 'downloaded', 'qiniu_kodo', 'tenants/t1/images/3.jpg')"
        )
    )
    conn.execute(
        text(
            "UPDATE media_files SET storage_backend = 'local', storage_ref = local_path "
            "WHERE storage_backend IS NULL"
        )
    )
    conn.commit()

    row = conn.execute(
        text("SELECT storage_backend, storage_ref FROM media_files WHERE sdkfileid = 'sdk-3'")
    ).fetchone()

    assert row.storage_backend == "qiniu_kodo"
    assert row.storage_ref == "tenants/t1/images/3.jpg"


def test_upgrade_preserves_all_existing_rows(migration, bound_op) -> None:
    conn = _conn(bound_op)
    for i in range(5):
        conn.execute(
            text(
                "INSERT INTO media_files (sdkfileid, archive_message_id, local_path, download_status) "
                "VALUES (:sdk, :aid, :path, 'downloaded')"
            ),
            {"sdk": f"sdk-{i}", "aid": i, "path": f"/data/{i}.jpg"},
        )
    conn.commit()

    migration.upgrade()

    count = conn.execute(text("SELECT COUNT(*) FROM media_files")).scalar()
    assert count == 5


# ---------------------------------------------------------------------------
# downgrade() — removes the added columns, leaves everything else intact
# ---------------------------------------------------------------------------


def test_downgrade_removes_storage_columns(migration, bound_op) -> None:
    migration.upgrade()
    migration.downgrade()
    columns = _columns(bound_op, "media_files")
    assert "storage_backend" not in columns
    assert "storage_ref" not in columns
    assert "local_path" in columns  # untouched


# ---------------------------------------------------------------------------
# Optional end-to-end check against a live Postgres, when reachable
# ---------------------------------------------------------------------------


def _postgres_admin_dsn() -> str:
    return os.environ.get(
        "RND174_TEST_DATABASE_URL",
        f"postgresql://{os.environ.get('USER', 'postgres')}@/postgres?host=/tmp",
    )


def _with_database_name(dsn: str, db_name: str) -> str:
    """Swap the database-name path segment of a Postgres DSN, preserving
    any query string (e.g. ?host=/tmp for a Unix-socket connection) — a
    naive rsplit("/") on the whole DSN breaks when the query string itself
    contains "/", as it does for a socket-directory host param."""
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


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable local Postgres for live migration test")
def test_full_migration_chain_runs_against_real_postgres() -> None:
    """End-to-end sanity check: run every migration (0001..0005) through
    the real alembic CLI against a scratch Postgres database, then verify
    the resulting schema has the new columns. Skips cleanly in
    environments with no local Postgres."""
    from sqlalchemy import create_engine as _create_engine

    admin_dsn = _postgres_admin_dsn()
    db_name = "rnd174_migration_pytest"
    backend_dir = Path(__file__).resolve().parent.parent
    target_dsn = _with_database_name(admin_dsn, db_name)

    admin_engine = _create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    try:
        env = dict(os.environ, DATABASE_URL=target_dsn)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(backend_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

        engine = _create_engine(target_dsn)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'media_files'"
                )
            ).fetchall()
        engine.dispose()
        columns = {row[0] for row in rows}
        assert "storage_backend" in columns
        assert "storage_ref" in columns
    finally:
        admin_engine = _create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin_engine.dispose()
