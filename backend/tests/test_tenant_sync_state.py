"""
Tests for RND-156 — Multi-tenant WeCom sync state isolation.

Validates:
  - Multiple tenants can each have their own WeCom config (corp_id).
  - _require_tenant_id() (scripts/sync_wecom_archive_once.py) resolves the
    correct tenant for a given corp_id, and only ever considers active
    tenant_wecom_configs rows.
  - _read_seq() / _upsert_seq() are scoped by (tenant_id, corp_id), not
    corp_id alone — two tenants' sync cursors never interfere, including
    the defensive edge case of a shared corp_id string.

Uses a real sqlite-backed DB via Base.metadata.create_all restricted to the
Tenant / TenantWecomConfig / SyncState tables — none of these three models
use postgres-only column types (JSONB/GIN), unlike ArchiveMessage, so the
real ORM models and real sync-cursor functions can be exercised directly
without a hand-written schema or a live Postgres instance.

Run (from backend/):
    pytest tests/test_tenant_sync_state.py -v
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import SyncState, Tenant, TenantWecomConfig
from scripts.sync_wecom_archive_once import _read_seq, _require_tenant_id, _upsert_seq


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, TenantWecomConfig.__table__, SyncState.__table__],
    )
    session = Session(engine)
    yield session
    session.close()


def _make_tenant(db: Session, tenant_id: str, slug: str) -> Tenant:
    tenant = Tenant(id=tenant_id, name=slug, slug=slug)
    db.add(tenant)
    db.commit()
    return tenant


def _make_config(
    db: Session, tenant_id: str, corp_id: str, is_active: bool = True
) -> TenantWecomConfig:
    config = TenantWecomConfig(
        id=f"cfg-{tenant_id}-{corp_id}",
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
# Multiple tenants can exist
# ---------------------------------------------------------------------------


def test_multiple_tenants_can_exist(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")
    assert db.query(Tenant).count() == 2


# ---------------------------------------------------------------------------
# _require_tenant_id — corp_id -> tenant_id resolution
# ---------------------------------------------------------------------------


def test_require_tenant_id_resolves_correct_tenant_per_corp(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")
    _make_config(db, "tenant-a", "corpA")
    _make_config(db, "tenant-b", "corpB")

    assert _require_tenant_id(db, "corpA") == "tenant-a"
    assert _require_tenant_id(db, "corpB") == "tenant-b"


def test_require_tenant_id_ignores_inactive_config(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    _make_config(db, "tenant-a", "corpA", is_active=False)

    with pytest.raises(SystemExit) as exc:
        _require_tenant_id(db, "corpA")
    assert exc.value.code == 1


def test_require_tenant_id_fails_for_unknown_corp(db) -> None:
    with pytest.raises(SystemExit) as exc:
        _require_tenant_id(db, "unknown-corp")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# _read_seq / _upsert_seq — sync cursor isolation
# ---------------------------------------------------------------------------


def test_read_seq_defaults_to_zero_when_absent(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    assert _read_seq(db, "corpA", "tenant-a") == 0


def test_sync_cursors_independent_per_tenant(db) -> None:
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")

    _upsert_seq(db, "corpA", 500, "tenant-a")
    _upsert_seq(db, "corpB", 900, "tenant-b")
    db.commit()

    assert _read_seq(db, "corpA", "tenant-a") == 500
    assert _read_seq(db, "corpB", "tenant-b") == 900


def test_sync_cursor_isolated_even_with_shared_corp_id(db) -> None:
    """Defensive edge case: even if two tenants' corp_id strings happened to
    collide, the sync cursor is keyed by (tenant_id, corp_id), never
    corp_id alone (see uq_sync_states_tenant_corp_id) — advancing one
    tenant's cursor must never move the other's."""
    _make_tenant(db, "tenant-a", "acme")
    _make_tenant(db, "tenant-b", "globex")

    _upsert_seq(db, "shared-corp", 100, "tenant-a")
    _upsert_seq(db, "shared-corp", 250, "tenant-b")
    db.commit()

    assert _read_seq(db, "shared-corp", "tenant-a") == 100
    assert _read_seq(db, "shared-corp", "tenant-b") == 250

    # Advancing tenant A's cursor must not move tenant B's.
    _upsert_seq(db, "shared-corp", 700, "tenant-a")
    db.commit()
    assert _read_seq(db, "shared-corp", "tenant-a") == 700
    assert _read_seq(db, "shared-corp", "tenant-b") == 250


def test_upsert_seq_updates_existing_row_in_place(db) -> None:
    _make_tenant(db, "tenant-a", "acme")

    _upsert_seq(db, "corpA", 10, "tenant-a")
    db.commit()
    assert db.query(SyncState).count() == 1

    _upsert_seq(db, "corpA", 20, "tenant-a")
    db.commit()

    # Still exactly one row (updated, not duplicated).
    assert db.query(SyncState).count() == 1
    assert _read_seq(db, "corpA", "tenant-a") == 20
