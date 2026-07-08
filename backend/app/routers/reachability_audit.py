"""
Admin diagnostic API for the Message Reachability Audit (RND-178).

Read-only, admin-authenticated, tenant-scoped. Returns aggregate statistics
by default; per-message samples are opt-in and always capped. See
app.reachability_audit for the classification logic and the full field
allow-list (no message content, raw payloads, or WeCom/media identifiers).

This endpoint is a diagnostic tool, not part of the conversation review
console's normal browsing surface — RND-180 is expected to build a UI on
top of it, not this router.
"""

from __future__ import annotations

from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db.models import AdminUser
from app.db.session import get_db
from app.reachability_audit import (
    DEFAULT_SAMPLE_LIMIT,
    DEFAULT_SCAN_LIMIT,
    MAX_SAMPLE_LIMIT,
    MAX_SCAN_LIMIT,
    build_message_reachability_report,
)

router = APIRouter()


class ReachabilitySampleOut(BaseModel):
    message_db_id: int
    reachability_status: str
    reason_code: str
    message_type: Optional[str] = None
    conversation_type: Optional[str] = None
    has_sender: bool
    has_room: bool
    recipient_count: int
    timeline_visible: bool
    msg_time: Optional[int] = None
    created_at: Optional[str] = None


class ReachabilityAuditOut(BaseModel):
    scanned_count: int
    matching_total: int
    limit: int
    offset: int
    has_more: bool
    max_scan_limit: int
    reachable_count: int
    unreachable_count: int
    counts_by_status: dict[str, int]
    counts_by_message_type: dict[str, int]
    counts_by_conversation_type: dict[str, int]
    samples: Optional[list[ReachabilitySampleOut]] = None


@router.get("/api/admin/reachability-audit", response_model=ReachabilityAuditOut)
def get_reachability_audit(
    conversation_id: Optional[str] = Query(None),
    message_type: Optional[str] = Query(None),
    msgtime_from: Optional[int] = Query(None, description="Inclusive lower bound, epoch ms"),
    msgtime_to: Optional[int] = Query(None, description="Inclusive upper bound, epoch ms"),
    limit: int = Query(DEFAULT_SCAN_LIMIT, ge=1, le=MAX_SCAN_LIMIT),
    offset: int = Query(0, ge=0),
    include_samples: bool = Query(False),
    sample_limit: int = Query(DEFAULT_SAMPLE_LIMIT, ge=1, le=MAX_SAMPLE_LIMIT),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Audit reachability for this tenant's successfully-archived messages.

    tenant_id is taken only from the authenticated session, never from a
    request param — matching every other route in this codebase.
    """
    _, tenant_id = auth
    return build_message_reachability_report(
        db,
        tenant_id,
        conversation_id=conversation_id,
        message_type=message_type,
        msgtime_from=msgtime_from,
        msgtime_to=msgtime_to,
        limit=limit,
        offset=offset,
        include_samples=include_samples,
        sample_limit=sample_limit,
    )
