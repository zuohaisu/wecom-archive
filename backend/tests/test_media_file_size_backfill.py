"""RND-331 tests: downloaded-size recovery, conditional CHECK, and rollups."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import MediaFile, Tenant, TenantStorageDaily
from app.services.tenant_storage_rollup import refresh_tenant_storage_daily
from scripts import backfill_media_file_size_once as backfill

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0023_media_file_size_storage_rollup.py"
)


def _legacy_media_engine(tmp_path):
    """Pre-RND-331 media table: permits historical downloaded+NULL rows."""
    engine = create_engine(f"sqlite:///{tmp_path / 'media-size.db'}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE media_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sdkfileid TEXT NOT NULL,
                archive_message_id INTEGER NOT NULL, tenant_id TEXT, file_type TEXT,
                local_path TEXT, oss_key TEXT, storage_backend TEXT, storage_ref TEXT,
                file_size BIGINT, download_status VARCHAR(16) NOT NULL,
                download_attempts INTEGER NOT NULL DEFAULT 0, migration_status VARCHAR(16),
                migration_attempted_at DATETIME, migration_error TEXT, bucket VARCHAR(128),
                mime_type VARCHAR(128), checksum_sha256 VARCHAR(64), thumbnail_ref TEXT,
                image_width INTEGER, image_height INTEGER, thumbnail_status VARCHAR(16),
                thumbnail_attempted_at DATETIME, thumbnail_error TEXT, playback_ref TEXT,
                playback_status VARCHAR(32), created_at DATETIME, updated_at DATETIME
            )
        """))
    return engine


def test_backfill_dry_run_then_apply_is_idempotent(tmp_path, monkeypatch) -> None:
    engine = _legacy_media_engine(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    stored = media_root / "one.jpg"
    stored.write_bytes(b"1234567")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO media_files (sdkfileid, archive_message_id, tenant_id, storage_backend,
            storage_ref, file_size, download_status) VALUES
            ('downloaded-missing-size', 1, 'tenant-a', 'local', 'one.jpg', NULL, 'downloaded'),
            ('pending-size-unknown', 2, 'tenant-a', 'local', 'one.jpg', NULL, 'pending')
        """))

    dry_run = backfill.run_backfill(engine, batch_size=1, dry_run=True)
    assert (dry_run.selected, dry_run.would_backfill, dry_run.failed_ids) == (1, 1, [])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT file_size FROM media_files WHERE id = 1")).scalar_one() is None

    applied = backfill.run_backfill(engine, batch_size=1, dry_run=False)
    assert (applied.selected, applied.backfilled, applied.failed_ids) == (1, 1, [])
    assert backfill.run_backfill(engine, batch_size=1, dry_run=False).selected == 0
    with engine.connect() as conn:
        assert conn.execute(text("SELECT file_size FROM media_files WHERE id = 1")).scalar_one() == 7
        assert conn.execute(text("SELECT file_size FROM media_files WHERE id = 2")).scalar_one() is None


def test_backfill_reports_missing_object_without_writing_zero(tmp_path, monkeypatch) -> None:
    engine = _legacy_media_engine(tmp_path)
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO media_files (sdkfileid, archive_message_id, storage_backend, storage_ref,
            file_size, download_status) VALUES ('gone', 1, 'local', 'gone.jpg', NULL, 'downloaded')
        """))

    result = backfill.run_backfill(engine, batch_size=10, dry_run=False)
    assert result.failed_ids == [1]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT file_size FROM media_files WHERE id = 1")).scalar_one() is None


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd331_migration", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _migration_ops(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY)"))
        conn.execute(text("""
            CREATE TABLE media_files (id INTEGER PRIMARY KEY, download_status TEXT NOT NULL,
            file_size BIGINT)
        """))
    migration = _load_migration()
    connection = engine.connect()
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    return migration, connection


def test_migration_blocks_remaining_downloaded_null_rows(tmp_path, monkeypatch) -> None:
    migration, conn = _migration_ops(tmp_path, monkeypatch)
    conn.execute(text("INSERT INTO media_files VALUES (1, 'downloaded', NULL)"))
    conn.commit()
    with pytest.raises(RuntimeError, match="backfill_media_file_size_once"):
        migration.upgrade()
    connection = conn
    connection.close()


def test_migration_check_rejects_only_downloaded_null_rows(tmp_path, monkeypatch) -> None:
    migration, conn = _migration_ops(tmp_path, monkeypatch)
    migration.upgrade()
    conn.execute(text("INSERT INTO media_files VALUES (1, 'pending', NULL)"))
    conn.execute(text("INSERT INTO media_files VALUES (2, 'failed', NULL)"))
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(text("INSERT INTO media_files VALUES (3, 'downloaded', NULL)"))
    conn.execute(text("INSERT INTO media_files VALUES (4, 'downloaded', 9)"))
    conn.commit()
    migration.downgrade()
    conn.close()


def test_daily_rollup_counts_downloaded_rows_only() -> None:
    engine = create_engine("sqlite:///:memory:")
    Tenant.__table__.create(engine)
    MediaFile.__table__.create(engine)
    TenantStorageDaily.__table__.create(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant-a", name="A", slug="a"))
        db.add_all([
            MediaFile(sdkfileid="done", archive_message_id=1, tenant_id="tenant-a", file_size=12, download_status="downloaded"),
            MediaFile(sdkfileid="pending", archive_message_id=2, tenant_id="tenant-a", file_size=None, download_status="pending"),
            MediaFile(sdkfileid="failed", archive_message_id=3, tenant_id="tenant-a", file_size=None, download_status="failed"),
        ])
        db.commit()
        assert refresh_tenant_storage_daily(db, usage_date=date(2026, 7, 29)) == 1
        db.commit()
        assert db.query(TenantStorageDaily).one().used_bytes == 12

        db.add(MediaFile(sdkfileid="done-two", archive_message_id=4, tenant_id="tenant-a", file_size=8, download_status="downloaded"))
        assert refresh_tenant_storage_daily(db, usage_date=date(2026, 7, 29)) == 1
        db.commit()
        assert db.query(TenantStorageDaily).one().used_bytes == 20
