"""RND-316 (C2-2) export security approval gate.

Provides approval-token issuance and validation. Raw tokens are returned only
at issuance; storage contains SHA-256 digests exclusively.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.db.models import ExportApprovalToken

DEFAULT_APPROVAL_TTL_SECONDS = 300


class ExportNotApprovedError(Exception):
    """Raised when an export approval token does not satisfy the gate."""


def _params_hash(params: Mapping[str, Any]) -> str:
    """Hash canonical JSON parameters without retaining their contents."""
    canonical = json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def issue_export_approval(
    *,
    db: Session,
    admin_user_id: str,
    tenant_id: str,
    params: Mapping[str, Any],
    ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
) -> tuple[str, datetime]:
    """Issue a short-lived, single-use approval bound to ``params``."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    params_hash = _params_hash(params)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    row = ExportApprovalToken(
        id=str(uuid.uuid4()),
        admin_user_id=admin_user_id,
        tenant_id=tenant_id,
        token=token_hash,
        params_hash=params_hash,
        expires_at=expires_at,
        used=False,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        tenant_id=tenant_id,
        action=AuditAction.EXPORT_APPROVAL_GRANTED,
        object_type=AuditObjectType.EXPORT_APPROVAL_TOKEN,
        admin_user_id=admin_user_id,
        object_id=row.id,
        detail={"params_hash": params_hash, "expires_at": expires_at.isoformat()},
    )
    return raw_token, expires_at


def require_export_approval(
    *,
    db: Session,
    token: Optional[str],
    params: Mapping[str, Any],
    admin_user_id: str,
    tenant_id: str,
) -> None:
    """Consume a valid approval token or raise ``ExportNotApprovedError``."""
    if not token:
        raise ExportNotApprovedError("missing approval token")

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = (
        db.query(ExportApprovalToken)
        .filter(ExportApprovalToken.token == token_hash)
        .first()
    )
    if row is None or row.tenant_id != tenant_id or row.admin_user_id != admin_user_id:
        raise ExportNotApprovedError("invalid approval token")
    if row.used:
        raise ExportNotApprovedError("approval token already consumed")

    expires_at = row.expires_at
    # SQLite returns naive values for timezone-aware columns in offline tests.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise ExportNotApprovedError("approval token expired")
    if row.params_hash != _params_hash(params):
        raise ExportNotApprovedError("approval token params mismatch")

    row.used = True
    write_audit(
        db,
        tenant_id=tenant_id,
        action=AuditAction.EXPORT_APPROVAL_CONSUMED,
        object_type=AuditObjectType.EXPORT_APPROVAL_TOKEN,
        admin_user_id=admin_user_id,
        object_id=row.id,
        detail={"params_hash": row.params_hash},
    )
