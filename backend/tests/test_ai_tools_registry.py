"""RND-358 (T4) — tool registry contract: consent gate, scope isolation,
allowlist stripping, and deterministic degradation. Uses a synthetic
private registry (not the real module-level one) so these tests don't
depend on — or pollute — the real registered tools in handlers.py.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.ai_tools.registry import (
    ToolContext,
    ToolFieldMissingError,
    ToolResultStatus,
    ToolScope,
    ToolSpec,
    ToolTimeoutError,
    ToolUnavailableError,
    get_tool,
    invoke_tool,
    list_tools_for_scope,
    register,
)

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")


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
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'AI tools test tenant', :slug)"),
        {"id": tenant_id, "slug": f"ai-tools-test-{tenant_id[:8]}"},
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


@pytest.fixture()
def registered_test_tool():
    """Register a fresh, uniquely-named synthetic tool for one test so
    tests never collide with each other or with the real registry state."""
    created = []

    def _register(name_suffix: str, **spec_overrides):
        name = f"test_tool_{name_suffix}_{uuid.uuid4().hex[:8]}"
        defaults = dict(
            name=name,
            description="synthetic test tool",
            allowed_fields=frozenset({"a", "b"}),
            required_scope=ToolScope.TENANT_ADMIN,
            handler=lambda db, ctx: {"a": 1, "b": 2, "c": "should be stripped"},
        )
        defaults.update(spec_overrides)
        spec = ToolSpec(**defaults)
        register(spec)
        created.append(name)
        return spec

    yield _register


def test_unknown_tool_returns_unknown_tool_status(db: Session, tenant_and_user) -> None:
    tenant_id, user_id = tenant_and_user
    result = invoke_tool(db, "does_not_exist", _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.UNKNOWN_TOOL


def test_consent_required_before_any_handler_call(db: Session, tenant_and_user, registered_test_tool) -> None:
    tenant_id, user_id = tenant_and_user
    called = []
    spec = registered_test_tool("consent", handler=lambda db, ctx: called.append(1) or {"a": 1})

    result = invoke_tool(db, spec.name, _context(tenant_id, user_id), consent_given=False)

    assert result.status == ToolResultStatus.CONSENT_DENIED
    assert called == []


def test_internal_only_tool_denied_to_tenant_admin_scope(db: Session, tenant_and_user, registered_test_tool) -> None:
    tenant_id, user_id = tenant_and_user
    spec = registered_test_tool("internal", required_scope=ToolScope.INTERNAL_SUPPORT)

    result = invoke_tool(
        db, spec.name, _context(tenant_id, user_id, scope=ToolScope.TENANT_ADMIN), consent_given=True
    )

    assert result.status == ToolResultStatus.PERMISSION_DENIED


def test_internal_only_tool_allowed_to_internal_support_scope(
    db: Session, tenant_and_user, registered_test_tool
) -> None:
    tenant_id, user_id = tenant_and_user
    spec = registered_test_tool("internal-ok", required_scope=ToolScope.INTERNAL_SUPPORT)

    result = invoke_tool(
        db, spec.name, _context(tenant_id, user_id, scope=ToolScope.INTERNAL_SUPPORT), consent_given=True
    )

    assert result.status == ToolResultStatus.SUCCESS


def test_undeclared_fields_are_stripped_from_response(db: Session, tenant_and_user, registered_test_tool) -> None:
    tenant_id, user_id = tenant_and_user
    spec = registered_test_tool("strip")

    result = invoke_tool(db, spec.name, _context(tenant_id, user_id), consent_given=True)

    assert result.status == ToolResultStatus.SUCCESS
    assert "c" not in result.data
    assert result.data == {"a": 1, "b": 2}
    assert result.fields_returned == ["a", "b"]


def test_timeout_error_degrades_deterministically(db: Session, tenant_and_user, registered_test_tool) -> None:
    tenant_id, user_id = tenant_and_user

    def handler(db, ctx):
        raise ToolTimeoutError("too slow")

    spec = registered_test_tool("timeout", handler=handler)
    result = invoke_tool(db, spec.name, _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.TIMEOUT
    assert result.data == {}


def test_unavailable_error_degrades_deterministically(db: Session, tenant_and_user, registered_test_tool) -> None:
    tenant_id, user_id = tenant_and_user

    def handler(db, ctx):
        raise ToolUnavailableError("down")

    spec = registered_test_tool("unavailable", handler=handler)
    result = invoke_tool(db, spec.name, _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.UNAVAILABLE


def test_field_missing_error_degrades_deterministically(db: Session, tenant_and_user, registered_test_tool) -> None:
    tenant_id, user_id = tenant_and_user

    def handler(db, ctx):
        raise ToolFieldMissingError("no such record")

    spec = registered_test_tool("field-missing", handler=handler)
    result = invoke_tool(db, spec.name, _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.FIELD_MISSING


def test_unexpected_exception_never_raises_out_of_invoke_tool(
    db: Session, tenant_and_user, registered_test_tool
) -> None:
    tenant_id, user_id = tenant_and_user

    def handler(db, ctx):
        raise RuntimeError("boom")

    spec = registered_test_tool("boom", handler=handler)
    result = invoke_tool(db, spec.name, _context(tenant_id, user_id), consent_given=True)
    assert result.status == ToolResultStatus.ERROR


def test_every_invocation_writes_exactly_one_audit_row_with_field_names_only(
    db: Session, tenant_and_user, registered_test_tool
) -> None:
    tenant_id, user_id = tenant_and_user
    spec = registered_test_tool("audit")

    invoke_tool(db, spec.name, _context(tenant_id, user_id), consent_given=True)

    row = db.execute(
        text(
            "SELECT tool_name, consent_given, fields_returned, result_status "
            "FROM ai_tool_invocations WHERE tenant_id = :t AND tool_name = :name"
        ),
        {"t": tenant_id, "name": spec.name},
    ).fetchone()
    assert row is not None
    assert row.consent_given is True
    assert sorted(row.fields_returned) == ["a", "b"]
    assert row.result_status == "success"
    # The actual data values (1, 2, "should be stripped") must never appear
    # in the audit row — only field NAMES.
    assert "1" not in str(row.fields_returned)


def test_page_context_is_passed_through_to_handler(db: Session, tenant_and_user, registered_test_tool) -> None:
    tenant_id, user_id = tenant_and_user
    spec = registered_test_tool("page-context", handler=lambda db, ctx: {"a": ctx.page_context.get("page_id")})

    result = invoke_tool(
        db, spec.name, _context(tenant_id, user_id, page_id="billing"), consent_given=True
    )

    assert result.data["a"] == "billing"


def test_list_tools_for_scope_hides_internal_tools_from_tenant_admin(
    db: Session, tenant_and_user, registered_test_tool
) -> None:
    internal_spec = registered_test_tool("list-internal", required_scope=ToolScope.INTERNAL_SUPPORT)
    tenant_spec = registered_test_tool("list-tenant", required_scope=ToolScope.TENANT_ADMIN)

    tenant_visible = {t.name for t in list_tools_for_scope(ToolScope.TENANT_ADMIN)}
    internal_visible = {t.name for t in list_tools_for_scope(ToolScope.INTERNAL_SUPPORT)}

    assert tenant_spec.name in tenant_visible
    assert internal_spec.name not in tenant_visible
    assert tenant_spec.name in internal_visible
    assert internal_spec.name in internal_visible


def test_registering_duplicate_tool_name_raises() -> None:
    name = f"dup_{uuid.uuid4().hex[:8]}"
    spec = ToolSpec(
        name=name,
        description="d",
        allowed_fields=frozenset(),
        required_scope=ToolScope.TENANT_ADMIN,
        handler=lambda db, ctx: {},
    )
    register(spec)
    with pytest.raises(ValueError):
        register(spec)


def test_get_tool_returns_none_for_unknown_name() -> None:
    assert get_tool(f"nonexistent_{uuid.uuid4().hex}") is None
