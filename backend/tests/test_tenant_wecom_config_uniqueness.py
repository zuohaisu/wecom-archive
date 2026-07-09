"""
Tests for RND-184 — tenant_wecom_configs corp_id uniqueness.

QA follow-up on RND-156: auth and sync both resolve tenant context by
corp_id (WHERE is_active = true). If two active tenant_wecom_configs rows
shared a corp_id, resolution was ambiguous and could select the wrong
tenant. This closes that gap with:

  - a DB-level partial unique index on corp_id (active rows only) —
    uq_tenant_wecom_configs_active_corp_id (see app/db/models.py,
    alembic/versions/0004_tenant_wecom_config_corp_id_uniqueness.py)
  - an ORM-level guard (before_insert/before_update) that raises
    DuplicateCorpIdError with an operator-facing message before the DB
    constraint would otherwise reject the write

Uses a real sqlite-backed DB via Base.metadata.create_all restricted to the
Tenant / TenantWecomConfig tables, same pattern as test_tenant_sync_state.py.
SQLite supports partial unique indexes (CREATE UNIQUE INDEX ... WHERE ...)
so the DB-level constraint is exercised for real here, not mocked — the
model declares sqlite_where alongside postgresql_where for exactly this
reason. Production runs Postgres; the constraint there is validated by the
Alembic migration's duplicate-detection guard and identical index semantics.

Run (from backend/):
    pytest tests/test_tenant_wecom_config_uniqueness.py -v
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import DuplicateCorpIdError, Tenant, TenantWecomConfig
from scripts.sync_wecom_archive_once import _require_tenant_id


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, TenantWecomConfig.__table__],
    )
    session = Session(engine)
    yield session
    session.close()


def _make_tenant(db: Session, tenant_id: str, slug: str) -> Tenant:
    tenant = Tenant(id=tenant_id, name=slug, slug=slug, is_active=True)
    db.add(tenant)
    db.commit()
    return tenant


def _make_config(
    db: Session,
    config_id: str,
    tenant_id: str,
    corp_id: str,
    is_active: bool = True,
) -> TenantWecomConfig:
    config = TenantWecomConfig(
        id=config_id,
        tenant_id=tenant_id,
        corp_id=corp_id,
        agent_id="1000001",
        app_secret="secret",
        is_active=is_active,
    )
    db.add(config)
    db.commit()
    return config


# ---------------------------------------------------------------------------
# Application-level validation (ORM before_insert / before_update)
# ---------------------------------------------------------------------------


def test_first_config_with_corp_id_succeeds(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    config = _make_config(db, "cfg-a", "tenant-a", "ww123")
    assert config.corp_id == "ww123"


def test_second_active_config_with_same_corp_id_fails(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")
    _make_config(db, "cfg-a", "tenant-a", "ww123")

    with pytest.raises(DuplicateCorpIdError, match="already assigned to another tenant"):
        _make_config(db, "cfg-b", "tenant-b", "ww123")

    db.rollback()
    # Only the first config was persisted.
    assert db.query(TenantWecomConfig).count() == 1


def test_updating_config_to_another_tenants_corp_id_fails(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")
    _make_config(db, "cfg-a", "tenant-a", "ww123")
    config_b = _make_config(db, "cfg-b", "tenant-b", "ww456")

    config_b.corp_id = "ww123"
    with pytest.raises(DuplicateCorpIdError, match="already assigned to another tenant"):
        db.commit()

    db.rollback()
    assert db.query(TenantWecomConfig).filter_by(id="cfg-b").one().corp_id == "ww456"


def test_updating_config_without_changing_corp_id_succeeds(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    config = _make_config(db, "cfg-a", "tenant-a", "ww123")

    config.agent_id = "1000002"
    config.corp_id = "ww123"  # unchanged
    db.commit()

    assert db.query(TenantWecomConfig).filter_by(id="cfg-a").one().agent_id == "1000002"


def test_reusing_corp_id_from_a_deactivated_config_succeeds(db) -> None:
    """A corp_id may be reassigned once the old config is deactivated —
    only active rows participate in the uniqueness rule."""
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")
    config_a = _make_config(db, "cfg-a", "tenant-a", "ww123")

    config_a.is_active = False
    db.commit()

    config_b = _make_config(db, "cfg-b", "tenant-b", "ww123")
    assert config_b.corp_id == "ww123"


def test_creating_inactive_config_with_duplicate_corp_id_succeeds(db) -> None:
    """A config created directly as inactive never competes for the corp_id."""
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")
    _make_config(db, "cfg-a", "tenant-a", "ww123")

    config_b = _make_config(db, "cfg-b", "tenant-b", "ww123", is_active=False)
    assert config_b.is_active is False


# ---------------------------------------------------------------------------
# Database-level constraint — exercised via Core insert() to bypass the ORM
# before_insert event entirely, proving the DB itself (not just app code)
# rejects duplicate active corp_id rows.
# ---------------------------------------------------------------------------


def test_db_constraint_rejects_duplicate_active_corp_id_via_core_insert(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")

    db.execute(
        insert(TenantWecomConfig.__table__).values(
            id="cfg-a",
            tenant_id="tenant-a",
            corp_id="ww123",
            agent_id="1000001",
            app_secret="secret",
            is_active=True,
        )
    )
    db.commit()

    with pytest.raises(IntegrityError):
        db.execute(
            insert(TenantWecomConfig.__table__).values(
                id="cfg-b",
                tenant_id="tenant-b",
                corp_id="ww123",
                agent_id="1000002",
                app_secret="secret",
                is_active=True,
            )
        )
        db.commit()

    db.rollback()
    assert db.query(TenantWecomConfig).count() == 1


def test_db_constraint_allows_duplicate_corp_id_when_inactive_via_core_insert(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")

    db.execute(
        insert(TenantWecomConfig.__table__).values(
            id="cfg-a",
            tenant_id="tenant-a",
            corp_id="ww123",
            agent_id="1000001",
            app_secret="secret",
            is_active=False,
        )
    )
    db.execute(
        insert(TenantWecomConfig.__table__).values(
            id="cfg-b",
            tenant_id="tenant-b",
            corp_id="ww123",
            agent_id="1000002",
            app_secret="secret",
            is_active=False,
        )
    )
    db.commit()

    assert db.query(TenantWecomConfig).count() == 2


# ---------------------------------------------------------------------------
# Auth/sync tenant resolution stays deterministic because duplicates are
# blocked before they can be persisted.
# ---------------------------------------------------------------------------


def test_tenant_resolution_deterministic_after_blocked_duplicate(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")
    _make_config(db, "cfg-a", "tenant-a", "ww123")

    with pytest.raises(DuplicateCorpIdError):
        _make_config(db, "cfg-b", "tenant-b", "ww123")
    db.rollback()

    # Resolution is unambiguous: exactly the original tenant comes back.
    assert _require_tenant_id(db, "ww123") == "tenant-a"
