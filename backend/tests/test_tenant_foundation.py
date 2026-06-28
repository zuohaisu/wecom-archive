"""
Tests for RND-111 — tenant foundation data model.

Validates:
  - All models import and compile without errors.
  - New tenant models have the expected table names and columns.
  - tenant_id columns are present on all required archive tables.
  - Default tenant bootstrap is idempotent (requires DATABASE_URL in env).
  - Existing admin console query paths compile against the updated models.

Run (from backend/):
    pytest tests/test_tenant_foundation.py -v

Bootstrap test requires DATABASE_URL and is skipped when not set.
"""

from __future__ import annotations

import os
import uuid

import pytest


# ---------------------------------------------------------------------------
# Model import / compile tests (no DB required)
# ---------------------------------------------------------------------------


def test_models_import() -> None:
    """All ORM models import without error."""
    from app.db import models  # noqa: F401

    assert hasattr(models, "Tenant")
    assert hasattr(models, "TenantWecomConfig")
    assert hasattr(models, "AdminUser")
    assert hasattr(models, "AdminSession")
    assert hasattr(models, "SyncState")
    assert hasattr(models, "ArchiveMessage")
    assert hasattr(models, "ArchiveMessageRecipient")
    assert hasattr(models, "Contact")
    assert hasattr(models, "KeyVersion")
    assert hasattr(models, "MediaFile")


def test_tenant_table_name() -> None:
    from app.db.models import Tenant

    assert Tenant.__tablename__ == "tenants"


def test_tenant_wecom_config_table_name() -> None:
    from app.db.models import TenantWecomConfig

    assert TenantWecomConfig.__tablename__ == "tenant_wecom_configs"


def test_admin_user_table_name() -> None:
    from app.db.models import AdminUser

    assert AdminUser.__tablename__ == "admin_users"


def test_admin_session_table_name() -> None:
    from app.db.models import AdminSession

    assert AdminSession.__tablename__ == "admin_sessions"


def test_archive_message_has_tenant_id() -> None:
    from app.db.models import ArchiveMessage

    col_names = {c.name for c in ArchiveMessage.__table__.columns}
    assert "tenant_id" in col_names


def test_archive_message_recipient_has_tenant_id() -> None:
    from app.db.models import ArchiveMessageRecipient

    col_names = {c.name for c in ArchiveMessageRecipient.__table__.columns}
    assert "tenant_id" in col_names


def test_sync_state_has_tenant_id() -> None:
    from app.db.models import SyncState

    col_names = {c.name for c in SyncState.__table__.columns}
    assert "tenant_id" in col_names


def test_contact_has_tenant_id() -> None:
    from app.db.models import Contact

    col_names = {c.name for c in Contact.__table__.columns}
    assert "tenant_id" in col_names


def test_tenant_id_is_nullable_on_archive_message() -> None:
    """tenant_id must remain nullable so migration can run against existing data."""
    from app.db.models import ArchiveMessage

    col = ArchiveMessage.__table__.columns["tenant_id"]
    assert col.nullable is True


