"""Customer-relations derivations shared by RND-373/374 (GH-83/84).

Two read-only views over authoritative data, for the 1–5 人小团队老板:

- 客户 → 相关员工 (external contact detail): the official follow/owner
  relationships from ``external_contact_follows`` / ``contacts.owner_*``
  plus archive-derived staff with first/last interaction times;
- 员工 → 相关客户 (staff view): the external parties a staff member has
  actually interacted with, aggregated per customer.

Official (registry-backed) and archive-derived relations are never
blended into one claim — the caller labels them separately. Everything is
tenant-scoped, reads ids/timestamps only (never message content), and
reuses the authoritative conversation-membership classification.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.conversation_membership import (
    _derive_conversation_membership,
    _staff_ids_for_participants,
)
from app.services.listing_service import (
    _fetch_compact_messages_for_entity,
    _load_recipients_map_compact,
)
from app.db.models import (
    AdminUser,
    Contact,
    ExternalContact,
    ExternalContactFollow,
)

_MS = 1000  # archive msgtime is epoch milliseconds


def _staff_display(
    db: Session, tenant_id: str, staff_userid: str
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (name, admin_status, role) for a staff id, best-effort."""
    admin = db.scalars(
        select(AdminUser).where(
            AdminUser.tenant_id == tenant_id,
            AdminUser.wecom_user_id == staff_userid,
        )
    ).first()
    if admin is not None:
        return admin.name, admin.status, admin.role
    contact = db.scalars(
        select(Contact).where(
            Contact.tenant_id == tenant_id,
            Contact.wecom_userid == staff_userid,
        )
    ).first()
    if contact is not None:
        return contact.name, None, None
    return None, None, None


def related_staff_for_contact(
    db: Session, tenant_id: str, external_userid: str
) -> list[dict]:
    """Return per-staff relations for one external customer.

    Items carry: staff_userid, display_name, relations ("official" and/or
    "derived"), follow_remark, admin_status, has_seat, message_count,
    first_interaction_at / last_interaction_at (ISO, derived from archive
    msgtime — ids and counts only, never message content).
    """
    messages = _fetch_compact_messages_for_entity(db, external_userid, tenant_id)
    if not messages:
        follows = _active_follows(db, tenant_id, external_userid)
        return [
            _staff_item(
                db, tenant_id, follow.follow_userid,
                official=True, derived=False,
                follow_remark=follow.remark_raw or follow.remark_normalized,
                count=0, first_ms=None, last_ms=None,
            )
            for follow in follows
        ]

    recipients_map = _load_recipients_map_compact(
        db, tenant_id, [m.id for m in messages]
    )

    participant_ids: set[str] = {m.sender for m in messages if m.sender}
    for receivers in recipients_map.values():
        participant_ids.update(receivers)
    participant_ids.discard(external_userid)
    staff_ids = _staff_ids_for_participants(db, tenant_id, participant_ids)

    per_staff: dict[str, dict] = {}
    for m in messages:
        receivers = recipients_map.get(m.id, [])
        involved = ({m.sender} if m.sender else set()) | set(receivers)
        for staff_id in involved & staff_ids:
            stat = per_staff.setdefault(
                staff_id, {"count": 0, "first": None, "last": None}
            )
            stat["count"] += 1
            if m.msgtime is not None:
                if stat["first"] is None or m.msgtime < stat["first"]:
                    stat["first"] = m.msgtime
                if stat["last"] is None or m.msgtime > stat["last"]:
                    stat["last"] = m.msgtime

    follows = {f.follow_userid: f for f in _active_follows(db, tenant_id, external_userid)}
    owner_id = _owner_userid(db, tenant_id, external_userid)
    if owner_id:
        follows.setdefault(owner_id, None)

    items = []
    for staff_id in sorted(follows) + sorted(set(per_staff) - set(follows)):
        follow = follows.get(staff_id)
        stat = per_staff.get(staff_id, {"count": 0, "first": None, "last": None})
        items.append(
            _staff_item(
                db, tenant_id, staff_id,
                official=follow is not None or owner_id == staff_id,
                derived=staff_id in per_staff,
                follow_remark=(
                    follow.remark_raw or follow.remark_normalized
                    if follow is not None else None
                ),
                count=stat["count"], first_ms=stat["first"], last_ms=stat["last"],
            )
        )
    return items


def _active_follows(db: Session, tenant_id: str, external_userid: str):
    return db.scalars(
        select(ExternalContactFollow).where(
            ExternalContactFollow.tenant_id == tenant_id,
            ExternalContactFollow.external_userid == external_userid,
            ExternalContactFollow.is_active.is_(True),
        )
    ).all()


