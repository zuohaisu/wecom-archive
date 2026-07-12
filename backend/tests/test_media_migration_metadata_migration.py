"""
Tests for RND-186 QA fix — Alembic migration 0007 (adds media_files.bucket
/ mime_type / checksum_sha256).

Same approach as tests/test_media_migration_bookkeeping_migration.py (0006):
loads migration 0007's upgrade()/downgrade() directly (by file path) and
runs them against a hand-built sqlite media_files table shaped like the
post-0006 schema — 0007 itself only uses ADD COLUMN, sqlite-compatible, and
performs no data backfill.

Run (from backend/):
    pytest tests/test_media_migration_metadata_migration.py -v
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
    / "0007_media_migration_metadata.py"
)

_POST_0006_SCHEMA_SQL = """
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
    migration_status TEXT,
    migration_attempted_at TEXT,
    migration_error TEXT,
    tenant_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "rnd186_migration_0007", str(_MIGRATION_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migration():
    return _load_migration_module()


@pytest.fixture()
def pre_migration_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre_0007.db'}")
    with engine.begin() as conn:
        for stmt in _POST_0006_SCHEMA_SQL.strip().split(";"):
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


def test_upgrade_adds_bucket_mime_type_checksum_columns(migration, bound_op) -> None:
    migration.upgrade()
    columns = _columns(bound_op, "media_files")
    assert "bucket" in columns
    assert "mime_type" in columns
    assert "checksum_sha256" in columns


def test_upgrade_leaves_existing_rows_with_null_metadata(migration, bound_op) -> None:
    """No backfill: a row already migrated under the pre-0007 tool version
    (storage_backend="qiniu_kodo", migration_status="migrated") gets
    bucket/mime_type/checksum_sha256 = NULL — "not recorded by this tool
    version" — not a fabricated value."""
    conn = _conn(bound_op)
    conn.execute(
        text(
            "INSERT INTO media_files "
            "(sdkfileid, archive_message_id, storage_backend, storage_ref, "
            " download_status, migration_status) "
            "VALUES ('sdk-1', 1, 'qiniu_kodo', 'tenants/t1/images/1.jpg', 'downloaded', 'migrated')"
        )
    )
    conn.commit()

    migration.upgrade()

    row = conn.execute(
        text(
            "SELECT bucket, mime_type, checksum_sha256 FROM media_files WHERE sdkfileid = 'sdk-1'"
        )
    ).fetchone()
    assert row.bucket is None
    assert row.mime_type is None
    assert row.checksum_sha256 is None


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


def test_downgrade_removes_metadata_columns_leaves_everything_else_intact(
    migration, bound_op
) -> None:
    migration.upgrade()
    migration.downgrade()
    columns = _columns(bound_op, "media_files")
    assert "bucket" not in columns
    assert "mime_type" not in columns
    assert "checksum_sha256" not in columns
    assert "storage_backend" in columns  # untouched, from migration 0005
    assert "migration_status" in columns  # untouched, from migration 0006
