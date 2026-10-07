"""SQLite upgrade/downgrade coverage for the RND-366 favorites migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0074_rnd366_favorites.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rnd366_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_pre_favorites_schema(engine) -> None:
    metadata = sa.MetaData()
    sa.Table("tenants", metadata, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table("admin_users", metadata, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table(
        "archive_messages",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_archive_messages_tenant_id_id"),
    )
    sa.Table(
        "media_files",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=True),
        sa.Column("sdkfileid", sa.Text(), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES ('tenant-a'), ('tenant-b')")
        )
        connection.execute(text("INSERT INTO admin_users (id) VALUES ('owner-a')"))
        connection.execute(
            text("INSERT INTO archive_messages (id, tenant_id) VALUES (1, 'tenant-a')")
        )
        connection.execute(
            text(
                "INSERT INTO media_files (id, tenant_id, sdkfileid) "
                "VALUES (1, 'tenant-a', 'sdk-a1')"
            )
        )


def test_favorites_migration_upgrade_downgrade_reupgrade_and_data_preservation(
    monkeypatch,
) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    _create_pre_favorites_schema(engine)

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        inspector = inspect(connection)
        assert "archive_favorites" in inspector.get_table_names()
        assert {
            "ix_archive_favorites_tenant_active_time",
            "ix_archive_favorites_tenant_active_actor",
        } <= {index["name"] for index in inspector.get_indexes("archive_favorites")}
        plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM archive_favorites "
                "WHERE tenant_id = 'tenant-a' AND canceled_at IS NULL "
                "ORDER BY favorited_at DESC, id ASC LIMIT 50"
            )
        ).all()
        assert any(
            "ix_archive_favorites_tenant_active_time" in str(row[-1])
            for row in plan
        )
        assert "uq_media_files_tenant_id_id" in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("media_files")
        }
        assert {
            "uq_archive_favorites_tenant_message",
            "uq_archive_favorites_tenant_media",
        } <= {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("archive_favorites")
        }
        foreign_keys = inspector.get_foreign_keys("archive_favorites")
        assert any(
            fk["name"] == "fk_archive_favorites_tenant_message"
            and fk["options"].get("ondelete") == "CASCADE"
            for fk in foreign_keys
        )
        assert any(
            fk["name"] == "fk_archive_favorites_tenant_media"
            and fk["options"].get("ondelete") == "CASCADE"
            for fk in foreign_keys
        )
        connection.execute(
            text(
                "INSERT INTO archive_favorites "
                "(id, tenant_id, object_type, archive_message_id, favorited_by_admin_user_id) "
                "VALUES ('favorite-a', 'tenant-a', 'message', 1, 'owner-a')"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO archive_favorites "
                    "(id, tenant_id, object_type, archive_message_id) "
                    "VALUES ('favorite-duplicate', 'tenant-a', 'message', 1)"
                )
            )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO archive_favorites "
                    "(id, tenant_id, object_type, archive_message_id) "
                    "VALUES ('favorite-cross-tenant', 'tenant-b', 'message', 1)"
                )
            )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO archive_favorites "
                    "(id, tenant_id, object_type, archive_message_id) "
                    "VALUES ('favorite-invalid-type', 'tenant-a', 'image', 1)"
                )
            )

        migration.downgrade()
        inspector = inspect(connection)
        assert "archive_favorites" not in inspector.get_table_names()
        assert "uq_media_files_tenant_id_id" not in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("media_files")
        }
        assert connection.execute(
            text("SELECT sdkfileid FROM media_files WHERE id = 1")
        ).scalar_one() == "sdk-a1"

        migration.upgrade()
        assert "archive_favorites" in inspect(connection).get_table_names()

    engine.dispose()


def test_favorites_migration_is_the_single_alembic_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0074"
    assert migration.down_revision == "0073"
