"""Schema and authentication coverage for RND-306 PlatformAdmin."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import Enum, Text, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import hash_password, verify_platform_admin
from app.db.models import PlatformAdmin


_COLUMNS = {
    "id",
    "name",
    "email",
    "password_hash",
    "role",
    "status",
    "created_at",
    "last_active_at",
    "last_login_at",
}
_ROLE_VALUES = ("superadmin",)
_STATUS_VALUES = ("active", "disabled", "pending")
_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


def test_platform_admin_symbols_and_tenantless_model_smoke() -> None:
    """PlatformAdmin and its model-layer authentication primitive import cleanly."""
    assert PlatformAdmin.__tablename__ == "platform_admins"
    assert callable(verify_platform_admin)

    columns = PlatformAdmin.__table__.columns
    assert _COLUMNS == set(columns.keys())
    assert "tenant_id" not in columns
    assert isinstance(columns["email"].type, Text)

    role = columns["role"]
    assert isinstance(role.type, Enum)
    assert role.type.name == "platform_admin_role"
    assert tuple(role.type.enums) == _ROLE_VALUES
    assert role.server_default is not None

    status = columns["status"]
    assert isinstance(status.type, Enum)
    assert status.type.name == "platform_admin_status"
    assert tuple(status.type.enums) == _STATUS_VALUES
    assert status.server_default is not None

    constraint_names = {
        constraint.name
        for constraint in PlatformAdmin.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert "uq_platform_admins_email" in constraint_names
    assert {index.name for index in PlatformAdmin.__table__.indexes} == {
        "ix_platform_admins_status"
    }


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_platform_admin_postgres_schema_and_authentication() -> None:
    """Exercise the migrated PostgreSQL schema and active-admin credentials."""
    engine = create_engine(os.environ["DATABASE_URL"])
    admin_id = str(uuid.uuid4())
    duplicate_id = str(uuid.uuid4())

    try:
        with Session(engine) as db:
            db.add(
                PlatformAdmin(
                    id=admin_id,
                    email="platform-admin@example.com",
                    password_hash=hash_password("secret123"),
                    role="superadmin",
                    status="active",
                )
            )
            db.commit()

            assert (
                verify_platform_admin(db, " PLATFORM-ADMIN@EXAMPLE.COM ", "secret123").id
                == admin_id
            )
            assert verify_platform_admin(db, "platform-admin@example.com", "wrong") is None
            assert verify_platform_admin(db, "unknown@example.com", "secret123") is None

            db.add(
                PlatformAdmin(
                    id=duplicate_id,
                    email="platform-admin@example.com",
                    password_hash=hash_password("secret123"),
                )
            )
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()

            admin = db.get(PlatformAdmin, admin_id)
            assert admin is not None
            admin.status = "disabled"
            db.commit()
            assert verify_platform_admin(db, "platform-admin@example.com", "secret123") is None

        inspector = inspect(engine)
        assert "platform_admins" in inspector.get_table_names()
        columns = {column["name"]: column for column in inspector.get_columns("platform_admins")}
        assert _COLUMNS == set(columns)
        assert "tenant_id" not in columns
        assert columns["role"]["type"].name == "platform_admin_role"
        assert columns["status"]["type"].name == "platform_admin_status"
        assert "uq_platform_admins_email" in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("platform_admins")
        }

        with engine.connect() as connection:
            enum_rows = connection.execute(
                text(
                    "SELECT type.typname, enum.enumlabel "
                    "FROM pg_type AS type "
                    "JOIN pg_enum AS enum ON enum.enumtypid = type.oid "
                    "WHERE type.typname IN "
                    "('platform_admin_role', 'platform_admin_status') "
                    "ORDER BY type.typname, enum.enumsortorder"
                )
            ).all()
        enum_values: dict[str, list[str]] = {}
        for type_name, label in enum_rows:
            enum_values.setdefault(type_name, []).append(label)
        assert tuple(enum_values["platform_admin_role"]) == _ROLE_VALUES
        assert tuple(enum_values["platform_admin_status"]) == _STATUS_VALUES
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM platform_admins WHERE id IN (:admin_id, :duplicate_id)"),
                {"admin_id": admin_id, "duplicate_id": duplicate_id},
            )
        engine.dispose()
