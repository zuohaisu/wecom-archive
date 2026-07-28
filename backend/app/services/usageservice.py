"""UsageService (RND-281 / A1-1) — tenant-scoped aggregation for the
dashboard / analytics / platform-console.

Every function takes an optional ``tenant_id``: when ``None`` the query is
run across ALL tenants (cross-tenant aggregation, reused by B1-3). No
routing, no schema change, no F0/auth code — pure read-only aggregation.

``msgtime`` on ArchiveMessage is epoch-MILLISECONDS (see
html_helpers.py / structured_message_parser.py); divide by 1000 before
converting to a timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Date, func, select
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, Contact, MediaFile, SyncState, Tenant


_SYNC_ERROR = "error"
_SYNC_SYNCING = "syncing"


def count_messages(db: Session, tenant_id: Optional[str] = None) -> int:
    """Total archived messages for the tenant (or all tenants)."""
    stmt = select(func.count(ArchiveMessage.id))
    if tenant_id is not None:
        stmt = stmt.where(ArchiveMessage.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


def sum_storage(db: Session, tenant_id: Optional[str] = None) -> int:
    """Total media bytes stored (sum of media_files.file_size, in bytes).

    Scoped to MEDIA bytes only. The planner's "+ DB 估算" (per-tenant DB
    size estimate) is deliberately OUT OF SCOPE for A1-1 — it needs a
    separate, potentially expensive approach and belongs in A1-2 if
    wanted. file_size is nullable, so coalesce to 0.
    """
    stmt = select(func.coalesce(func.sum(MediaFile.file_size), 0))
    if tenant_id is not None:
        stmt = stmt.where(MediaFile.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


def count_monitored_employees(db: Session, tenant_id: Optional[str] = None) -> int:
    """Distinct monitored WeCom employees (dedup on contacts.wecom_userid)."""
    stmt = select(func.count(func.distinct(Contact.wecom_userid)))
    if tenant_id is not None:
        stmt = stmt.where(Contact.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


def get_archived_days(db: Session, tenant_id: Optional[str] = None) -> int:
    """Number of archived days.

    Per-tenant (tenant_id given): span from the tenant's ``created_at`` to
    the first archived message's msgtime (or ``now`` when the tenant has no
    messages yet) — literal reading of the planner spec
    "tenant 创建→首条消息或 now".

    Global (tenant_id is None): number of DISTINCT calendar days across all
    messages (no single tenant anchor exists at platform scope).

    Both return an int >= 0.
    """
    if tenant_id is None:
        stmt = select(
            func.count(
                func.distinct(
                    func.to_timestamp(ArchiveMessage.msgtime / 1000.0).cast(Date)
                )
            )
        ).where(ArchiveMessage.msgtime.isnot(None))
        return int(db.execute(stmt).scalar() or 0)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None or tenant.created_at is None:
        return 0
    start = tenant.created_at

    first_msg = db.execute(
        select(func.min(ArchiveMessage.msgtime)).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
        )
    ).scalar()
    end = (
        datetime.fromtimestamp(first_msg / 1000.0, tz=timezone.utc)
        if first_msg
        else datetime.now(timezone.utc)
    )
    return max(0, (end.date() - start.date()).days)


def sync_health(db: Session, tenant_id: Optional[str] = None) -> dict:
    """Aggregate SyncState health for the tenant (or all tenants).

    Returns a dict: {status, error_message, last_seq, updated_at}.
      status: "error" if any row errors, else "syncing" if any syncing,
              else "idle"; "unknown" when there are no rows at all.
    """
    stmt = select(SyncState)
    if tenant_id is not None:
        stmt = stmt.where(SyncState.tenant_id == tenant_id)
    rows = db.execute(stmt).scalars().all()
    if not rows:
        return {
            "status": "unknown",
            "error_message": None,
            "last_seq": None,
            "updated_at": None,
        }

    statuses = {row.status for row in rows}
    if _SYNC_ERROR in statuses:
        overall = _SYNC_ERROR
    elif _SYNC_SYNCING in statuses:
        overall = _SYNC_SYNCING
    else:
        overall = "idle"

    error_row = next((row for row in rows if row.status == _SYNC_ERROR), None)
    last_seq = max((row.last_seq for row in rows), default=None)
    updated_ats = [row.updated_at for row in rows if row.updated_at is not None]
    updated_at = max(updated_ats).isoformat() if updated_ats else None

    return {
        "status": overall,
        "error_message": error_row.error_message if error_row else None,
        "last_seq": last_seq,
        "updated_at": updated_at,
    }
