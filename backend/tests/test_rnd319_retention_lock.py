"""Acceptance coverage for RND-319's independent retention-lock script."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import ArchiveMessage, AuditLog, RetentionLock
from scripts.apply_retention_lock_once import run_retention_lock_once

_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0030_retention_locks.py"
)


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.connection.driver_connection.executescript(
            """
            CREATE TABLE retention_configs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL UNIQUE,
                retention_days INTEGER NOT NULL,
                is_locked BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME
            );
            CREATE TABLE archive_messages (
                id INTEGER PRIMARY KEY,
                msgid TEXT NOT NULL,
                seq INTEGER NOT NULL,
                publickey_ver INTEGER NOT NULL,
                raw_encrypted_payload TEXT,
                encrypt_random_key TEXT NOT NULL,
                encrypt_chat_msg TEXT NOT NULL,
                decrypt_status TEXT NOT NULL,
                decrypted_payload TEXT,
                structured_content TEXT,
                content_text TEXT,
                msgtype TEXT,
                sender TEXT,
                roomid TEXT,
                msgtime INTEGER,
                tolist TEXT,
                sdkfileid TEXT,
                is_revoked BOOLEAN NOT NULL DEFAULT 0,
                revoked_at DATETIME,
                tenant_id TEXT,
                created_at DATETIME
            );
            CREATE TABLE retention_locks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                archive_message_id INTEGER NOT NULL UNIQUE,
                locked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE audit_logs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                admin_user_id TEXT,
                action TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT,
                detail JSON,
                created_at DATETIME NOT NULL
            );
            """
        )
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _message_time(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _add_config(db: Session, tenant_id: str, retention_days: int = 30) -> None:
    db.execute(
        text(
            "INSERT INTO retention_configs (id, tenant_id, retention_days, is_locked) "
            "VALUES (:id, :tenant_id, :retention_days, 0)"
        ),
        {"id": f"config-{tenant_id}", "tenant_id": tenant_id, "retention_days": retention_days},
    )


def _add_message(db: Session, message_id: int, tenant_id: str, msgtime: int) -> None:
    db.add(
        ArchiveMessage(
            id=message_id,
            msgid=f"message-{message_id}",
            seq=message_id,
            publickey_ver=1,
            encrypt_random_key="encrypted-key",
            encrypt_chat_msg="encrypted-message",
            decrypt_status="success",
            tenant_id=tenant_id,
            msgtime=msgtime,
        )
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def test_locks_only_expired_messages_for_configured_tenants_and_audits(db: Session) -> None:
    cutoff = _NOW - timedelta(days=30)
    _add_config(db, "configured-expired")
    _add_config(db, "configured-current")
    _add_message(db, 1, "configured-expired", _message_time(cutoff - timedelta(milliseconds=1)))
    _add_message(db, 2, "configured-expired", _message_time(cutoff))
    _add_message(db, 3, "configured-current", _message_time(_NOW))
    _add_message(db, 4, "unconfigured", _message_time(cutoff - timedelta(days=365)))
    db.commit()

    summary = run_retention_lock_once(db, now=_NOW)
    db.expire_all()

    assert summary == {"configured-expired": 1, "configured-current": 0}
    locks = db.query(RetentionLock).all()
    assert len(locks) == 1
    assert locks[0].archive_message_id == 1
    assert locks[0].tenant_id == "configured-expired"
    assert _as_utc(locks[0].locked_at) == _NOW
    assert db.query(RetentionLock).filter_by(archive_message_id=2).count() == 0
    assert db.query(RetentionLock).filter_by(archive_message_id=3).count() == 0
    assert db.query(RetentionLock).filter_by(archive_message_id=4).count() == 0

    audits = db.query(AuditLog).all()
    assert len(audits) == 1
    assert audits[0].tenant_id == "configured-expired"
    assert audits[0].action == "retention.messages_locked"
    assert audits[0].object_type == "tenant"
    assert audits[0].detail == {"locked_count": 1, "cutoff": cutoff.isoformat()}


def test_second_run_is_idempotent_and_does_not_write_empty_audit(db: Session) -> None:
    _add_config(db, "configured")
    _add_message(db, 1, "configured", _message_time(_NOW - timedelta(days=31)))
    db.commit()

    assert run_retention_lock_once(db, now=_NOW) == {"configured": 1}
    db.expire_all()
    first_lock = db.query(RetentionLock).one()

    assert run_retention_lock_once(db, now=_NOW + timedelta(days=1)) == {"configured": 0}
    db.expire_all()
    assert db.query(RetentionLock).count() == 1
    assert db.query(RetentionLock).one().id == first_lock.id
    assert _as_utc(db.query(RetentionLock).one().locked_at) == _NOW
    assert db.query(AuditLog).filter(AuditLog.action == "retention.messages_locked").count() == 1


def test_retention_lock_unique_constraint_is_the_database_backstop(db: Session) -> None:
    db.add_all(
        [
            RetentionLock(id="lock-a", tenant_id="tenant-a", archive_message_id=1, locked_at=_NOW),
            RetentionLock(id="lock-b", tenant_id="tenant-a", archive_message_id=1, locked_at=_NOW),
        ]
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_retention_lock_migration_upgrade_and_downgrade() -> None:
    spec = importlib.util.spec_from_file_location("rnd319_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tenants (id TEXT PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE archive_messages (id INTEGER PRIMARY KEY)"))
        operations = Operations(MigrationContext.configure(connection))
        previous_proxy = getattr(migration.op, "_proxy", None)
        migration.op._proxy = operations
        try:
            migration.upgrade()
            columns = {
                column["name"] for column in inspect(connection).get_columns("retention_locks")
            }
            assert columns == {"id", "tenant_id", "archive_message_id", "locked_at"}
            constraints = inspect(connection).get_unique_constraints("retention_locks")
            assert constraints == [
                {"name": "uq_retention_locks_archive_message_id", "column_names": ["archive_message_id"]}
            ]
            migration.downgrade()
            assert "retention_locks" not in inspect(connection).get_table_names()
        finally:
            migration.op._proxy = previous_proxy
    engine.dispose()
