"""SQLite-compatible checks for additive RND-258 migration 0014."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic/versions/0014_voice_playback_variants.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd258_migration_0014", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migration():
    return _load_migration()


@pytest.fixture()
def bound_op(tmp_path, migration, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre_0014.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE media_files ("
                "id INTEGER PRIMARY KEY, sdkfileid TEXT, archive_message_id INTEGER, "
                "download_status TEXT, file_type TEXT)"
            )
        )
    connection = engine.connect()
    operations = Operations(MigrationContext.configure(connection))
    monkeypatch.setattr(migration, "op", operations)
    yield operations
    connection.close()


def test_upgrade_adds_playback_columns_and_default(migration, bound_op) -> None:
    migration.upgrade()
    conn = bound_op.get_bind()
    columns = {row[1] for row in conn.execute(text("PRAGMA table_info(media_files)"))}
    assert {"playback_ref", "playback_status"}.issubset(columns)
    conn.execute(
        text(
            "INSERT INTO media_files (id, sdkfileid, archive_message_id, download_status) "
            "VALUES (1, 'sdk', 1, 'downloaded')"
        )
    )
    assert conn.execute(text("SELECT playback_status FROM media_files WHERE id = 1")).scalar() == "not_applicable"


def test_downgrade_removes_only_playback_columns(migration, bound_op) -> None:
    migration.upgrade()
    migration.downgrade()
    columns = {row[1] for row in bound_op.get_bind().execute(text("PRAGMA table_info(media_files)"))}
    assert "playback_ref" not in columns
    assert "playback_status" not in columns
    assert "sdkfileid" in columns
