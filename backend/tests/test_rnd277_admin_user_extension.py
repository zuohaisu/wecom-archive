"""Schema coverage for RND-277 F0-1 AdminUser account-system fields."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import Enum, create_engine, text
from sqlalchemy.orm import Session

from app.db.models import AdminUser, Tenant


_ACCOUNT_COLUMNS = {
    "password_hash",
    "role",
    "status",
    "email",
    "phone",
    "department",
    "last_active_at",
    "invite_token",
    "invited_by",
    "invite_status",
}
_ROLE_VALUES = ("owner", "admin", "compliance", "legal", "readonlyaudit")
_STATUS_VALUES = ("active", "disabled")
_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


def test_admin_user_account_columns_and_enums() -> None:
    columns = AdminUser.__table__.columns
    assert _ACCOUNT_COLUMNS <= set(columns.keys())

    role = columns["role"]
    assert isinstance(role.type, Enum)
    assert role.type.name == "admin_user_role"
    assert tuple(role.type.enums) == _ROLE_VALUES
    assert role.server_default is not None

    status = columns["status"]
    assert isinstance(status.type, Enum)
    assert status.type.name == "admin_user_status"
    assert tuple(status.type.enums) == _STATUS_VALUES
    assert status.server_default is not None


def test_admin_user_existing_tenant_wecom_unique_constraint_remains() -> None:
    from sqlalchemy import UniqueConstraint

    constraint_names = {
        constraint.name
        for constraint in AdminUser.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_admin_users_tenant_wecom" in constraint_names


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_admin_user_account_fields_are_backed_by_postgres_schema() -> None:
    """Verify head schema and legacy login-shaped inserts against PostgreSQL."""
    engine = create_engine(os.environ["DATABASE_URL"])
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    with Session(engine) as session:
        try:
            column_names = set(
                session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'admin_users'"
                    )
                ).scalars()
            )
            assert _ACCOUNT_COLUMNS <= column_names

            enum_rows = session.execute(
                text(
                    "SELECT type.typname, enum.enumlabel "
                    "FROM pg_type AS type "
                    "JOIN pg_enum AS enum ON enum.enumtypid = type.oid "
                    "WHERE type.typname IN ('admin_user_role', 'admin_user_status') "
                    "ORDER BY type.typname, enum.enumsortorder"
                )
            ).all()
            enum_values = {}
            for type_name, label in enum_rows:
                enum_values.setdefault(type_name, []).append(label)
            assert tuple(enum_values["admin_user_role"]) == _ROLE_VALUES
            assert tuple(enum_values["admin_user_status"]) == _STATUS_VALUES

            session.add(Tenant(id=tenant_id, name="RND-277 test", slug=f"rnd277-{tenant_id}"))
            session.add(
                AdminUser(
                    id=user_id,
                    tenant_id=tenant_id,
                    wecom_user_id=f"rnd277-{user_id}",
                    name="RND-277 login-path regression",
                    last_login_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            inserted = session.get(AdminUser, user_id)
            assert inserted is not None
            assert inserted.role == "admin"
            assert inserted.status == "active"
        finally:
            session.rollback()
            session.execute(text("DELETE FROM admin_users WHERE id = :id"), {"id": user_id})
            session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
            session.commit()
