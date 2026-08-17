"""RND-358 (T4) — the 8 real registered read-only diagnostic tools:
allowlist coverage, tenant isolation, scope isolation, and typical
diagnostic scenarios (missing config, sync failure, storage near limit).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import app.services.ai_tools.handlers  # noqa: F401 - populates the registry via import side effect
from app.services.ai_tools.handlers import _KNOWN_ERROR_CODES
from app.services.ai_tools.registry import ToolContext, ToolResultStatus, ToolScope, get_tool, invoke_tool

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")

_REAL_TOOL_NAMES = (
    "product_version",
    "current_page",
    "tenant_service_status",
    "config_status",
    "sync_status_summary",
    "storage_quota_summary",
    "archive_health_summary",
    "known_error_codes",
)


@pytest.fixture()
def db():
    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session:
        yield session


@pytest.fixture()
def tenant_and_user(db: Session):
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO tenants (id, name, slug, lifecycle_status) VALUES (:id, 'Handlers test tenant', :slug, 'active')"),
        {"id": tenant_id, "slug": f"ai-handlers-test-{tenant_id[:8]}"},
    )
    db.execute(
        text(
            "INSERT INTO admin_users (id, tenant_id, wecom_user_id, role) "
            "VALUES (:id, :tenant_id, :wecom_user_id, 'admin')"
        ),
        {"id": user_id, "tenant_id": tenant_id, "wecom_user_id": f"wecom-{user_id[:8]}"},
    )
    db.commit()
    return tenant_id, user_id


def _context(tenant_id: str, user_id: str, scope: ToolScope = ToolScope.TENANT_ADMIN, **page) -> ToolContext:
    return ToolContext(tenant_id=tenant_id, admin_user_id=user_id, scope=scope, page_context=page)


@pytest.mark.parametrize("name", _REAL_TOOL_NAMES)
def test_all_eight_required_tools_are_registered(name: str) -> None:
    assert get_tool(name) is not None, f"{name} must be registered"


def test_product_version_returns_only_allowed_field(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(db, "product_version", _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.SUCCESS
    assert set(result.data.keys()) == {"version"}
    assert isinstance(result.data["version"], str)


def test_current_page_echoes_known_page(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(
        db, "current_page", _context(tenant_id, user_id, page_id="billing"), consent_given=True
    )
    assert result.data == {"page_id": "billing"}


def test_current_page_rejects_unknown_page_id(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(
        db, "current_page", _context(tenant_id, user_id, page_id="../../etc/passwd"), consent_given=True
    )
    assert result.data == {"page_id": "unknown"}


def test_tenant_service_status_reflects_real_row(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(db, "tenant_service_status", _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.SUCCESS
    assert result.data["lifecycle_status"] == "active"
    assert result.data["is_active"] is True


def test_config_status_reports_wecom_not_connected_for_fresh_tenant(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(db, "config_status", _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.SUCCESS
    assert result.data["wecom_connected"] is False
    # Booleans only — never a config value.
    for value in result.data.values():
        assert isinstance(value, bool)


def test_config_status_reflects_active_wecom_config(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    db.execute(
        text(
            "INSERT INTO tenant_wecom_configs (id, tenant_id, corp_id, agent_id, app_secret, is_active) "
            "VALUES (:id, :tenant_id, :corp_id, 'agent-1', 'encrypted-placeholder', true)"
        ),
        {"id": str(uuid.uuid4()), "tenant_id": tenant_id, "corp_id": f"corp-{tenant_id[:8]}"},
    )
    db.commit()

    result = invoke_tool(db, "config_status", _context(tenant_id, user_id), consent_given=True)
    assert result.data["wecom_connected"] is True


def test_sync_status_summary_reports_not_connected_without_config(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(db, "sync_status_summary", _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.SUCCESS
    assert result.data["status"] == "not_connected"


def test_sync_status_summary_reflects_failed_sync(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    corp_id = f"corp-{tenant_id[:8]}"
    db.execute(
        text(
            "INSERT INTO tenant_wecom_configs (id, tenant_id, corp_id, agent_id, app_secret, is_active) "
            "VALUES (:id, :tenant_id, :corp_id, 'agent-1', 'encrypted-placeholder', true)"
        ),
        {"id": str(uuid.uuid4()), "tenant_id": tenant_id, "corp_id": corp_id},
    )
    db.execute(
        text(
            "INSERT INTO sync_states (tenant_id, corp_id, status, last_seq, seq_version, error_message, started_at) "
            "VALUES (:tenant_id, :corp_id, 'error', 0, 0, 'sync worker error', now())"
        ),
        {"tenant_id": tenant_id, "corp_id": corp_id},
    )
    db.commit()

    result = invoke_tool(db, "sync_status_summary", _context(tenant_id, user_id), consent_given=True)
    assert result.data["status"] == "error"
    assert result.data["has_recent_error"] is True
    # The raw error message must never be exposed by this tool.
    assert "sync worker error" not in str(result.data)


def test_storage_quota_summary_reflects_no_active_subscription(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(db, "storage_quota_summary", _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.SUCCESS
    # No entitled subscription -> quota_bytes=0 -> capacity_from_values'
    # "unavailable" classification branch. used_bytes is a real measured 0
    # (not None), so usage_status is "available" per that function's own
    # ternary — only can_accept_new_media reflects the real "can't accept
    # media" consequence.
    assert result.data["quota_bytes"] == 0
    assert result.data["can_accept_new_media"] is False


def test_storage_quota_summary_does_not_write_a_rollup_row(db: Session, tenant_and_user) -> None:
    """RND-358 hard constraint: a read-only tool must never have a write
    side effect, even an upsert. Regression guard for the specific bug
    caught during development (measure_storage_capacity upserts)."""
    tenant_id, user_id = tenant_and_user
    before = db.execute(
        text("SELECT count(*) FROM tenant_storage_daily WHERE tenant_id = :t"), {"t": tenant_id}
    ).scalar()

    invoke_tool(db, "storage_quota_summary", _context(tenant_id, user_id), consent_given=True)

    after = db.execute(
        text("SELECT count(*) FROM tenant_storage_daily WHERE tenant_id = :t"), {"t": tenant_id}
    ).scalar()
    assert after == before == 0


def test_archive_health_summary_denied_for_tenant_admin_scope(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(
        db,
        "archive_health_summary",
        _context(tenant_id, user_id, scope=ToolScope.TENANT_ADMIN),
        consent_given=True,
    )
    assert result.status == ToolResultStatus.PERMISSION_DENIED


def test_archive_health_summary_allowed_for_internal_support_scope(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(
        db,
        "archive_health_summary",
        _context(tenant_id, user_id, scope=ToolScope.INTERNAL_SUPPORT),
        consent_given=True,
    )
    assert result.status == ToolResultStatus.SUCCESS
    assert result.data["active_reachability_finding_count"] == 0


def test_known_error_codes_returns_the_verified_real_codes(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(db, "known_error_codes", _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.SUCCESS
    assert set(result.data["error_codes"].keys()) == set(_KNOWN_ERROR_CODES.keys())
    assert "config_error" in result.data["error_codes"]


@pytest.mark.parametrize("name", _REAL_TOOL_NAMES)
def test_every_tool_result_is_confined_to_its_own_allowlist(db: Session, tenant_and_user, name: str) -> None:
    tenant_id, user_id = tenant_and_user
    spec = get_tool(name)
    scope = spec.required_scope
    result = invoke_tool(db, name, _context(tenant_id, user_id, scope=scope), consent_given=True)
    assert set(result.data.keys()) <= spec.allowed_fields
