"""
RND-159 — Search API: contacts and messages.

All routes are protected by get_current_user (RND-110).
All archive queries are scoped by session tenant_id.
tenant_id is NEVER accepted from user-supplied request params.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_, text, union
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.conversation_membership import (
    _derive_conversation_membership,
    _load_recipients_map,
    _is_staff as _legacy_is_staff,
)
from app.db.models import AdminUser, ArchiveMessage, ArchiveMessageRecipient, Contact
from app.db.session import get_db
from app.display_names import resolve_person_display_name, resolve_room_display_name

router = APIRouter()
logger = logging.getLogger(__name__)

_MAX_LIMIT = 100
_SNIPPET_CONTEXT_CHARS = 80


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ContactSearchResult(BaseModel):
    wecom_userid: str
    display_name: str
    match_field: str  # "name" or "wecom_userid"


class MessageSearchResult(BaseModel):
    msgid: str
    sender: Optional[str]
    sender_display_name: str
    content_snippet: str
    msgtime: Optional[int]
    roomid: Optional[str]
    conversation_id: str
    conversation_type: str  # "group" or "direct"
    conversation_name: str
    match_position: int
    entity_id: Optional[str] = None     # The WeCom userid to navigate to
    entity_type: Optional[str] = None   # "staff" or "contact"


class MessageSearchPagination(BaseModel):
    has_older: bool
    next_before: Optional[str] = None


class MessageSearchResponse(BaseModel):
    results: list[MessageSearchResult]
    pagination: MessageSearchPagination


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_ilike_pattern(raw: str) -> str:
    """Escape % and _ wildcards in ILIKE patterns (RND-159 acceptance fix 6)."""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet_with_context(text: str, keyword: str, context_chars: int = _SNIPPET_CONTEXT_CHARS) -> tuple[str, int]:
    """Extract a snippet around the first occurrence of keyword (case-insensitive)
    and return (snippet, match_position)."""
    lower_text = text.lower()
    lower_keyword = keyword.lower()
    pos = lower_text.find(lower_keyword)
    if pos == -1:
        # Keyword not found (shouldn't happen after DB match), return head
        return text[:context_chars * 2 + len(keyword)], 0

    start = max(0, pos - context_chars)
    end = min(len(text), pos + len(keyword) + context_chars)

    snippet = ""
    if start > 0:
        snippet += "…"
    snippet += text[start:end]
    if end < len(text):
        snippet += "…"

    return snippet, pos


def _build_display_name_map(db: Session, tenant_id: str, userids: set[str]) -> dict[str, Optional[str]]:
    """Build {wecom_userid: name} from the contacts table."""
    if not userids:
        return {}
    return {
        c.wecom_userid: c.name
        for c in db.query(Contact)
        .filter(Contact.tenant_id == tenant_id, Contact.wecom_userid.in_(userids))
        .all()
    }


def _build_staff_ids(db: Session, tenant_id: str) -> set[str]:
    """Return the set of wecom_userids treated as staff for this tenant.
    Simplified version of conversation_membership._collect_staff_ids."""
    participant_ids: set[str] = set()
    for row in (
        db.query(ArchiveMessage.sender)
        .filter(ArchiveMessage.sender.isnot(None), ArchiveMessage.tenant_id == tenant_id)
        .distinct()
        .all()
    ):
        if row[0]:
            participant_ids.add(row[0])
    for row in (
        db.query(ArchiveMessageRecipient.receiver_userid)
        .filter(ArchiveMessageRecipient.tenant_id == tenant_id)
        .distinct()
        .all()
    ):
        if row[0]:
            participant_ids.add(row[0])

    prefix_ids = {p for p in participant_ids if _legacy_is_staff(p)}
    admin_user_rows = (
        db.query(AdminUser.wecom_user_id)
        .filter(AdminUser.tenant_id == tenant_id)
        .distinct()
        .all()
    )
    admin_user_ids = {row[0] for row in admin_user_rows if row[0]}
    if not admin_user_ids:
        return prefix_ids
    admin_seat_ids = admin_user_ids & participant_ids
    return prefix_ids | admin_seat_ids


def _pick_entity_id(
    staff_ids: set[str],
    sender: Optional[str],
    recipients: list[str],
    is_staff_fn,
) -> Tuple[Optional[str], Optional[str]]:
    """Pick the best navigable entity (entity_id, entity_type) for a message.

    Prefer a staff member in the conversation; otherwise pick a contact.
    """
    all_participants: set[str] = set()
    if sender:
        all_participants.add(sender)
    all_participants.update(recipients)
    all_participants.discard("")

    # Prefer a staff entity for navigation
    for pid in all_participants:
        if pid in staff_ids or is_staff_fn(pid):
            return pid, "staff"

    # Fall back to any contact
    for pid in all_participants:
        return pid, "contact"

    return None, None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/search/contacts", response_model=list[ContactSearchResult])
def search_contacts(
    q: str = Query(..., min_length=1, description="Search keyword for contact name or WeCom user ID"),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT, description="Max rows to return (1–100)"),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Search contacts by display name or WeCom user ID.
    Tenant-scoped: only returns contacts belonging to the authenticated tenant.
    Name matches are ranked before userid matches.
    Searches both Contact.name/Contact.wecom_userid and AdminUser.name/wecom_user_id.
    """
    _, tenant_id = auth
    escaped_q = _escape_ilike_pattern(q)
    pattern = f"%{escaped_q}%"

    # Search both Contact and AdminUser tables, deduplicate by wecom_userid.
    # AdminUser name takes precedence for staff display names.
    contact_rows = (
        db.query(Contact.wecom_userid, Contact.name)
        .filter(
            Contact.tenant_id == tenant_id,
            or_(
                Contact.name.ilike(pattern, escape="\\"),
                Contact.wecom_userid.ilike(pattern, escape="\\"),
            ),
        )
        .all()
    )

    admin_rows = (
        db.query(AdminUser.wecom_user_id, AdminUser.name)
        .filter(
            AdminUser.tenant_id == tenant_id,
            or_(
                AdminUser.name.ilike(pattern, escape="\\"),
                AdminUser.wecom_user_id.ilike(pattern, escape="\\"),
            ),
        )
        .all()
    )

    # Merge: AdminUser takes precedence for display_name, Contact fills gaps
    result_map: dict[str, tuple[str, bool, bool]] = {}
    # Track whether we matched, for ordering
    for wecom_userid, name in contact_rows:
        matched_in_name = bool(name and q.lower() in name.lower())
        matched_in_id = not matched_in_name
        result_map[wecom_userid] = (name, matched_in_name, matched_in_id)

    for wecom_userid, name in admin_rows:
        matched_in_name = bool(name and q.lower() in name.lower())
        matched_in_id = not matched_in_name
        if wecom_userid in result_map:
            existing_name, _, _ = result_map[wecom_userid]
            if name and not existing_name:
                result_map[wecom_userid] = (name, matched_in_name, matched_in_id)
        else:
            result_map[wecom_userid] = (name, matched_in_name, matched_in_id)

    # Sort: name matches first, then by display_name
    sorted_results = sorted(
        result_map.items(),
        key=lambda kv: (
            0 if kv[1][1] else 1,
            (kv[1][0] or kv[0]).lower() if kv[1][0] else kv[0].lower(),
        ),
    )[:limit]

    return [
        ContactSearchResult(
            wecom_userid=wecom_userid,
            display_name=resolve_person_display_name(wecom_userid, name),
            match_field="name" if matched_name else "wecom_userid",
        )
        for wecom_userid, (name, matched_name, _) in sorted_results
    ]