def _owner_userid(db: Session, tenant_id: str, external_userid: str) -> Optional[str]:
    contact = db.scalars(
        select(Contact).where(
            Contact.tenant_id == tenant_id,
            Contact.wecom_userid == external_userid,
        )
    ).first()
    return getattr(contact, "owner_wecom_userid", None) if contact is not None else None


def _staff_item(
    db: Session, tenant_id: str, staff_userid: str, *, official: bool,
    derived: bool, follow_remark: Optional[str], count: int,
    first_ms: Optional[int], last_ms: Optional[int],
) -> dict:
    name, admin_status, _role = _staff_display(db, tenant_id, staff_userid)
    return {
        "staff_userid": staff_userid,
        "display_name": name,
        "admin_status": admin_status,
        "is_official_follow": official,
        "is_derived_from_archive": derived,
        "follow_remark": follow_remark,
        "message_count": count,
        "first_interaction_at": (
            datetime_from_ms(first_ms) if first_ms is not None else None
        ),
        "last_interaction_at": (
            datetime_from_ms(last_ms) if last_ms is not None else None
        ),
    }


def datetime_from_ms(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def related_customers_for_staff(
    db: Session, tenant_id: str, staff_userid: str, *, q: Optional[str] = None,
    limit: int = 20, offset: int = 0,
) -> tuple[list[dict], int]:
    """Return the external customers a staff member has interacted with.

    Customers are the non-staff counterparties in the staff member's
    archived conversations, aggregated per customer id with conversation
    counts and first/last interaction. Sorted by last interaction
    descending; ``q`` filters by display name or id. Pagination is applied
    after aggregation — the directory never loads unbounded history.
    """
    messages = _fetch_compact_messages_for_entity(db, staff_userid, tenant_id)
    if not messages:
        return [], 0

    recipients_map = _load_recipients_map_compact(
        db, tenant_id, [m.id for m in messages]
    )

    participant_ids: set[str] = {m.sender for m in messages if m.sender}
    for receivers in recipients_map.values():
        participant_ids.update(receivers)
    participant_ids.discard(staff_userid)
    staff_ids = _staff_ids_for_participants(db, tenant_id, participant_ids)

    per_customer: dict[str, dict] = {}
    for m in messages:
        receivers = {m.sender} if m.sender else set()
        receivers.update(recipients_map.get(m.id, []))
        receivers.discard(staff_userid)
        conv_id, conv_type, _staff_set, contact_set = _derive_conversation_membership(
            m.sender, m.roomid, sorted(receivers),
            lambda uid: uid in staff_ids,
        )
        for customer in contact_set:
            stat = per_customer.setdefault(
                customer, {"count": 0, "first": None, "last": None, "direct": 0, "group": 0}
            )
            stat["count"] += 1
            if conv_type == "direct":
                stat["direct"] += 1
            else:
                stat["group"] += 1
            if m.msgtime is not None:
                if stat["first"] is None or m.msgtime < stat["first"]:
                    stat["first"] = m.msgtime
                if stat["last"] is None or m.msgtime > stat["last"]:
                    stat["last"] = m.msgtime

    if q:
        needle = q.lower()
        display = _load_display_names_for_ids(db, tenant_id, set(per_customer))
        per_customer = {
            cid: stat
            for cid, stat in per_customer.items()
            if needle in cid.lower() or needle in (display.get(cid) or "").lower()
        }

    ordered = sorted(
        per_customer.items(),
        key=lambda pair: (pair[1]["last"] or 0, pair[0]),
        reverse=True,
    )
    total = len(ordered)
    page = ordered[offset : offset + limit]

    registered_external = set(
        db.scalars(
            select(ExternalContact.external_userid).where(
                ExternalContact.tenant_id == tenant_id,
                ExternalContact.external_userid.in_([cid for cid, _ in page] or ["-"]),
            )
        )
    ) if page else set()
    display_names = _load_display_names_for_ids(db, tenant_id, {cid for cid, _ in page})

    items = []
    for cid, stat in page:
        items.append({
            "customer_userid": cid,
            "display_name": display_names.get(cid),
            "is_registered_external": cid in registered_external,
            "direct_messages": stat["direct"],
            "group_messages": stat["group"],
            "message_count": stat["count"],
            "first_interaction_at": (
                datetime_from_ms(stat["first"]) if stat["first"] is not None else None
            ),
            "last_interaction_at": (
                datetime_from_ms(stat["last"]) if stat["last"] is not None else None
            ),
        })
    return items, total
