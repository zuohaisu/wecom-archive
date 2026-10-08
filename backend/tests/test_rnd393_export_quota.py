"""RND-393 server-authoritative monthly export quota coverage."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import ExportMonthlyUsage, Tenant
from app.services.export_quota import (
    ExportQuotaExceeded,
    consume_export_quota,
    get_export_quota_summary,
)


AUGUST = datetime(2026, 8, 31, 15, 59, tzinfo=timezone.utc)
SEPTEMBER = datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine, tables=[Tenant.__table__, ExportMonthlyUsage.__table__]
    )
    with Session(engine) as session:
        session.add_all(
            [
                Tenant(id="tenant-a", name="A", slug="tenant-a"),
                Tenant(id="tenant-b", name="B", slug="tenant-b"),
            ]
        )
        session.commit()
        yield session


def test_text_tenth_is_allowed_and_eleventh_is_rejected(db: Session) -> None:
    for expected_used in range(1, 11):
        bucket = consume_export_quota(db, "tenant-a", "text", at=AUGUST)
        assert bucket.used == expected_used

    with pytest.raises(ExportQuotaExceeded) as error:
        consume_export_quota(db, "tenant-a", "text", at=AUGUST)

    assert error.value.bucket.limit == 10
    assert error.value.bucket.remaining == 0


def test_media_second_is_rejected_and_tenants_are_isolated(db: Session) -> None:
    first_a = consume_export_quota(db, "tenant-a", "media_zip", at=AUGUST)
    first_b = consume_export_quota(db, "tenant-b", "media_zip", at=AUGUST)
    assert first_a.used == first_b.used == 1

    with pytest.raises(ExportQuotaExceeded):
        consume_export_quota(db, "tenant-a", "media_zip", at=AUGUST)
    with pytest.raises(ExportQuotaExceeded):
        consume_export_quota(db, "tenant-b", "media_zip", at=AUGUST)


def test_asia_shanghai_natural_month_resets_at_exact_boundary(db: Session) -> None:
    august = consume_export_quota(db, "tenant-a", "media_zip", at=AUGUST)
    september = consume_export_quota(db, "tenant-a", "media_zip", at=SEPTEMBER)

    assert august.period_start.isoformat() == "2026-08-01"
    assert september.period_start.isoformat() == "2026-09-01"
    assert september.used == 1
    summary = get_export_quota_summary(db, "tenant-a", at=SEPTEMBER)
    assert summary.resets_at.isoformat() == "2026-10-01T00:00:00+08:00"


def test_selfhost_exports_do_not_apply_monthly_commercial_limits(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_EDITION", "selfhost")

    for expected_used in range(1, 13):
        bucket = consume_export_quota(db, "tenant-a", "text", at=AUGUST)
        assert bucket.used == expected_used
        assert bucket.limit is None and bucket.remaining is None
    for expected_used in range(1, 4):
        bucket = consume_export_quota(db, "tenant-a", "media_zip", at=AUGUST)
        assert bucket.used == expected_used
        assert bucket.limit is None and bucket.remaining is None

    summary = get_export_quota_summary(db, "tenant-a", at=AUGUST)
    assert summary.text.limit is None and summary.text.remaining is None
    assert summary.media_zip.limit is None and summary.media_zip.remaining is None


def test_quota_consumption_is_rollback_safe_and_takes_tenant_row_lock(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_scalar = db.scalar
    lock_seen = False

    def tracking_scalar(statement, *args, **kwargs):
        nonlocal lock_seen
        entity = statement.column_descriptions[0].get("entity")
        if entity is Tenant:
            lock_seen = statement._for_update_arg is not None
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db, "scalar", tracking_scalar)
    consume_export_quota(db, "tenant-a", "text", at=AUGUST)
    assert lock_seen is True
    db.rollback()
    assert db.scalar(select(ExportMonthlyUsage)) is None


def test_0047_migration_creates_and_removes_authoritative_counter(tmp_path) -> None:
    migration_path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "0047_rnd393_export_monthly_usage.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0047", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    engine = create_engine(f"sqlite:///{tmp_path / 'quota-migration.db'}")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY)"))
        operations = Operations(MigrationContext.configure(connection))
        migration.op = operations
        migration.upgrade()
        columns = {
            row[1]
            for row in connection.execute(
                text("PRAGMA table_info(export_monthly_usage)")
            )
        }
        assert {"tenant_id", "period_start", "export_type", "used_count"} <= columns
        migration.downgrade()
        assert connection.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='export_monthly_usage'"
            )
        ).fetchone() is None
