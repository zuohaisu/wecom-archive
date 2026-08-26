"""RND-362 soft-delete lifecycle tests."""

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AdminUser, ArchiveMessage, AuditLog, Tenant
from app.services.message_deletion import (
    DeletionLockedError,
    active_message_filter,
    restore_messages,
    soft_delete_messages,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    fts_index = next(index for index in ArchiveMessage.__table__.indexes if index.name == "ix_archive_messages_content_text_fts")
    ArchiveMessage.__table__.indexes.remove(fts_index)
    try:
        Base.metadata.create_all(engine, tables=[Tenant.__table__, AdminUser.__table__, ArchiveMessage.__table__, AuditLog.__table__])
    finally:
        ArchiveMessage.__table__.indexes.add(fts_index)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id="tenant-a", name="A", slug="a"))
        db.add(Tenant(id="tenant-b", name="B", slug="b"))
        db.add(AdminUser(id="owner-a", tenant_id="tenant-a", wecom_user_id="owner", role="owner"))
        for message_id, (tenant_id, msgid) in enumerate(
            (("tenant-a", "a-1"), ("tenant-a", "a-2"), ("tenant-b", "b-1")), start=1
        ):
            db.add(ArchiveMessage(
                id=message_id, msgid=msgid, seq=1, publickey_ver=1, encrypt_random_key="key",
                encrypt_chat_msg="payload", tenant_id=tenant_id, decrypt_status="success",
            ))
        db.commit()
    return factory


def test_soft_delete_is_tenant_scoped_idempotent_and_hidden_from_active_reads():
    factory = _factory()
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    with factory() as db:
        result = soft_delete_messages(
            db, tenant_id="tenant-a", actor_id="owner-a", msgids=["a-1", "b-1", "missing"], at=now
        )
        assert (result.deleted, result.already_deleted, result.not_found) == (1, 0, 2)
        again = soft_delete_messages(db, tenant_id="tenant-a", actor_id="owner-a", msgids=["a-1"], at=now)
        assert (again.deleted, again.already_deleted, again.not_found) == (0, 1, 0)
        active = db.query(ArchiveMessage).filter(ArchiveMessage.tenant_id == "tenant-a", active_message_filter()).all()
        assert [row.msgid for row in active] == ["a-2"]
        deleted = db.query(ArchiveMessage).filter_by(tenant_id="tenant-a", msgid="a-1").one()
        assert deleted.purge_after.date().isoformat() == "2026-09-23"
        assert db.query(AuditLog).filter_by(action="messages.soft_deleted").count() == 2


def test_restore_and_deletion_lock_fail_closed():
    factory = _factory()
    with factory() as db:
        soft_delete_messages(db, tenant_id="tenant-a", actor_id="owner-a", msgids=["a-1"])
        result = restore_messages(db, tenant_id="tenant-a", actor_id="owner-a", msgids=["a-1"])
        assert (result.deleted, result.already_deleted, result.not_found) == (1, 0, 0)
        db.get(Tenant, "tenant-a").deletion_locked = True
        try:
            soft_delete_messages(db, tenant_id="tenant-a", actor_id="owner-a", msgids=["a-2"])
        except DeletionLockedError:
            pass
        else:
            raise AssertionError("compliance hold must reject deletion")
