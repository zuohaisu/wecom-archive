"""Tool registration, invocation, and audit (RND-358 / T4).

A tool is only reachable through invoke_tool(): there is no other path
from the AI answer/diagnostic flow to a handler function, so "the model
can only call registered read-only tools" is a structural property, not a
convention someone has to remember. Every call — including denied and
failed ones — writes exactly one AiToolInvocation audit row recording the
FIELD NAMES returned, never values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.db.models import AiToolInvocation

logger = logging.getLogger(__name__)


class ToolScope(str, Enum):
    """Who may see/invoke a tool. INTERNAL_SUPPORT is a strict superset —
    an internal support account can invoke everything a tenant admin can,
    plus internal-only tools; a tenant admin can never reach an
    internal-only tool, regardless of consent."""

    TENANT_ADMIN = "tenant_admin"
    INTERNAL_SUPPORT = "internal_support"


class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    CONSENT_DENIED = "consent_denied"
    PERMISSION_DENIED = "permission_denied"
    UNKNOWN_TOOL = "unknown_tool"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    FIELD_MISSING = "field_missing"
    ERROR = "error"


class ToolTimeoutError(Exception):
    """Raise from a handler when the underlying read did not complete in
    time. Never let a raw timeout exception (e.g. from an HTTP client)
    escape a handler uncaught — wrap it into this."""


class ToolUnavailableError(Exception):
    """Raise from a handler when the underlying service/table is reachable
    but cannot currently answer (e.g. a dependent service is down)."""


class ToolFieldMissingError(Exception):
    """Raise from a handler when a field this tool is supposed to report
    has no value to report for reasons other than availability (e.g. an
    expected upstream record does not exist)."""


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    admin_user_id: str
    scope: ToolScope
    page_context: dict = field(default_factory=dict)  # user-supplied, allowlisted by the handler that reads it


@dataclass(frozen=True)
class ToolResult:
    status: ToolResultStatus
    data: dict
    fields_returned: list[str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    allowed_fields: frozenset
    required_scope: ToolScope
    handler: Callable[[Session, ToolContext], dict]


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    if spec.name in _REGISTRY:
        raise ValueError(f"tool already registered: {spec.name}")
    _REGISTRY[spec.name] = spec


def get_tool(name: str) -> Optional[ToolSpec]:
    return _REGISTRY.get(name)


def list_tools_for_scope(scope: ToolScope) -> list[ToolSpec]:
    if scope == ToolScope.INTERNAL_SUPPORT:
        return sorted(_REGISTRY.values(), key=lambda t: t.name)
    return sorted(
        (t for t in _REGISTRY.values() if t.required_scope == ToolScope.TENANT_ADMIN),
        key=lambda t: t.name,
    )


def _write_invocation(
    db: Session,
    context: ToolContext,
    tool_name: str,
    consent_given: bool,
    result: ToolResult,
) -> None:
    db.add(
        AiToolInvocation(
            tenant_id=context.tenant_id,
            admin_user_id=context.admin_user_id,
            tool_name=tool_name,
            consent_given=consent_given,
            fields_returned=result.fields_returned,
            result_status=result.status.value,
        )
    )
    db.commit()


def invoke_tool(db: Session, name: str, context: ToolContext, *, consent_given: bool) -> ToolResult:
    """The single call site every AI-facing surface (answer_service tool
    step, T3's consent-gated UI action) must go through. consent_given is
    a required, explicit argument — there is no default that lets a caller
    accidentally skip the user-facing consent check."""
    spec = get_tool(name)
    if spec is None:
        result = ToolResult(ToolResultStatus.UNKNOWN_TOOL, {}, [])
        _write_invocation(db, context, name, consent_given, result)
        return result

    if not consent_given:
        result = ToolResult(ToolResultStatus.CONSENT_DENIED, {}, [])
        _write_invocation(db, context, spec.name, consent_given, result)
        return result

    if spec.required_scope == ToolScope.INTERNAL_SUPPORT and context.scope != ToolScope.INTERNAL_SUPPORT:
        result = ToolResult(ToolResultStatus.PERMISSION_DENIED, {}, [])
        _write_invocation(db, context, spec.name, consent_given, result)
        return result

    try:
        raw = spec.handler(db, context)
    except ToolTimeoutError:
        result = ToolResult(ToolResultStatus.TIMEOUT, {}, [])
    except ToolUnavailableError:
        result = ToolResult(ToolResultStatus.UNAVAILABLE, {}, [])
    except ToolFieldMissingError:
        result = ToolResult(ToolResultStatus.FIELD_MISSING, {}, [])
    except Exception as exc:  # noqa: BLE001 - a handler bug must degrade, never 500 the AI flow
        logger.error("ai_tools handler %s failed: %s", spec.name, type(exc).__name__)
        result = ToolResult(ToolResultStatus.ERROR, {}, [])
    else:
        # Defense in depth: even if a handler accidentally returns an
        # undeclared field, it is stripped here before it can reach an
        # audit log or a model context.
        filtered = {k: v for k, v in raw.items() if k in spec.allowed_fields}
        result = ToolResult(ToolResultStatus.SUCCESS, filtered, sorted(filtered.keys()))

    _write_invocation(db, context, spec.name, consent_given, result)
    return result
