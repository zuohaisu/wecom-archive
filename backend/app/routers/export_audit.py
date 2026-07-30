"""RND-317 (C2-3) export-audit recording endpoint.

This endpoint records an export event for independent hook verification. It
never generates an export file; C2-1 owns that responsibility.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audit import record_export_audit
from app.auth import get_current_user
from app.db.models import AdminUser, ArchiveMessage
from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/export", tags=["export-audit"])


class ExportRecordScope(BaseModel):
    """Metadata-only filters defining the records covered by an export."""

    conversation_ids: Optional[list[str]] = None
    date_from: Optional[int] = None
    date_to: Optional[int] = None
    msgtype: Optional[str] = None


class ExportRecordRequest(BaseModel):
    format: str = "csv"
    scope: ExportRecordScope = Field(default_factory=ExportRecordScope)
    approval_token: Optional[str] = None


def _approval_params(req: ExportRecordRequest) -> dict:
    """Return the canonical parameters shared by approval and audit hashes."""
    return {"format": req.format, "scope": req.scope.model_dump(exclude_none=True)}


def _count_records(db: Session, tenant_id: str, scope: ExportRecordScope) -> int:
    """Count export records using metadata predicates only (SF-1)."""
    query = db.query(func.count(ArchiveMessage.id)).filter(
        ArchiveMessage.tenant_id == tenant_id
    )
    if scope.conversation_ids:
        query = query.filter(ArchiveMessage.roomid.in_(scope.conversation_ids))
    if scope.date_from is not None:
        query = query.filter(ArchiveMessage.msgtime >= scope.date_from)
    if scope.date_to is not None:
        query = query.filter(ArchiveMessage.msgtime <= scope.date_to)
    if scope.msgtype:
        query = query.filter(ArchiveMessage.msgtype == scope.msgtype)
    return int(query.scalar() or 0)


@router.post("/record")
def record_export(
    req: ExportRecordRequest,
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record an export execution after consuming an approval when available."""
    user, tenant_id = auth
    params = _approval_params(req)
    scope = params["scope"]
    gate_enforced = False
    approval_ref = None

    # RND-316 remains an optional deployment dependency so this hook can be
    # verified independently before the approval-gate release is deployed.
    try:
        from app.export_approval import ExportNotApprovedError, require_export_approval
    except ImportError:
        logger.warning(
            "RND-317: app.export_approval (RND-316) not deployed; "
            "recording export with gate_enforced=False"
        )
    else:
        if req.approval_token:
            try:
                require_export_approval(
                    db=db,
                    token=req.approval_token,
                    params=params,
                    admin_user_id=user.id,
                    tenant_id=tenant_id,
                )
            except ExportNotApprovedError as error:
                raise HTTPException(403, f"export approval rejected: {error}")
            gate_enforced = True
            approval_ref = hashlib.sha256(
                req.approval_token.encode("utf-8")
            ).hexdigest()
        else:
            logger.warning(
                "RND-317: export recorded without approval token although "
                "the RND-316 gate is deployed"
            )

    record_count = _count_records(db, tenant_id, req.scope)
    record_export_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        export_format=req.format,
        record_count=record_count,
        scope=scope,
        approval_ref=approval_ref,
        gate_enforced=gate_enforced,
    )
    db.commit()
    return {
        "recorded": True,
        "format": req.format,
        "record_count": record_count,
        "gate_enforced": gate_enforced,
    }
