"""Immutable security-activity audit helpers and action catalogue."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.db.models import AuditLog

logger = logging.getLogger(__name__)


class AuditCategory:
    SECURITY = "security"
    ACCOUNT = "account"
    CONFIGURATION = "configuration"
    DATA_ACCESS = "data_access"
    SYSTEM = "system"


class AuditAction:
    """Canonical action verbs. Production writers must use these constants."""

    LOGIN = "auth.login"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    PASSWORD_RESET_REQUESTED = "auth.password_reset_requested"
    PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"
    PASSWORD_CHANGED = "auth.password_changed"
    USER_INVITED = "user.invited"
    USER_INVITE_ACCEPTED = "user.invite_accepted"
    USER_ENABLED = "user.enabled"
    USER_DISABLED = "user.disabled"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_ACCESS_REQUESTED = "user.access_requested"
    USER_ACCESS_REQUEST_LINKED = "user.access_request_linked"
    USER_ACCESS_REQUEST_ACCOUNT_CREATED = "user.access_request_account_created"
    USER_PASSWORD_RESET_INITIATED = "user.password_reset_initiated"
    CONFIG_CHANGED = "config.changed"
    RETENTION_CONFIG_CHANGED = "retention.config_changed"
    RETENTION_CONFIG_LOCKED = "retention.config_locked"
    EXPORT_APPROVAL_GRANTED = "export.approval_granted"
    EXPORT_APPROVAL_DENIED = "export.approval_denied"
    EXPORT_APPROVAL_CONSUMED = "export.approval_consumed"
    EXPORT = "export.executed"
    MEDIA_DOWNLOAD = "media.download"
    PLATFORM_TENANT_ACCESSED = "platform.tenant_accessed"
    PLATFORM_TENANT_ACTIVATED = "platform.tenant_activated"
    PLATFORM_TENANT_DEACTIVATED = "platform.tenant_deactivated"
    ORGANIZATION_PROVISIONED = "organization.provisioned"
    SUBSCRIPTION_ACTIVATED = "subscription.activated"
    SUBSCRIPTION_RENEWED = "subscription.renewed"
    DECRYPT_COMPLETED = "decrypt.completed"
    RETENTION_MESSAGES_LOCKED = "retention.messages_locked"


class AuditObjectType:
    USER = "admin_user"
    SESSION = "admin_session"
    TENANT_CONFIG = "tenant_config"
    PASSWORD_RESET_TOKEN = "password_reset_token"
    EXPORT_APPROVAL_TOKEN = "export_approval_token"
    EXPORT = "export"
    MEDIA_FILE = "media_file"
    TENANT = "tenant"
    RETENTION_CONFIG = "retention_config"
    KEY_VERSION = "key_version"
    ACCESS_REQUEST = "access_request"
    SUBSCRIPTION = "subscription"


# The catalogue is intentionally application-level: category is computed for
# both old rows and new writes, so no schema change is necessary. Unknown old
# actions fail closed into ``system`` rather than being presented as a human
# security event.
ACTION_CATALOG: dict[str, tuple[str, str]] = {
    AuditAction.LOGIN: (AuditCategory.SECURITY, AuditObjectType.USER),
    AuditAction.LOGIN_FAILED: (AuditCategory.SECURITY, AuditObjectType.SESSION),
    AuditAction.LOGOUT: (AuditCategory.SECURITY, AuditObjectType.SESSION),
    AuditAction.PASSWORD_RESET_REQUESTED: (AuditCategory.SECURITY, AuditObjectType.USER),
    AuditAction.PASSWORD_RESET_COMPLETED: (AuditCategory.SECURITY, AuditObjectType.USER),
    AuditAction.PASSWORD_CHANGED: (AuditCategory.SECURITY, AuditObjectType.USER),
    AuditAction.USER_INVITED: (AuditCategory.ACCOUNT, AuditObjectType.USER),
    AuditAction.USER_INVITE_ACCEPTED: (AuditCategory.ACCOUNT, AuditObjectType.USER),
    AuditAction.USER_ENABLED: (AuditCategory.ACCOUNT, AuditObjectType.USER),
    AuditAction.USER_DISABLED: (AuditCategory.ACCOUNT, AuditObjectType.USER),
    AuditAction.USER_ROLE_CHANGED: (AuditCategory.ACCOUNT, AuditObjectType.USER),
    AuditAction.USER_ACCESS_REQUESTED: (AuditCategory.ACCOUNT, AuditObjectType.ACCESS_REQUEST),
    AuditAction.USER_ACCESS_REQUEST_LINKED: (AuditCategory.ACCOUNT, AuditObjectType.ACCESS_REQUEST),
    AuditAction.USER_ACCESS_REQUEST_ACCOUNT_CREATED: (AuditCategory.ACCOUNT, AuditObjectType.ACCESS_REQUEST),
    AuditAction.USER_PASSWORD_RESET_INITIATED: (AuditCategory.ACCOUNT, AuditObjectType.USER),
    AuditAction.CONFIG_CHANGED: (AuditCategory.CONFIGURATION, AuditObjectType.TENANT_CONFIG),
    AuditAction.RETENTION_CONFIG_CHANGED: (AuditCategory.CONFIGURATION, AuditObjectType.RETENTION_CONFIG),
    AuditAction.RETENTION_CONFIG_LOCKED: (AuditCategory.CONFIGURATION, AuditObjectType.RETENTION_CONFIG),
    AuditAction.EXPORT_APPROVAL_GRANTED: (AuditCategory.DATA_ACCESS, AuditObjectType.EXPORT_APPROVAL_TOKEN),
    AuditAction.EXPORT_APPROVAL_DENIED: (AuditCategory.DATA_ACCESS, AuditObjectType.EXPORT_APPROVAL_TOKEN),
    AuditAction.EXPORT_APPROVAL_CONSUMED: (AuditCategory.DATA_ACCESS, AuditObjectType.EXPORT_APPROVAL_TOKEN),
    AuditAction.EXPORT: (AuditCategory.DATA_ACCESS, AuditObjectType.EXPORT),
    AuditAction.MEDIA_DOWNLOAD: (AuditCategory.DATA_ACCESS, AuditObjectType.MEDIA_FILE),
    AuditAction.PLATFORM_TENANT_ACCESSED: (AuditCategory.SECURITY, AuditObjectType.TENANT),
    AuditAction.PLATFORM_TENANT_ACTIVATED: (AuditCategory.SECURITY, AuditObjectType.TENANT),
    AuditAction.PLATFORM_TENANT_DEACTIVATED: (AuditCategory.SECURITY, AuditObjectType.TENANT),
    AuditAction.ORGANIZATION_PROVISIONED: (AuditCategory.ACCOUNT, AuditObjectType.TENANT),
    AuditAction.SUBSCRIPTION_ACTIVATED: (AuditCategory.ACCOUNT, AuditObjectType.SUBSCRIPTION),
    AuditAction.SUBSCRIPTION_RENEWED: (AuditCategory.ACCOUNT, AuditObjectType.SUBSCRIPTION),
    AuditAction.DECRYPT_COMPLETED: (AuditCategory.SYSTEM, AuditObjectType.KEY_VERSION),
    AuditAction.RETENTION_MESSAGES_LOCKED: (AuditCategory.SYSTEM, AuditObjectType.TENANT),
}
AUDIT_CATEGORIES = frozenset(
    {
        AuditCategory.SECURITY,
        AuditCategory.ACCOUNT,
        AuditCategory.CONFIGURATION,
        AuditCategory.DATA_ACCESS,
        AuditCategory.SYSTEM,
    }
)


def audit_category(action: str) -> str:
    """Return the authoritative category; unknown historical actions are system."""
    return ACTION_CATALOG.get(action, (AuditCategory.SYSTEM, ""))[0]


def actions_for_categories(categories: set[str]) -> set[str]:
    """Return known actions in categories; unknown history is handled separately."""
    return {action for action, (category, _object_type) in ACTION_CATALOG.items() if category in categories}


def audit_category_expression():
    """SQL expression equivalent to :func:`audit_category` for API filtering."""
    return case(
        {action: category for action, (category, _object_type) in ACTION_CATALOG.items()},
        value=AuditLog.action,
        else_=AuditCategory.SYSTEM,
    )


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
    """Append an immutable audit row. FAIL-SAFE: never raises or commits."""
    try:
        with db.begin_nested():
            db.add(
                AuditLog(
                    id=str(uuid.uuid4()), tenant_id=tenant_id, admin_user_id=admin_user_id,
                    action=action, object_type=object_type, object_id=object_id,
                    detail=dict(detail) if detail else None, created_at=datetime.now(timezone.utc),
                )
            )
            db.flush()
    except Exception:
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
    """Record one export event with hash-only scope context, never committing."""
    params_hash = None
    if scope is not None:
        canonical = json.dumps({"format": export_format, "scope": scope}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        params_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    write_audit(
        db, tenant_id=tenant_id, action=AuditAction.EXPORT,
        object_type=AuditObjectType.EXPORT, admin_user_id=admin_user_id,
        detail={"format": export_format, "record_count": record_count, "params_hash": params_hash,
                "approval_ref": approval_ref, "gate_enforced": gate_enforced},
    )
