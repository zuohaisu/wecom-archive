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

from typing import Optional

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from app import conversation_membership
from app.db.models import ArchiveMessage, Contact, MediaFile, SyncState


_SYNC_ERROR = "error"
_SYNC_SYNCING = "syncing"
_MS_PER_DAY = 86_400_000
_BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000


def _reviewable_message_filter():
    """Return the archive condition shared by user-facing aggregates.

    A row becomes available to the review console only after decryption
    succeeds.  Counting pending/failed envelopes made a headline number look
    larger than the data users could actually inspect.
    """
    return ArchiveMessage.decrypt_status == "success"


def count_messages(db: Session, tenant_id: Optional[str] = None) -> int:
    """Total ingested archive envelopes for an existing usage consumer."""
    stmt = select(func.count(ArchiveMessage.id))
    if tenant_id is not None:
        stmt = stmt.where(ArchiveMessage.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


def count_reviewable_messages(db: Session, tenant_id: Optional[str] = None) -> int:
    """Count successfully archived messages that can appear in review UI."""
    stmt = select(func.count(ArchiveMessage.id)).where(_reviewable_message_filter())
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


def sum_downloaded_storage(db: Session, tenant_id: Optional[str] = None) -> int:
    """Return the byte total of media files whose download completed."""
    stmt = select(func.coalesce(func.sum(MediaFile.file_size), 0)).where(
        MediaFile.download_status == "downloaded"
    )
    if tenant_id is not None:
        stmt = stmt.where(MediaFile.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


def count_monitored_employees(db: Session, tenant_id: Optional[str] = None) -> int:
    """Distinct monitored WeCom employees (dedup on contacts.wecom_userid)."""
    stmt = select(func.count(func.distinct(Contact.wecom_userid)))
    if tenant_id is not None:
        stmt = stmt.where(Contact.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


def count_archived_members(db: Session, tenant_id: str) -> int:
    """Return internal members actually represented in this tenant's archive.

    This deliberately uses the same internal-member classifier as conversation
    review instead of treating every ``contacts`` row as an archive member.
    """
    return len(conversation_membership._collect_staff_ids(db, tenant_id))


def get_archived_days(db: Session, tenant_id: Optional[str] = None) -> int:
    """Count Beijing calendar days containing reviewable archived messages.

    This is intentionally *not* the elapsed time from the first message to
    today: gaps in an archive must not be presented as continuous coverage.
    The result is therefore an honest coverage count for both tenant and
    platform summaries.
    """
    beijing_day = cast(
        func.floor((ArchiveMessage.msgtime + _BEIJING_OFFSET_MS) / _MS_PER_DAY),
        Integer,
    )
    stmt = select(func.count(func.distinct(beijing_day))).where(
        ArchiveMessage.msgtime.isnot(None),
        _reviewable_message_filter(),
    )
    if tenant_id is not None:
        stmt = stmt.where(ArchiveMessage.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


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
