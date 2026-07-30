"""
Audit-log write hook (RND-294 / A7-2).

Provides `write_audit(...)` for recording admin actions into the immutable,
append-only `audit_logs` table created by RND-293 (A7-1). See that ticket for
the schema and the immutable contract.

Design rules
------------
- FAIL-SAFE: any failure while recording an audit row is isolated inside a
  SAVEPOINT and swallowed, so a broken audit sink can NEVER break the primary
  request or poison its transaction.
- `write_audit` adds + flushes the row but does NOT commit; the calling
  route's existing `db.commit()` persists it atomically with the action.
- `detail` holds ONLY structured, non-sensitive context. Never pass message
  bodies or decrypted payloads (SF-1 data minimization).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from app.db.models import AuditLog

logger = logging.getLogger(__name__)


class AuditAction:
    """Canonical action verbs for the deliberately Text-backed columns."""

    LOGIN = "auth.login"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    PASSWORD_RESET_REQUESTED = "auth.password_reset_requested"
    PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"
    USER_INVITED = "user.invited"
    USER_DISABLED = "user.disabled"
    USER_ENABLED = "user.enabled"
    CONFIG_VIEWED = "config.viewed"
    CONFIG_CHANGED = "config.changed"
    EXPORT_APPROVAL_GRANTED = "export.approval_granted"
    EXPORT_APPROVAL_CONSUMED = "export.approval_consumed"
    EXPORT_APPROVAL_DENIED = "export.approval_denied"
    EXPORT = "export.executed"


class AuditObjectType:
    USER = "admin_user"
    SESSION = "admin_session"
    TENANT_CONFIG = "tenant_config"
    PASSWORD_RESET_TOKEN = "password_reset_token"
    EXPORT_APPROVAL_TOKEN = "export_approval_token"
    EXPORT = "export"


def write_audit(
    db: Session,
    *,
    tenant_id: str,
    action: str,
    object_type: str,
    admin_user_id: Optional[str] = None,
    object_id: Optional[str] = None,
    detail: Optional[Mapping[str, Any]] = None,
) -> None:
    """Append an immutable audit row. FAIL-SAFE: never raises."""
    try:
        with db.begin_nested():
            db.add(
                AuditLog(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    admin_user_id=admin_user_id,
                    action=action,
                    object_type=object_type,
                    object_id=object_id,
                    detail=dict(detail) if detail else None,
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.flush()
    except Exception:
        # Isolated by the savepoint above — outer transaction is untouched.
        logger.exception("write_audit: failed to record audit row (action=%s)", action)


def record_export_audit(
    db: Session,
    *,
    tenant_id: str,
    admin_user_id: Optional[str],
    export_format: str,
    record_count: Optional[int] = None,
    scope: Optional[Mapping[str, Any]] = None,
    approval_ref: Optional[str] = None,
    gate_enforced: Optional[bool] = None,
) -> None:
    """Record one export event with non-sensitive, hash-only scope context.

    The underlying ``write_audit`` call is fail-safe and does not commit; the
    caller commits it atomically with its own export operation. Message bodies
    and decrypted payloads must never be passed here.
    """
    params_hash = None
    if scope is not None:
        canonical = json.dumps(
            {"format": export_format, "scope": scope},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        params_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    write_audit(
        db,
        tenant_id=tenant_id,
        action=AuditAction.EXPORT,
        object_type=AuditObjectType.EXPORT,
        admin_user_id=admin_user_id,
        detail={
            "format": export_format,
            "record_count": record_count,
            "params_hash": params_hash,
            "approval_ref": approval_ref,
            "gate_enforced": gate_enforced,
        },
    )
