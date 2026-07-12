"""
Tests for RND-186 — Alembic migration 0006 (adds media_files.migration_status
/ migration_attempted_at / migration_error).

Same approach as tests/test_media_storage_backend_migration.py (0005): the
full chain can't run against sqlite (0001 uses Postgres-only JSONB), so this
loads migration 0006's upgrade()/downgrade() directly (by file path) and
runs them against a hand-built sqlite media_files table shaped like the
post-0005 schema — 0006 itself only uses ADD COLUMN / CREATE INDEX, both
sqlite-compatible, and performs no data backfill.

Run (from backend/):
    pytest tests/test_media_migration_bookkeeping_migration.py -v
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0006_media_migration_bookkeeping.py"
)

_POST_0005_SCHEMA_SQL = """
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sdkfileid TEXT NOT NULL,
    archive_message_id INTEGER NOT NULL,
    file_type TEXT,
    local_path TEXT,
    oss_key TEXT,
    storage_backend TEXT,
    storage_ref TEXT,
    file_size INTEGER,
    download_status TEXT NOT NULL DEFAULT 'pending',
    tenant_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "rnd186_migration_0006", str(_MIGRATION_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migration():
    return _load_migration_module()


@pytest.fixture()
def pre_migration_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre_0006.db'}")
    with engine.begin() as conn:
        for stmt in _POST_0005_SCHEMA_SQL.strip().split(";"):
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


def test_upgrade_adds_migration_bookkeeping_columns(migration, bound_op) -> None:
    migration.upgrade()
    columns = _columns(bound_op, "media_files")
    assert "migration_status" in columns
    assert "migration_attempted_at" in columns
    assert "migration_error" in columns


def test_upgrade_leaves_existing_rows_with_null_migration_status(migration, bound_op) -> None:
    """No backfill: every pre-existing row (including one already
    storage_backend="qiniu_kodo" from RND-174) gets migration_status=NULL
    — "never attempted by this tool" — not any value implying it was
    already considered migrated by scripts/migrate_local_media_to_qiniu.py."""
    conn = _conn(bound_op)
    conn.execute(
        text(
            "INSERT INTO media_files "
            "(sdkfileid, archive_message_id, local_path, storage_backend, storage_ref, download_status) "
            "VALUES ('sdk-1', 1, '/data/1.jpg', 'local', '/data/1.jpg', 'downloaded')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO media_files "
            "(sdkfileid, archive_message_id, storage_backend, storage_ref, download_status) "
            "VALUES ('sdk-2', 2, 'qiniu_kodo', 'tenants/t1/images/2.jpg', 'downloaded')"
        )
    )
    conn.commit()

    migration.upgrade()

    rows = conn.execute(
        text("SELECT sdkfileid, migration_status FROM media_files ORDER BY sdkfileid")
    ).fetchall()
    assert [r.migration_status for r in rows] == [None, None]


def test_upgrade_preserves_all_existing_rows(migration, bound_op) -> None:
    conn = _conn(bound_op)
    for i in range(5):
        conn.execute(
            text(
                "INSERT INTO media_files "
                "(sdkfileid, archive_message_id, local_path, storage_backend, storage_ref, download_status) "
                "VALUES (:sdk, :aid, :path, 'local', :path, 'downloaded')"
            ),
            {"sdk": f"sdk-{i}", "aid": i, "path": f"/data/{i}.jpg"},
        )
    conn.commit()

    migration.upgrade()

    count = conn.execute(text("SELECT COUNT(*) FROM media_files")).scalar()
    assert count == 5


def test_downgrade_removes_migration_columns_leaves_storage_columns_intact(
    migration, bound_op
) -> None:
    migration.upgrade()
    migration.downgrade()
    columns = _columns(bound_op, "media_files")
    assert "migration_status" not in columns
    assert "migration_attempted_at" not in columns
    assert "migration_error" not in columns
    assert "storage_backend" in columns  # untouched, from migration 0005
    assert "storage_ref" in columns
