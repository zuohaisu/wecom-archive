from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0044_rnd385_quota_blocked_media.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd385_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_upgrade_downgrade_and_reupgrade(monkeypatch) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE media_files (id INTEGER PRIMARY KEY)"))
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("media_quota_blocks")
        }
        assert columns == {
            "media_file_id",
            "tenant_id",
            "observed_bytes",
            "reason",
            "blocked_at",
            "updated_at",
        }
        migration.downgrade()
        assert "media_quota_blocks" not in inspect(connection).get_table_names()
        migration.upgrade()
        assert "media_quota_blocks" in inspect(connection).get_table_names()


def test_migration_extends_payment_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0044"
    assert migration.down_revision == "0043"
