"""RND-371 avatar metadata migration upgrade/downgrade contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0064_rnd371_contact_avatars.py"
)
_AVATAR_COLUMNS = {
    "avatar_storage_backend",
    "avatar_storage_ref",
    "avatar_content_type",
    "avatar_source",
    "avatar_synced_at",
    "avatar_status",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("contact_avatar_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_contact_avatar_migration_round_trips_on_sqlite(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'avatars.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE contacts (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE external_contacts (id INTEGER PRIMARY KEY)"))

    connection = engine.connect()
    migration = _load_migration()
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))

    migration.upgrade()
    inspector = inspect(connection)
    for table_name in ("contacts", "external_contacts"):
        assert _AVATAR_COLUMNS <= {
            column["name"] for column in inspector.get_columns(table_name)
        }

    migration.downgrade()
    inspector = inspect(connection)
    for table_name in ("contacts", "external_contacts"):
        assert not _AVATAR_COLUMNS & {
            column["name"] for column in inspector.get_columns(table_name)
        }
    connection.close()
    engine.dispose()
