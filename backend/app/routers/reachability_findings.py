"""Tenant-scoped, read-only agent diagnostics for reachability findings."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db.models import AdminUser, ReachabilityFinding
from app.db.session import get_db
from app.schemas.reachability_findings import (
    ALLOWED_FINDING_REASONS,
    ReachabilityFindingListOut,
    ReachabilityFindingOut,
    ReachabilityFindingSummaryOut,
)

router = APIRouter()
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100
# A per-process secret keeps cursor anchors confidential without adding a
# deployment credential. Restarted processes fail old cursors closed.
_CURSOR_CIPHER = Fernet(Fernet.generate_key())


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_time_window") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _filter_fingerprint(tenant_id: str, status: str, reason: Optional[str], from_at: Optional[datetime], to_at: Optional[datetime]) -> str:
    # Never return this value: it binds a decrypted cursor to both tenant and
    # filters without placing either plaintext value in the token.
    value = "|".join((tenant_id, status, reason or "", str(from_at or ""), str(to_at or "")))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode_cursor(last_seen: datetime, public_id: str, fingerprint: str) -> str:
    payload = json.dumps(
        {"seen": last_seen.isoformat(), "public": public_id, "filter": fingerprint},
        separators=(",", ":"),
    ).encode("utf-8")
    return _CURSOR_CIPHER.encrypt(payload).decode("ascii")


def _decode_cursor(cursor: str, fingerprint: str) -> tuple[datetime, str]:
    try:
        payload = json.loads(_CURSOR_CIPHER.decrypt(cursor.encode("ascii")).decode("utf-8"))
        if set(payload) != {"seen", "public", "filter"} or payload["filter"] != fingerprint:
            raise ValueError
        seen = _parse_time(payload["seen"])
        if seen is None or not isinstance(payload["public"], str):
            raise ValueError
        return seen, payload["public"]
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid_cursor") from exc


def _out(row: ReachabilityFinding) -> ReachabilityFindingOut:
    return ReachabilityFindingOut(
        public_finding_id=row.public_id,
        reason=row.reason_code,
        status=row.status,
        first_seen_at=row.first_seen,
        last_seen_at=row.last_seen,
        resolved_at=row.resolved_at,
        occurrence_count=row.occurrence_count,
        algorithm_version=row.algorithm_version,
    )


@router.get("/api/admin/reachability-findings", response_model=ReachabilityFindingListOut)
def list_reachability_findings(
    request: Request,
    status: str = Query("active"),
    reason: Optional[str] = Query(None),
    from_at: Optional[str] = Query(None, alias="from"),
    to_at: Optional[str] = Query(None, alias="to"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    cursor: Optional[str] = Query(None),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReachabilityFindingListOut:
    if "tenant_id" in request.query_params:
        raise HTTPException(status_code=422, detail="tenant_scope_is_session_derived")
    if status not in {"active", "resolved"}:
        raise HTTPException(status_code=422, detail="invalid_status")
    if reason is not None and reason not in ALLOWED_FINDING_REASONS:
        raise HTTPException(status_code=422, detail="invalid_reason")
    parsed_from, parsed_to = _parse_time(from_at), _parse_time(to_at)
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(status_code=422, detail="invalid_time_window")
    _, tenant_id = auth
    fingerprint = _filter_fingerprint(tenant_id, status, reason, parsed_from, parsed_to)
    query = db.query(ReachabilityFinding).filter(
        ReachabilityFinding.tenant_id == tenant_id,
        ReachabilityFinding.status == status,
    )
    if reason:
        query = query.filter(ReachabilityFinding.reason_code == reason)
    if parsed_from:
        query = query.filter(ReachabilityFinding.last_seen >= parsed_from)
    if parsed_to:
        query = query.filter(ReachabilityFinding.last_seen <= parsed_to)
    total = query.count()
    if cursor:
        cursor_seen, cursor_public_id = _decode_cursor(cursor, fingerprint)
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            # SQLite stores timezone-bearing fixture timestamps as text while
            # its datetime adapter binds comparison values without the offset.
            # Compare canonical timestamp strings only in this test dialect.
            seen_column = func.strftime(
                "%Y-%m-%dT%H:%M:%f", ReachabilityFinding.last_seen
            )
            seen_value = cursor_seen.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        else:
            seen_column = ReachabilityFinding.last_seen
            seen_value = cursor_seen
        query = query.filter(
            or_(
                seen_column < seen_value,
                and_(seen_column == seen_value, ReachabilityFinding.public_id < cursor_public_id),
            )
        )
    rows = query.order_by(
        ReachabilityFinding.last_seen.desc(), ReachabilityFinding.public_id.desc()
    ).limit(limit + 1).all()
    page, extra = rows[:limit], len(rows) > limit
    next_cursor = (
        _encode_cursor(page[-1].last_seen, page[-1].public_id, fingerprint)
        if extra and page else None
    )
    # Aggregate only; all queries remain tenant-scoped even though the API
    # never accepts a tenant selector.
    active_count = (
        db.query(ReachabilityFinding.id)
        .filter(ReachabilityFinding.tenant_id == tenant_id, ReachabilityFinding.status == "active")
        .count()
    )
    resolved_count = (
        db.query(ReachabilityFinding.id)
        .filter(ReachabilityFinding.tenant_id == tenant_id, ReachabilityFinding.status == "resolved")
        .count()
    )
    return ReachabilityFindingListOut(
        items=[_out(row) for row in page],
        next_cursor=next_cursor,
        summary=ReachabilityFindingSummaryOut(total=total, active=active_count, resolved=resolved_count),
    )
