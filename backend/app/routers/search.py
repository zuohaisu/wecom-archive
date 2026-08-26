"""
RND-159 — Search API: contacts and messages.

All routes are protected by get_current_user (RND-110).
All archive queries are scoped by session tenant_id.
tenant_id is NEVER accepted from user-supplied request params.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.conversation_membership import (
    _derive_conversation_membership,
    _load_recipients_map,
    _is_staff as _legacy_is_staff,
    _collect_staff_ids,
    _load_display_names_for_ids,
)
from app.db.group_chat_metadata import load_group_chat_display_names
from app.db.models import (
    AdminUser,
    ArchiveMessage,
    ArchiveMessageRecipient,
    Contact,
    ExternalContact,
)
from app.db.session import get_db
from app.display_names import resolve_person_display_name, resolve_room_display_name
from app.services.message_deletion import active_message_filter
from app.services.external_contact_identity import (
    load_external_contact_search_matches,
    normalized_search_term,
    resolve_external_contact_display_name,
)

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
    # "name" | "wecom_userid" | "remark" | "current_nickname" |
    # "historical_nickname". The latter three identify one external contact.
    match_field: str
    match_context_userid: Optional[str] = None
    is_external_contact: bool = False


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
    msgtype: Optional[str] = None
    # RND-230 QA remediation (finding C12): the full tenant-scoped
    # staff-side / contact-side participant sets for this message (sender +
    # recipients, split via the same is_staff check used everywhere else),
    # not just the single `entity_id` picked for navigation. A group
    # message can have several contacts and/or several staff, and a
    # staff-authored message's recipients are otherwise invisible to the
    # frontend — see _derive_conversation_membership, whose staff_set/
    # contact_set this reuses directly rather than recomputing.
    contact_ids: list[str] = []
    staff_ids: list[str] = []


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


_DATE_RANGE_PRESET_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}


def _participant_filter(db: Session, tenant_id: str, ids: set[str]):
    """Build a SQL-level (message.sender in ids) OR (message has a
    recipient row in ids) predicate for the `user`/`staff` filters. The
    recipient side is an EXISTS-shaped subquery (id IN (SELECT
    message_id ...)), never a Python-side pull of the full recipient
    table — the message row set stays bounded by the outer query's
    LIMIT, same as RND-228's scalability contract for the rest of this
    endpoint."""
    if not ids:
        # An empty (e.g. fully-filtered-out) id set must match nothing,
        # not "no filter at all".
        return ArchiveMessage.id.is_(None)
    recipient_subq = select(ArchiveMessageRecipient.message_id).where(
        ArchiveMessageRecipient.tenant_id == tenant_id,
        ArchiveMessageRecipient.receiver_userid.in_(ids),
    )
    return or_(
        ArchiveMessage.sender.in_(ids),
        ArchiveMessage.id.in_(recipient_subq),
    )


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
    # A whitespace/invisible-only term cannot match a meaningful external
    # identity and should not manufacture a broad or confusing result set.
    if normalized_search_term(q) is None:
        return []
    escaped_q = _escape_ilike_pattern(q)
    pattern = f"%{escaped_q}%"

    # Search both Contact and AdminUser tables, deduplicate by wecom_userid.
    # AdminUser name takes precedence for staff display names.
    # Each source query is bounded at the SQL level (name-match-first
    # ordering + LIMIT) so a large tenant's total matches never get pulled
    # into Python before truncation — the in-memory result set is capped at
    # ~2x limit (one bounded fetch per source) instead of growing with the
    # match count. A source's true top-`limit` rows are always among these,
    # so the final merge+sort below still picks the correct overall top
    # `limit`; the accepted tradeoff is that extreme cross-source dedup can
    # leave the final result short of `limit` rather than re-querying to
    # backfill.
    contact_name_match = case(
        (Contact.name.ilike(pattern, escape="\\"), 0),
        else_=1,
    )
    contact_rows = (
        db.query(Contact.wecom_userid, Contact.name)
        .filter(
            Contact.tenant_id == tenant_id,
            or_(
                Contact.name.ilike(pattern, escape="\\"),
                Contact.wecom_userid.ilike(pattern, escape="\\"),
            ),
        )
        .order_by(contact_name_match, Contact.name, Contact.wecom_userid)
        .limit(limit)
        .all()
    )

    admin_name_match = case(
        (AdminUser.name.ilike(pattern, escape="\\"), 0),
        else_=1,
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
        .order_by(admin_name_match, AdminUser.name, AdminUser.wecom_user_id)
        .limit(limit)
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

    internal_results = [
        ContactSearchResult(
            wecom_userid=wecom_userid,
            display_name=resolve_person_display_name(wecom_userid, name),
            match_field="name" if matched_name else "wecom_userid",
        )
        for wecom_userid, (name, matched_name, _) in sorted_results
    ]

    # RND-170: the global contact search also resolves the one external
    # identity behind an employee remark, current nickname, or former
    # nickname. The new identity tables can be absent only in intentionally
    # minimal legacy test fixtures; in that case the established Contact/Admin
    # search remains available rather than turning the whole endpoint into 500.
    external_matches = load_external_contact_search_matches(
        db, tenant_id, q, limit=limit
    )
    external_rows = []
    if external_matches:
        try:
            with db.begin_nested():
                external_rows = db.scalars(
                    select(ExternalContact).where(
                        ExternalContact.tenant_id == tenant_id,
                        ExternalContact.external_userid.in_(external_matches),
                    )
                ).all()
        except DBAPIError:
            external_rows = []

    match_order = {"remark": 0, "current_nickname": 1, "historical_nickname": 2}
    combined: dict[str, ContactSearchResult] = {
        item.wecom_userid: item for item in internal_results
    }
    for contact in external_rows:
        matches = external_matches.get(contact.external_userid, [])
        if not matches:
            continue
        match = min(matches, key=lambda item: match_order.get(item["match_type"], 99))
        combined[contact.external_userid] = ContactSearchResult(
            wecom_userid=contact.external_userid,
            display_name=resolve_external_contact_display_name(
                contact.external_userid,
                current_nickname=(
                    contact.current_nickname_display or contact.current_nickname_raw
                ),
            ),
            match_field=match["match_type"],
            match_context_userid=match.get("follow_userid"),
            is_external_contact=True,
        )

    return sorted(
        combined.values(),
        key=lambda item: (
            1 if item.match_field == "wecom_userid" else 0,
            item.display_name.casefold(),
            item.wecom_userid,
        ),
    )[:limit]


@router.get("/api/search/messages", response_model=MessageSearchResponse)
def search_messages(
    q: Optional[str] = Query(None, min_length=1, description="Case-insensitive substring match on message content; optional when at least one filter is set"),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT, description="Max rows to return (1–100)"),
    before: Optional[str] = Query(None, description="Cursor for pagination (msgtime:id format)"),
    date_range: Optional[str] = Query(None, pattern="^(1d|7d|30d|90d)$", description="Preset recency window"),
    date_from: Optional[int] = Query(None, description="Lower bound on msgtime, ms epoch, inclusive"),
    date_to: Optional[int] = Query(None, description="Upper bound on msgtime, ms epoch, inclusive"),
    user: Optional[list[str]] = Query(None, description="Contact-side participant userid(s); message must have one as sender or recipient"),
    staff: Optional[list[str]] = Query(None, description="Staff-side participant userid(s); message must have one as sender or recipient"),
    msgtype: Optional[list[str]] = Query(None, description="Message type(s); defaults to text-only when omitted"),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Search archived messages by content and/or filter (RND-230).
    Tenant-scoped: only returns messages belonging to the authenticated tenant.
    `q` is optional — with no keyword, at least one filter must be supplied
    (enforced below) so this never degrades into an unbounded table scan.
    Without an explicit `msgtype` filter, only text messages are searched
    (v1 index scope, unchanged from before this change). Only messages that
    have been successfully decrypted and are not revoked are ever returned.
    Pagination uses cursor-based (msgtime:id) — same as conversation timeline.
    """
    _, tenant_id = auth

    has_filter = any([date_range, date_from is not None, date_to is not None, user, staff, msgtype])
    if q is None and not has_filter:
        raise HTTPException(
            status_code=400,
            detail="Provide a search keyword (q) or at least one filter",
        )

    # staff_ids is computed eagerly only when the `staff` filter needs it;
    # otherwise it stays None and is computed later (only if there are any
    # rows), matching the existing entity-navigation lookup's lazy trigger.
    staff_ids: Optional[set[str]] = None

    filters = [
        ArchiveMessage.tenant_id == tenant_id,
        active_message_filter(),
        ArchiveMessage.decrypt_status == "success",
        ArchiveMessage.is_revoked.is_(False),
    ]

    if q is not None:
        escaped_q = _escape_ilike_pattern(q)
        pattern = f"%{escaped_q}%"
        filters.append(ArchiveMessage.content_text.ilike(pattern, escape="\\"))

    if msgtype:
        filters.append(ArchiveMessage.msgtype.in_(msgtype))
    else:
        filters.append(ArchiveMessage.msgtype == "text")

    effective_date_from = date_from
    if date_range:
        preset_from = int(time.time() * 1000) - _DATE_RANGE_PRESET_DAYS[date_range] * 24 * 60 * 60 * 1000
        effective_date_from = preset_from if effective_date_from is None else max(effective_date_from, preset_from)
    if effective_date_from is not None:
        filters.append(ArchiveMessage.msgtime >= effective_date_from)
    if date_to is not None:
        filters.append(ArchiveMessage.msgtime <= date_to)

    if staff:
        staff_ids = _collect_staff_ids(db, tenant_id)
        filters.append(_participant_filter(db, tenant_id, set(staff) & staff_ids))

    if user:
        filters.append(_participant_filter(db, tenant_id, set(user)))

    query = (
        db.query(
            ArchiveMessage.msgid,
            ArchiveMessage.sender,
            ArchiveMessage.content_text,
            ArchiveMessage.msgtime,
            ArchiveMessage.roomid,
            ArchiveMessage.id,
            ArchiveMessage.msgtype,
        )
        .filter(*filters)
    )

    if before:
        try:
            before_msgtime_str, before_id_str = before.split(":", 1)
            before_msgtime = int(before_msgtime_str)
            before_id = int(before_id_str)
        except (ValueError, AttributeError):
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
    # (already computed above if the `staff` filter needed it).
    if staff_ids is None:
        staff_ids = _collect_staff_ids(db, tenant_id)

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
    display_names = _load_display_names_for_ids(db, tenant_id, all_userids)
    room_display_names = load_group_chat_display_names(
        db, tenant_id, (row.roomid for row in rows)
    )

    results: list[MessageSearchResult] = []
    for row in rows:
        snippet, match_pos = _snippet_with_context(row.content_text or "", q or "")
        sender_name = resolve_person_display_name(row.sender, display_names.get(row.sender))

        recipients = recipients_map.get(row.id, [])
        conv_id, conv_type, row_staff_set, row_contact_set = _derive_conversation_membership(
            row.sender, row.roomid, recipients, _is_staff_member
        )

        # Compute conversation display name
        if conv_type == "group":
            roomid = row.roomid or conv_id
            conv_name = resolve_room_display_name(roomid, room_display_names.get(roomid))
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
                msgtype=row.msgtype,
                contact_ids=sorted(row_contact_set),
                staff_ids=sorted(row_staff_set),
            )
        )

    # Pagination cursor
    last = rows[-1]
    next_before = f"{last.msgtime if last.msgtime is not None else 0}:{last.id}" if has_older else None

    return MessageSearchResponse(
        results=results,
        pagination=MessageSearchPagination(has_older=has_older, next_before=next_before),
    )
