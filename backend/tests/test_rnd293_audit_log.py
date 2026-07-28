"""Schema coverage for RND-293 A7-1 immutable audit logs."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import Text, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.db.models import AdminUser, AuditLog, Tenant


_AUDIT_LOG_COLUMNS = {
    "id",
    "tenant_id",
    "admin_user_id",
    "action",
    "object_type",
    "object_id",
    "detail",
    "created_at",
}
_AUDIT_LOG_INDEXES = {
    "ix_audit_logs_tenant_id",
    "ix_audit_logs_admin_user_id",
    "ix_audit_logs_created_at",
}
_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


def test_audit_log_model_has_immutable_schema() -> None:
    columns = AuditLog.__table__.columns
    assert _AUDIT_LOG_COLUMNS <= set(columns.keys())
    assert "updated_at" not in columns
    assert all(column.onupdate is None for column in columns)

    assert isinstance(columns["action"].type, Text)
    assert isinstance(columns["object_type"].type, Text)
    assert isinstance(columns["detail"].type, JSONB)
    assert columns["created_at"].server_default is not None


def test_audit_log_foreign_keys_and_indexes() -> None:
    columns = AuditLog.__table__.columns
    tenant_fk = next(iter(columns["tenant_id"].foreign_keys))
    admin_user_fk = next(iter(columns["admin_user_id"].foreign_keys))
    assert tenant_fk.target_fullname == "tenants.id"
    assert admin_user_fk.target_fullname == "admin_users.id"

    index_names = {index.name for index in AuditLog.__table__.indexes}
    assert _AUDIT_LOG_INDEXES <= index_names


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_audit_log_is_backed_by_postgres_schema() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    audit_log_id = str(uuid.uuid4())

    with Session(engine) as session:
        try:
            session.add(Tenant(id=tenant_id, name="RND-293 test", slug=f"rnd293-{tenant_id}"))
            session.flush()
            session.add(
                AdminUser(
                    id=user_id,
                    tenant_id=tenant_id,
                    wecom_user_id=f"rnd293-{user_id}",
                    name="RND-293 audit actor",
                    last_login_at=datetime.now(timezone.utc),
                )
            )
            session.flush()
            session.add(
                AuditLog(
                    id=audit_log_id,
                    tenant_id=tenant_id,
                    admin_user_id=user_id,
                    action="config.view",
                    object_type="tenant_config",
                    object_id=None,
                    detail={"ip": "127.0.0.1"},
                )
            )
            session.commit()

            inserted = session.get(AuditLog, audit_log_id)
            assert inserted is not None
            assert inserted.tenant_id == tenant_id
            assert inserted.admin_user_id == user_id
            assert inserted.action == "config.view"
            assert inserted.object_type == "tenant_config"
            assert inserted.object_id is None
            assert inserted.detail == {"ip": "127.0.0.1"}
            assert inserted.created_at is not None

            column_names = set(
                session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'audit_logs'"
                    )
                ).scalars()
            )
            assert _AUDIT_LOG_COLUMNS <= column_names
            assert "updated_at" not in column_names
        finally:
            session.rollback()
            session.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": audit_log_id})
            session.execute(text("DELETE FROM admin_users WHERE id = :id"), {"id": user_id})
            session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
            session.commit()
