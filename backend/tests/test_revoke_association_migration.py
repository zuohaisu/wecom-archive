"""
Tests for RND-201 — Alembic migration 0009 (revoke association).

Same approach as tests/test_media_migration_metadata_migration.py (0007):
loads migration 0009's upgrade()/downgrade() directly (by file path) and
runs them against a hand-built sqlite archive_messages table shaped like
the post-0008 schema (0008 is the current head at the time this migration
was authored). Both changes in 0009 -- new columns on archive_messages,
and the new message_revocations table -- are pure ADD COLUMN / CREATE
TABLE, sqlite-compatible, and perform no data backfill.

Run (from backend/):
    pytest tests/test_revoke_association_migration.py -v
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
    / "0009_revoke_association.py"
)

_POST_0008_SCHEMA_SQL = """
CREATE TABLE tenants (
    id TEXT PRIMARY KEY, name TEXT, slug TEXT, is_active INTEGER,
    deletion_locked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE archive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid TEXT NOT NULL,
    seq INTEGER NOT NULL,
    publickey_ver INTEGER NOT NULL,
    raw_encrypted_payload TEXT,
    encrypt_random_key TEXT NOT NULL,
    encrypt_chat_msg TEXT NOT NULL,
    decrypt_status TEXT NOT NULL DEFAULT 'pending',
    decrypted_payload TEXT,
    structured_content TEXT,
    content_text TEXT,
    msgtype TEXT,
    sender TEXT,
    roomid TEXT,
    msgtime INTEGER,
    tolist TEXT,
    sdkfileid TEXT,
    deleted_at DATETIME, deleted_by_admin_user_id TEXT, delete_reason TEXT, purge_after DATETIME, restored_at DATETIME, restored_by_admin_user_id TEXT, deletion_batch_id TEXT,
    tenant_id TEXT,
    created_at TEXT
);
"""


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "rnd201_migration_0009", str(_MIGRATION_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migration():
    return _load_migration_module()


@pytest.fixture()
def pre_migration_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre_0009.db'}")
    with engine.begin() as conn:
        for stmt in _POST_0008_SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    return engine


@pytest.fixture()
def bound_op(pre_migration_engine, migration, monkeypatch):
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


def _tables(bound_op) -> set:
    rows = _conn(bound_op).execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table'")
    ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_adds_is_revoked_and_revoked_at_columns(migration, bound_op) -> None:
    migration.upgrade()
    columns = _columns(bound_op, "archive_messages")
    assert "is_revoked" in columns
    assert "revoked_at" in columns


def test_upgrade_creates_message_revocations_table(migration, bound_op) -> None:
    migration.upgrade()
    assert "message_revocations" in _tables(bound_op)
    columns = _columns(bound_op, "message_revocations")
    for expected in (
        "id",
        "tenant_id",
        "revoke_event_message_id",
        "revoke_event_msgid",
        "revoke_event_msgtime",
        "target_msgid",
        "original_message_id",
        "status",
        "created_at",
        "updated_at",
    ):
        assert expected in columns


def test_upgrade_does_not_fabricate_revoked_state_for_existing_messages(migration, bound_op) -> None:
    """Existing rows have no supporting revoke event -- is_revoked must
    default to false (never true), revoked_at must default to NULL
    (never a fabricated timestamp)."""
    conn = _conn(bound_op)
    conn.execute(
        text(
            "INSERT INTO archive_messages "
            "(msgid, seq, publickey_ver, encrypt_random_key, encrypt_chat_msg, "
            " decrypt_status, content_text, msgtype, tenant_id) "
            "VALUES ('m-1', 1, 1, 'k', 'c', 'success', 'hello', 'text', 'tenant-a')"
        )
    )
    conn.commit()

    migration.upgrade()

    row = conn.execute(
        text("SELECT is_revoked, revoked_at FROM archive_messages WHERE msgid = 'm-1'")
    ).fetchone()
    assert row.is_revoked in (0, False)
    assert row.revoked_at is None


def test_upgrade_preserves_all_existing_archive_message_rows(migration, bound_op) -> None:
    conn = _conn(bound_op)
    for i in range(5):
        conn.execute(
            text(
                "INSERT INTO archive_messages "
                "(msgid, seq, publickey_ver, encrypt_random_key, encrypt_chat_msg, "
                " decrypt_status, content_text, msgtype, tenant_id) "
                "VALUES (:msgid, :seq, 1, 'k', 'c', 'success', :text, 'text', 'tenant-a')"
            ),
            {"msgid": f"m-{i}", "seq": i, "text": f"hello {i}"},
        )
    conn.commit()

    migration.upgrade()

    count = conn.execute(text("SELECT COUNT(*) FROM archive_messages")).scalar()
    assert count == 5
    texts = {
        row[0]
        for row in conn.execute(text("SELECT content_text FROM archive_messages")).fetchall()
    }
    assert texts == {"hello 0", "hello 1", "hello 2", "hello 3", "hello 4"}


def test_upgrade_is_additive_only_no_column_dropped_or_altered(migration, bound_op) -> None:
    before = _columns(bound_op, "archive_messages")
    migration.upgrade()
    after = _columns(bound_op, "archive_messages")
    assert before.issubset(after)


def test_downgrade_removes_message_revocations_table_and_new_columns(migration, bound_op) -> None:
    migration.upgrade()
    migration.downgrade()
    assert "message_revocations" not in _tables(bound_op)
    columns = _columns(bound_op, "archive_messages")
    assert "is_revoked" not in columns
    assert "revoked_at" not in columns
    # Untouched, pre-existing columns survive the round trip.
    assert "msgid" in columns
    assert "structured_content" in columns


def test_downgrade_preserves_existing_archive_message_rows(migration, bound_op) -> None:
    conn = _conn(bound_op)
    conn.execute(
        text(
            "INSERT INTO archive_messages "
            "(msgid, seq, publickey_ver, encrypt_random_key, encrypt_chat_msg, "
            " decrypt_status, content_text, msgtype, tenant_id) "
            "VALUES ('m-1', 1, 1, 'k', 'c', 'success', 'hello', 'text', 'tenant-a')"
        )
    )
    conn.commit()

    migration.upgrade()
    migration.downgrade()

    row = conn.execute(
        text("SELECT content_text FROM archive_messages WHERE msgid = 'm-1'")
    ).fetchone()
    assert row.content_text == "hello"


# ---------------------------------------------------------------------------
# Optional end-to-end check against a live Postgres, when reachable
# (same pattern as test_media_storage_backend_migration.py) — this is the
# only way to genuinely exercise "upgrade from the current production
# head" and "upgrade from an empty database", since the full 0001..0009
# chain cannot run against sqlite at all (0001 creates Postgres-only
# JSONB columns).
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


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable local Postgres for live migration test")
def test_full_migration_chain_from_empty_database_runs_against_real_postgres() -> None:
    """End-to-end: run the full migration chain through the real alembic
    CLI against a brand-new, empty scratch Postgres database (exercises
    "empty-database upgrade" and "upgrade to the current head" in the
    same run), then verify the resulting schema has the new columns/
    table/constraints/indexes 0009 introduced. Runs against whatever
    "head" currently is (0011 as of RND-201 round 3 / B4), so the
    uniqueness assertion below reflects 0011's tightened constraint, not
    0009's original one -- see test_message_revocations_tenant_integrity_
    migration.py for 0011's own focused constraint tests."""
    from sqlalchemy import create_engine as _create_engine

    admin_dsn = _postgres_admin_dsn()
    db_name = "rnd201_migration_pytest"
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
            archive_message_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'archive_messages'"
                    )
                ).fetchall()
            }
            revocation_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'message_revocations'"
                    )
                ).fetchall()
            }
            constraints = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_name = 'message_revocations'"
                    )
                ).fetchall()
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE tablename = 'message_revocations'"
                    )
                ).fetchall()
            }
        engine.dispose()

        assert "is_revoked" in archive_message_columns
        assert "revoked_at" in archive_message_columns
        for expected in (
            "id",
            "tenant_id",
            "revoke_event_message_id",
            "revoke_event_msgid",
            "revoke_event_msgtime",
            "target_msgid",
            "original_message_id",
            "status",
            "created_at",
            "updated_at",
        ):
            assert expected in revocation_columns
        # RND-201 round 3 / B4: migration 0011 replaced the composite
        # (tenant_id, revoke_event_message_id) unique constraint 0009
        # shipped with a plain UNIQUE(revoke_event_message_id) -- see
        # test_message_revocations_tenant_integrity_migration.py.
        assert "uq_message_revocations_revoke_event_message_id" in constraints
        assert "uq_message_revocations_tenant_revoke_event" not in constraints
        assert "ix_message_revocations_tenant_target_msgid" in indexes
        assert "ix_message_revocations_original_message_id" in indexes
    finally:
        admin_engine = _create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin_engine.dispose()


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable local Postgres for live migration test")
def test_downgrade_from_head_then_upgrade_again_round_trips_cleanly_on_real_postgres() -> None:
    """Downgrade 0009 -> 0008 then upgrade back to head against a real
    Postgres database — verifies the downgrade path actually works (not
    just the sqlite unit test above) and that re-upgrading is safe."""
    from sqlalchemy import create_engine as _create_engine

    admin_dsn = _postgres_admin_dsn()
    db_name = "rnd201_migration_roundtrip_pytest"
    backend_dir = Path(__file__).resolve().parent.parent
    target_dsn = _with_database_name(admin_dsn, db_name)

    admin_engine = _create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    try:
        env = dict(os.environ, DATABASE_URL=target_dsn)
        up = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
        )
        assert up.returncode == 0, up.stderr

        down = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0008"],
            cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
        )
        assert down.returncode == 0, down.stderr

        engine = _create_engine(target_dsn)
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
                ).fetchall()
            }
        engine.dispose()
        assert "message_revocations" not in tables

        up_again = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
        )
        assert up_again.returncode == 0, up_again.stderr

        engine = _create_engine(target_dsn)
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
                ).fetchall()
            }
        engine.dispose()
        assert "message_revocations" in tables
    finally:
        admin_engine = _create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin_engine.dispose()
