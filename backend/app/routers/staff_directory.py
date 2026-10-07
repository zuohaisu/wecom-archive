"""Read-only internal-staff directory with archive message statistics.

Haisu split request: the message statistics that used to ride on the
console-seat table (users.py) belong to the staff directory, displayed
read-only. Haisu follow-up: the directory lists EVERY internal staff
member who appeared in the archive — not only seat-linked accounts or
``staff_``-prefixed ids. Internal is therefore the complement of the
authoritative external registry: an archive participant is internal
staff unless WeCom's external-contact sync registered them in
``external_contacts``. Counts mirror the RND-284 rule: messages SENT by
the staff id — ids and counts only, never payloads.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.conversation_membership import _collect_archive_participant_ids
from app.db.models import AdminUser, ArchiveMessage, Contact, ExternalContact
from app.db.session import get_db
from app.schemas.staff_directory import StaffDirectoryItem, StaffDirectoryPage
from app.services.avatar_sync import internal_avatar_presentations

router = APIRouter()


@router.get("/staff", response_model=StaffDirectoryPage)
def list_internal_staff(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    q: Optional[str] = Query(None, max_length=128),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StaffDirectoryPage:
    """Return the tenant's internal staff with read-only message counts."""
    _, tenant_id = auth
    participants = _collect_archive_participant_ids(db, tenant_id)
    registered_external = set(
        db.scalars(
            select(ExternalContact.external_userid).where(
                ExternalContact.tenant_id == tenant_id
            )
        ).all()
    )
    staff_ids = participants - registered_external

    display_rows = (
        db.query(Contact)
        .filter(Contact.tenant_id == tenant_id, Contact.wecom_userid.in_(staff_ids))
        .all()
        if staff_ids
        else []
    )
    contacts_by_id = {row.wecom_userid: row for row in display_rows}
    seats_by_id = {
        row.wecom_user_id: row
        for row in db.query(AdminUser)
        .filter(AdminUser.tenant_id == tenant_id, AdminUser.wecom_user_id.in_(staff_ids))
        .all()
        if row.wecom_user_id
    }

    filtered_ids = staff_ids
    if q:
        needle = q.lower()

        def _matches(staff_id: str) -> bool:
            if needle in staff_id.lower():
                return True
            contact = contacts_by_id.get(staff_id)
            return bool(contact and contact.name and needle in contact.name.lower())

        filtered_ids = {staff_id for staff_id in staff_ids if _matches(staff_id)}

    ordered_ids = sorted(
        filtered_ids,
        key=lambda staff_id: (
            contacts_by_id[staff_id].name or "" if staff_id in contacts_by_id else "",
            staff_id,
        ),
    )

    counts_30d, counts_total = _sent_message_counts(db, tenant_id, ordered_ids)
    page_ids = ordered_ids[(page - 1) * per_page : page * per_page]
    avatars = internal_avatar_presentations(db, tenant_id, set(page_ids))

    items = [
        StaffDirectoryItem(
            wecom_userid=staff_id,
            name=(
                contacts_by_id[staff_id].name
                if staff_id in contacts_by_id
                else (seats_by_id[staff_id].name if staff_id in seats_by_id else None)
            ),
            department=(
                seats_by_id[staff_id].department if staff_id in seats_by_id else None
            ),
            avatar_url=avatars[staff_id].url,
            avatar_status=avatars[staff_id].status,
            has_seat=staff_id in seats_by_id,
            msg_count_30d=counts_30d.get(staff_id, 0),
            msg_count_total=counts_total.get(staff_id, 0),
        )
        for staff_id in page_ids
    ]
    return StaffDirectoryPage(
        total=len(ordered_ids), page=page, per_page=per_page, items=items
    )


def _sent_message_counts(
    db: Session, tenant_id: str, staff_ids: list[str]
) -> tuple[dict[str, int], dict[str, int]]:
    """Return {staff_id: sent-count} for the 30-day window and all time."""
    empty: tuple[dict[str, int], dict[str, int]] = ({}, {})
    if not staff_ids:
        return empty
    cutoff_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    )
    try:
        counts_30d = dict(
            db.query(ArchiveMessage.sender, func.count(ArchiveMessage.id))
            .filter(
                ArchiveMessage.tenant_id == tenant_id,
                ArchiveMessage.sender.in_(staff_ids),
                ArchiveMessage.msgtime >= cutoff_ms,
            )
            .group_by(ArchiveMessage.sender)
            .all()
        )
        counts_total = dict(
            db.query(ArchiveMessage.sender, func.count(ArchiveMessage.id))
            .filter(
                ArchiveMessage.tenant_id == tenant_id,
                ArchiveMessage.sender.in_(staff_ids),
            )
            .group_by(ArchiveMessage.sender)
            .all()
        )
    except SQLAlchemyError:
        # Statistics are display-only garnish on a directory page; a counting
        # failure must not turn the staff listing into a 500. Counts stay 0.
        return empty
    return counts_30d, counts_total