@router.get("/api/search/messages", response_model=MessageSearchResponse)
def search_messages(
    q: str = Query(..., min_length=1, description="Case-insensitive substring match on message content"),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT, description="Max rows to return (1–100)"),
    before: Optional[str] = Query(None, description="Cursor for pagination (msgtime:id format)"),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Search archived text messages by content.
    Tenant-scoped: only returns messages belonging to the authenticated tenant.
    Only searches text messages (msgtype='text') that have been successfully
    decrypted and are not revoked.
    Pagination uses cursor-based (msgtime:id) — same as conversation timeline.
    """
    _, tenant_id = auth
    escaped_q = _escape_ilike_pattern(q)
    pattern = f"%{escaped_q}%"

    query = (
        db.query(
            ArchiveMessage.msgid,
            ArchiveMessage.sender,
            ArchiveMessage.content_text,
            ArchiveMessage.msgtime,
            ArchiveMessage.roomid,
            ArchiveMessage.id,
        )
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.content_text.ilike(pattern, escape="\\"),
            ArchiveMessage.msgtype == "text",
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.is_revoked.is_(False),
        )
    )

    if before:
        try:
            before_msgtime_str, before_id_str = before.split(":", 1)
            before_msgtime = int(before_msgtime_str)
            before_id = int(before_id_str)
        except (ValueError, AttributeError):
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Malformed pagination cursor")

        query = query.filter(
            or_(
                ArchiveMessage.msgtime < before_msgtime,
                and_(
                    ArchiveMessage.msgtime == before_msgtime,
                    ArchiveMessage.id < before_id,
                ),
            )
        )

    rows = (
        query
        .order_by(ArchiveMessage.msgtime.desc(), ArchiveMessage.id.desc())
        .limit(limit + 1)  # Fetch one extra to detect has_older
        .all()
    )

    has_older = len(rows) > limit
    if has_older:
        rows = rows[:limit]

    if not rows:
        return MessageSearchResponse(results=[], pagination=MessageSearchPagination(has_older=False, next_before=None))

    # Determine staff ids for entity navigation and conversation membership
    staff_ids = _build_staff_ids(db, tenant_id)

    def _is_staff_member(uid: str) -> bool:
        return uid in staff_ids or _legacy_is_staff(uid)

    # Build display name map for ALL participants (senders + recipients)
    msg_ids = [r.id for r in rows]
    recipients_map = _load_recipients_map(db, tenant_id, msg_ids)
    all_userids: set[str] = set()
    for r in rows:
        if r.sender:
            all_userids.add(r.sender)
        for rid in recipients_map.get(r.id, []):
            all_userids.add(rid)
    display_names = _build_display_name_map(db, tenant_id, all_userids)

    results: list[MessageSearchResult] = []
    for row in rows:
        snippet, match_pos = _snippet_with_context(row.content_text or "", q)
        sender_name = resolve_person_display_name(row.sender, display_names.get(row.sender))

        recipients = recipients_map.get(row.id, [])
        conv_id, conv_type, _staff_set, _contact_set = _derive_conversation_membership(
            row.sender, row.roomid, recipients, _is_staff_member
        )

        # Compute conversation display name
        if conv_type == "group":
            conv_name = resolve_room_display_name(conv_id)
        else:
            # For direct conversations, show the other party's name (load all participant names)
            other_ids = [p for p in recipients if p != row.sender]
            if not other_ids:
                other_ids = [p for p in ({row.sender} | set(recipients)) if p]
            other_id = other_ids[0] if other_ids else conv_id
            conv_name = resolve_person_display_name(other_id, display_names.get(other_id))

        # Pick navigable entity
        entity_id, entity_type = _pick_entity_id(staff_ids, row.sender, recipients, _is_staff_member)

        results.append(
            MessageSearchResult(
                msgid=row.msgid,
                sender=row.sender,
                sender_display_name=sender_name,
                content_snippet=snippet,
                msgtime=row.msgtime,
                roomid=row.roomid,
                conversation_id=conv_id,
                conversation_type=conv_type,
                conversation_name=conv_name,
                match_position=match_pos,
                entity_id=entity_id,
                entity_type=entity_type,
            )
        )

    # Pagination cursor
    last = rows[-1]
    next_before = f"{last.msgtime if last.msgtime is not None else 0}:{last.id}" if has_older else None

    return MessageSearchResponse(
        results=results,
        pagination=MessageSearchPagination(has_older=has_older, next_before=next_before),
    )