def test_tenant_slug_unique_constraint() -> None:
    from app.db.models import Tenant
    from sqlalchemy import UniqueConstraint

    uq_names = {
        c.name
        for c in Tenant.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_tenants_slug" in uq_names


def test_admin_users_unique_constraint() -> None:
    from app.db.models import AdminUser
    from sqlalchemy import UniqueConstraint

    uq_names = {
        c.name
        for c in AdminUser.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_admin_users_tenant_wecom" in uq_names


def test_tenant_wecom_config_unique_tenant() -> None:
    from app.db.models import TenantWecomConfig
    from sqlalchemy import UniqueConstraint

    uq_names = {
        c.name
        for c in TenantWecomConfig.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_tenant_wecom_configs_tenant" in uq_names


# ---------------------------------------------------------------------------
# Conversation router compiles against updated models
# ---------------------------------------------------------------------------


def test_sync_state_tenant_scoped_unique_constraint() -> None:
    """SyncState must use UNIQUE(tenant_id, corp_id), not a global UNIQUE(corp_id)."""
    from app.db.models import SyncState
    from sqlalchemy import UniqueConstraint

    uq_names = {
        c.name
        for c in SyncState.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_sync_states_tenant_corp_id" in uq_names
    # Global-only constraint must not exist on the model
    assert "uq_sync_states_corp_id" not in uq_names


def test_archive_message_tenant_scoped_unique_constraint() -> None:
    """ArchiveMessage must use UNIQUE(tenant_id, msgid), not a global UNIQUE(msgid)."""
    from app.db.models import ArchiveMessage
    from sqlalchemy import UniqueConstraint

    uq_names = {
        c.name
        for c in ArchiveMessage.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_archive_messages_tenant_msgid" in uq_names
    assert "uq_archive_messages_msgid" not in uq_names


def test_contact_tenant_scoped_unique_constraint() -> None:
    """Contact must use UNIQUE(tenant_id, wecom_userid), not a global UNIQUE(wecom_userid)."""
    from app.db.models import Contact
    from sqlalchemy import UniqueConstraint

    uq_names = {
        c.name
        for c in Contact.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_contacts_tenant_wecom_userid" in uq_names
    assert "uq_contacts_wecom_userid" not in uq_names


def test_sync_state_corp_id_not_inline_unique() -> None:
    """corp_id column must not have inline unique=True (would create a global constraint)."""
    from app.db.models import SyncState

    col = SyncState.__table__.columns["corp_id"]
    assert col.unique is not True


def test_archive_message_msgid_not_inline_unique() -> None:
    from app.db.models import ArchiveMessage

    col = ArchiveMessage.__table__.columns["msgid"]
    assert col.unique is not True


def test_contact_wecom_userid_not_inline_unique() -> None:
    from app.db.models import Contact

    col = Contact.__table__.columns["wecom_userid"]
    assert col.unique is not True


def test_conversations_router_imports() -> None:
    """Conversations router must import without error after model changes."""
    from app.routers import conversations  # noqa: F401

    assert hasattr(conversations, "router")


# ---------------------------------------------------------------------------
# Sync worker — mandatory tenant resolution (no DB required)
# ---------------------------------------------------------------------------


def test_require_tenant_id_exits_when_row_missing() -> None:
    """_require_tenant_id must exit(1) when no active tenant config row exists."""
    import inspect
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
    from unittest.mock import MagicMock

    from sync_wecom_archive_once import _require_tenant_id

    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(SystemExit) as exc_info:
        _require_tenant_id(mock_session, "test_corp")

    assert exc_info.value.code == 1


def test_require_tenant_id_exits_on_db_error() -> None:
    """_require_tenant_id must exit(1) when the tenant table is absent (migration not applied)."""
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
    from unittest.mock import MagicMock

    from sync_wecom_archive_once import _require_tenant_id

    mock_session = MagicMock()
    mock_session.query.side_effect = Exception("relation \"tenant_wecom_configs\" does not exist")

    with pytest.raises(SystemExit) as exc_info:
        _require_tenant_id(mock_session, "test_corp")

    assert exc_info.value.code == 1


def test_require_tenant_id_returns_string_when_found() -> None:
    """_require_tenant_id must return the tenant_id string when an active row exists."""
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
    from unittest.mock import MagicMock

    from sync_wecom_archive_once import _require_tenant_id

    mock_row = MagicMock()
    mock_row.tenant_id = "00000000-0000-0000-0000-000000000001"
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = mock_row

    result = _require_tenant_id(mock_session, "test_corp")

    assert result == "00000000-0000-0000-0000-000000000001"


def test_read_seq_requires_tenant_id() -> None:
    """_read_seq must require tenant_id with no None default (no fallback to global lookup)."""
    import inspect
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

    from sync_wecom_archive_once import _read_seq

    sig = inspect.signature(_read_seq)
    param = sig.parameters["tenant_id"]
    assert param.default is inspect.Parameter.empty, (
        "_read_seq tenant_id must be a required parameter, not optional"
    )


def test_upsert_seq_requires_tenant_id() -> None:
    """_upsert_seq must require tenant_id with no None default (no fallback to global lookup)."""
    import inspect
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

    from sync_wecom_archive_once import _upsert_seq

    sig = inspect.signature(_upsert_seq)
    param = sig.parameters["tenant_id"]
    assert param.default is inspect.Parameter.empty, (
        "_upsert_seq tenant_id must be a required parameter, not optional"
    )


# ---------------------------------------------------------------------------
# Bootstrap idempotency test (requires DATABASE_URL)
# ---------------------------------------------------------------------------

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_bootstrap_default_tenant_idempotent() -> None:
    """Running bootstrap twice does not raise and leaves row counts stable."""
    import importlib
    import sys

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    database_url = os.environ["DATABASE_URL"]
    engine = create_engine(database_url)

    # Ensure migration 0002 has been applied — skip if not.
    with Session(engine) as session:
        try:
            session.execute(text("SELECT 1 FROM tenants LIMIT 1"))
        except Exception:
            pytest.skip("Migration 0002 not applied — run alembic upgrade head first")

    # Override env vars required by bootstrap script.
    os.environ.setdefault("WECOM_CORP_ID", "test_corp_bootstrap")
    os.environ.setdefault("WECOM_AGENT_ID", "1000001")
    os.environ.setdefault("WECOM_OAUTH_SECRET", "test_secret_placeholder")

    # Import bootstrap module from scripts/
    scripts_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import bootstrap_default_tenant as bt

    # Run once — should succeed.
    bt._step_upsert_tenant.__wrapped__ if hasattr(bt._step_upsert_tenant, "__wrapped__") else None

    with Session(engine) as session:
        bt._step_upsert_tenant(session)
        count_before = session.execute(
            text("SELECT COUNT(*) FROM tenants WHERE id = :tid"),
            {"tid": bt.DEFAULT_TENANT_ID},
        ).scalar()
        assert count_before == 1

        # Run again — ON CONFLICT DO NOTHING, count stays 1.
        bt._step_upsert_tenant(session)
        count_after = session.execute(
            text("SELECT COUNT(*) FROM tenants WHERE id = :tid"),
            {"tid": bt.DEFAULT_TENANT_ID},
        ).scalar()
        assert count_after == 1


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_archive_queries_still_work() -> None:
    """Existing conversations router queries compile and execute without error."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact

    engine = create_engine(os.environ["DATABASE_URL"])

    with Session(engine) as db:
        # These are the same query patterns used by the conversations router.
        _ = db.query(ArchiveMessage).limit(1).all()
        _ = db.query(ArchiveMessageRecipient).limit(1).all()
        _ = db.query(Contact).limit(1).all()
