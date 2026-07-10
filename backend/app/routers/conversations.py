"""
Conversation aggregation APIs for the 365 WeCom Archive review console.

All routes are protected by get_current_user (RND-110).
All archive queries are scoped by session tenant_id.
tenant_id is NEVER accepted from user-supplied request params.

Aggregates raw archive_messages + archive_message_recipients into
first-class Conversation objects so the frontend never has to infer
conversations from raw messages.

Conversation identity:
  Group  → conversation_id = roomid
  Direct → conversation_id = "direct__<uid_a>___<uid_b>"  (sorted, triple-underscore separator)

Monitored-account / archive-seat detection (RND-132):
  No formal archive-seat roster exists anywhere in this schema — there is no
  config table or tenant field that explicitly lists which WeCom userid is a
  monitored archive seat. Two signals are combined, in order of confidence:

    1. Legacy "staff_" prefix convention. This is how the RND-96 mock/dev
       fixtures name monitored accounts, and is kept for backward
       compatibility with existing tests and any tenant that happens to
       provision seat accounts with this prefix.
    2. Any wecom_userid that is BOTH (a) an admin_users row for this tenant
       (i.e. has authenticated into this admin console at least once) AND
       (b) observed as a sender/recipient in this tenant's archive. Only an
       actual WeCom employee operating a monitored seat can satisfy both —
       an external contact never logs into the internal admin console — so
       this is a real relational signal, not an inference over message
       content. It is the production fallback: real decrypted senders are
       plain WeCom userids with no "staff_" prefix, so signal 1 alone
       finds nothing outside mock data.

  See _collect_staff_ids() for the implementation. This is deliberately NOT
  "every distinct sender" — that would surface customer/contact IDs as fake
  "staff" entries, which the console must never do.

  Among the resulting set, the seat with the most recent latest_message_time
  is reported as the single "active" seat (seat_status=active); all others
  are "history" (seat_status=history) — still visible so their archived
  conversations remain reviewable. This ranking is itself a stand-in for a
  real active-seat configuration source, which does not exist yet.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db.models import AdminUser, ArchiveMessage, ArchiveMessageRecipient, Contact, MediaFile
from app.db.session import get_db
from app.display_names import resolve_person_display_name, resolve_room_display_name
from app.media_classification import classify_media, resolve_image_media_status
from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageUnavailable,
    detect_image_content_type,
    detect_image_content_type_for_ref,
    get_media_storage_provider,
    resolve_effective_storage_reference,
    resolve_image_file_state,
    resolve_media_file_state,
    resolve_servable_image_path,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_staff(uid: str) -> bool:
    return uid.startswith("staff_")


def _collect_archive_participant_ids(db: Session, tenant_id: str) -> set[str]:
    """Return every distinct wecom_userid observed as a sender or recipient
    in this tenant's archive — staff and contacts alike, unfiltered."""
    sender_rows = (
        db.query(ArchiveMessage.sender)
        .filter(
            ArchiveMessage.sender.isnot(None),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .distinct()
        .all()
    )
    recipient_rows = (
        db.query(ArchiveMessageRecipient.receiver_userid)
        .filter(ArchiveMessageRecipient.tenant_id == tenant_id)
        .distinct()
        .all()
    )
    return {row[0] for row in sender_rows + recipient_rows if row[0]}


def _collect_staff_ids(db: Session, tenant_id: str) -> set[str]:
    """
    Return the set of wecom_userids treated as WeCom archive seats (staff)
    for this tenant. See the module docstring for why two signals are
    combined instead of a single formal source.
    """
    participant_ids = _collect_archive_participant_ids(db, tenant_id)
    prefix_ids = {p for p in participant_ids if _is_staff(p)}

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


def _direct_conv_id(uid_a: str, uid_b: str) -> str:
    """Stable conversation ID for a 1:1 pair regardless of sender/receiver order."""
    a, b = sorted([uid_a, uid_b])
    return f"direct__{a}___{b}"


def _load_display_names(db: Session, tenant_id: str) -> dict[str, Optional[str]]:
    """Return {wecom_userid: name} raw from Contact.name, scoped to tenant.

    Values are never blank (Contact rows are only ever created with a
    non-blank name — see upsert_contact_display_name), but a given ID may
    simply be absent from the dict if no Contact row exists yet. Callers
    resolve the final display label via resolve_person_display_name, which
    supplies the raw-ID fallback for absent/blank entries.
    """
    return {
        c.wecom_userid: c.name
        for c in db.query(Contact).filter(Contact.tenant_id == tenant_id).all()
    }


def _load_recipients_map(
    db: Session, tenant_id: str, msg_ids: list[int]
) -> dict[int, list[str]]:
    """Return {message_id: [receiver_userid, ...]} for the given message
    primary-key IDs, scoped to tenant_id. The explicit tenant_id filter is
    defense-in-depth on top of msg_ids already coming from a tenant-scoped
    message query — a malformed/mistagged archive_message_recipients row
    (wrong tenant_id, but message_id pointing at a real message belonging
    to a different tenant) must never leak into this tenant's recipient
    list on the strength of message_id alone."""
    if not msg_ids:
        return {}
    result: dict[int, list[str]] = {}
    for r in (
        db.query(ArchiveMessageRecipient)
        .filter(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessageRecipient.message_id.in_(msg_ids),
        )
        .all()
    ):
        result.setdefault(r.message_id, []).append(r.receiver_userid)
    return result


def _load_media_files_map(
    db: Session, tenant_id: str, msg_ids: list[int]
) -> dict[int, MediaFile]:
    """Return {archive_message_id: MediaFile} for the given message primary-key
    IDs, scoped to tenant_id. At most one row per message is expected
    (media_files.sdkfileid is unique per tenant and one image message has
    one sdkfileid). The explicit tenant_id filter is defense-in-depth on
    top of msg_ids already coming from a tenant-scoped message query — a
    media_files row must never be surfaced on the strength of
    archive_message_id alone."""
    if not msg_ids:
        return {}
    return {
        row.archive_message_id: row
        for row in db.query(MediaFile)
        .filter(
            MediaFile.tenant_id == tenant_id,
            MediaFile.archive_message_id.in_(msg_ids),
        )
        .all()
    }


def _latest_own_participation_time(
    db: Session, entity_id: str, tenant_id: str
) -> Optional[int]:
    """
    Return the max msgtime among messages where entity_id is literally the
    sender, or literally a listed recipient — WITHOUT expanding through
    shared group rooms.

    This is deliberately narrower than _fetch_messages_for_entity(), which
    expands seed messages to every message in a shared group room so the
    Sessions list can show full group context. That expansion is correct
    for session viewing, but if it were also used to compute a seat's
    "latest activity" for active/history ranking, a historical seat that
    once participated in a group would incorrectly inherit a later message
    in that same room sent by someone else after the seat stopped
    participating (RND-132 QA fix). Only this function's result may be used
    for seat active/history classification and ranking.
    """
    sender_max = (
        db.query(func.max(ArchiveMessage.msgtime))
        .filter(
            ArchiveMessage.sender == entity_id,
            ArchiveMessage.tenant_id == tenant_id,
        )
        .scalar()
    )
    recipient_max = (
        db.query(func.max(ArchiveMessage.msgtime))
        .join(
            ArchiveMessageRecipient,
            ArchiveMessage.id == ArchiveMessageRecipient.message_id,
        )
        .filter(
            ArchiveMessageRecipient.receiver_userid == entity_id,
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessage.tenant_id == tenant_id,
        )
        .scalar()
    )
    candidates = [v for v in (sender_max, recipient_max) if v is not None]
    return max(candidates) if candidates else None


def _fetch_messages_for_entity(
    db: Session, entity_id: str, tenant_id: str
) -> list:
    """
    Return all messages that belong to conversations involving entity_id,
    scoped to the given tenant.

    Strategy:
    - Find message IDs where entity_id is sender or recipient within the tenant.
    - From those, collect group roomids and expand to ALL messages in those rooms
      (for full group context even when entity isn't listed as recipient on every row).
    - Direct messages are included as-is (every direct message directly involves the entity).
    """
    sender_ids: set[int] = {
        row[0]
        for row in db.query(ArchiveMessage.id)
        .filter(
            ArchiveMessage.sender == entity_id,
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    }
    recipient_ids: set[int] = {
        row[0]
        for row in db.query(ArchiveMessageRecipient.message_id)
        .filter(
            ArchiveMessageRecipient.receiver_userid == entity_id,
            ArchiveMessageRecipient.tenant_id == tenant_id,
        )
        .all()
    }
    seed_ids = sender_ids | recipient_ids
    if not seed_ids:
        return []

    seed_msgs = (
        db.query(ArchiveMessage)
        .filter(
            ArchiveMessage.id.in_(seed_ids),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    )
    group_rooms = {m.roomid for m in seed_msgs if m.roomid}

    final_ids: set[int] = set()

    if group_rooms:
        for row in (
            db.query(ArchiveMessage.id)
            .filter(
                ArchiveMessage.roomid.in_(group_rooms),
                ArchiveMessage.tenant_id == tenant_id,
            )
            .all()
        ):
            final_ids.add(row[0])

    direct_ids = {m.id for m in seed_msgs if not m.roomid}
    final_ids.update(direct_ids)

    if not final_ids:
        return []

    return (
        db.query(ArchiveMessage)
        .filter(
            ArchiveMessage.id.in_(final_ids),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    )


def _derive_conversation_membership(
    sender: Optional[str],
    roomid: Optional[str],
    recipients: list[str],
    is_staff: Callable[[str], bool],
) -> Tuple[str, str, set[str], set[str]]:
    """
    Compute (conversation_id, conversation_type, staff_participants,
    contact_participants) for a single message, using the same per-message
    rule _build_conversation_list has always used.

    Extracted as a standalone, reusable function so the Message
    Reachability Audit (RND-178) can recompute a message's expected
    conversation membership through this exact path instead of a parallel,
    divergence-prone reimplementation. Pure behavior-preserving refactor —
    no change to conv_id/conv_type outputs.
    """
    sender = sender or ""
    roomid = roomid or ""
    all_parties = {p for p in ({sender} | set(recipients)) if p}
    staff_set = {p for p in all_parties if is_staff(p)}
    contact_set = {p for p in all_parties if not is_staff(p)}

    if roomid:
        return roomid, "group", staff_set, contact_set

    if staff_set and contact_set:
        conv_id = _direct_conv_id(sorted(staff_set)[0], sorted(contact_set)[0])
    else:
        parts = sorted(all_parties)
        if len(parts) >= 2:
            conv_id = f"direct__{parts[0]}___{parts[1]}"
        else:
            conv_id = f"direct__{parts[0] if parts else 'unknown'}"
    return conv_id, "direct", staff_set, contact_set


def _build_conversation_list(
    messages: list,
    recipients_map: dict[int, list[str]],
    display_names: dict[str, str],
    staff_ids: Optional[set[str]] = None,
) -> list[dict]:
    """
    Aggregate a flat message list into conversation summary objects.

    staff_ids: when provided (the tenant's resolved seat set from
    _collect_staff_ids), membership in this set determines staff/contact
    classification instead of the legacy "staff_" prefix check — this lets
    callers classify participants correctly in tenants where the archive
    seat's real userid does not use that prefix. Defaults to None so this
    remains a pure function callable without a DB round-trip (existing
    tests rely on this).

    Returns a list sorted by last_message_time descending (most recent first).
    """
    convs: dict[str, dict] = {}

    def is_staff(uid: str) -> bool:
        if staff_ids is not None:
            return uid in staff_ids
        return _is_staff(uid)

    for msg in messages:
        roomid = msg.roomid or ""
        recipients = recipients_map.get(msg.id, [])
        conv_id, conv_type, staff_set, contact_set = _derive_conversation_membership(
            msg.sender, msg.roomid, recipients, is_staff
        )

        if conv_id not in convs:
            convs[conv_id] = {
                "conversation_id": conv_id,
                "conversation_type": conv_type,
                "roomid": msg.roomid if roomid else None,
                "monitored_account_ids": set(),
                "contact_ids": set(),
                "msgs": [],
            }

        convs[conv_id]["monitored_account_ids"].update(staff_set)
        convs[conv_id]["contact_ids"].update(contact_set)
        convs[conv_id]["msgs"].append(msg)

    result = []
    for conv_id, data in convs.items():
        msgs_sorted = sorted(data["msgs"], key=lambda m: m.msgtime or 0)
        latest = msgs_sorted[-1]

        sids = sorted(data["monitored_account_ids"])
        cids = sorted(data["contact_ids"])

        if data["conversation_type"] == "group":
            room_raw_id = data["roomid"] or conv_id
            room_display_name = resolve_room_display_name(room_raw_id)
            display_name = room_display_name
            raw_id = room_raw_id
        else:
            room_display_name = None
            room_raw_id = None
            if cids:
                raw_id = cids[0]
            elif sids:
                raw_id = sids[0]
            else:
                raw_id = conv_id
            display_name = resolve_person_display_name(raw_id, display_names.get(raw_id))

        latest_sender_id = latest.sender
        latest_sender_display_name = (
            resolve_person_display_name(latest_sender_id, display_names.get(latest_sender_id))
            if latest_sender_id
            else None
        )

        result.append(
            {
                "conversation_id": conv_id,
                "conversation_type": data["conversation_type"],
                "display_name": display_name,
                "raw_id": raw_id,
                "roomid": data["roomid"],
                "monitored_account_ids": sids,
                "monitored_account_raw_ids": sids,
                "monitored_account_display_names": [
                    resolve_person_display_name(sid, display_names.get(sid)) for sid in sids
                ],
                "contact_ids": cids,
                "contact_raw_ids": cids,
                "contact_display_names": [
                    resolve_person_display_name(cid, display_names.get(cid)) for cid in cids
                ],
                "room_display_name": room_display_name,
                "room_raw_id": room_raw_id,
                "last_message_time": latest.msgtime,
                "last_message_text": (latest.content_text or "")[:200],
                "message_count": len(msgs_sorted),
                "latest_sender_id": latest_sender_id,
                "latest_sender_raw_id": latest_sender_id,
                "latest_sender_display_name": latest_sender_display_name,
                "review_status": None,
                "ai_status": None,
                "ai_summary": None,
            }
        )

    result.sort(key=lambda x: x["last_message_time"] or 0, reverse=True)
    return result


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class MonitoredAccountOut(BaseModel):
    monitored_account_id: str
    display_name: str
    staff_id: str
    raw_id: str
    seat_status: str  # "active" | "history" | "unknown"
    is_active_archive_seat: bool
    latest_message_time: Optional[int] = None
    conversation_count: int = 0


class ContactOut(BaseModel):
    contact_id: str
    display_name: str
    raw_id: str


class ConversationOut(BaseModel):
    conversation_id: str
    conversation_type: str
    display_name: str
    raw_id: str
    roomid: Optional[str] = None
    monitored_account_ids: list[str]
    monitored_account_raw_ids: list[str]
    monitored_account_display_names: list[str]
    contact_ids: list[str]
    contact_raw_ids: list[str]
    contact_display_names: list[str]
    room_display_name: Optional[str] = None
    room_raw_id: Optional[str] = None
    last_message_time: Optional[int] = None
    last_message_text: Optional[str] = None
    message_count: int
    latest_sender_id: Optional[str] = None
    latest_sender_raw_id: Optional[str] = None
    latest_sender_display_name: Optional[str] = None
    review_status: Optional[str] = None
    ai_status: Optional[str] = None
    ai_summary: Optional[str] = None


class TimelineMessageOut(BaseModel):
    msgid: str
    sender: Optional[str] = None
    sender_display_name: Optional[str] = None
    sender_raw_id: Optional[str] = None
    recipients: list[str]
    recipient_display_names: list[str] = []
    recipient_raw_ids: list[str] = []
    msgtime: Optional[int] = None
    msgtype: Optional[str] = None
    content_text: Optional[str] = None
    roomid: Optional[str] = None
    decrypt_status: str
    media_type: str
    media_status: Optional[str] = None
    unsupported_reason: Optional[str] = None
    media_url: Optional[str] = None


class PaginationOut(BaseModel):
    has_older: bool
    next_before: Optional[str] = None


class ConversationMessagesOut(BaseModel):
    messages: list[TimelineMessageOut]
    pagination: PaginationOut


# ---------------------------------------------------------------------------
# Message pagination cursor (RND-132 QA fix)
#
# msgtime alone is not a unique key — multiple archive_messages rows can
# share the exact same msgtime (e.g. a burst ingested in one batch). A
# cursor built from msgtime only, compared with strict "<", silently drops
# every row that shares the boundary msgtime once more than `limit` rows
# share it. The cursor is therefore a compound (msgtime, id) pair: id is
# the ArchiveMessage primary key, which is always present and gives a
# stable, monotonic tie-breaker so pagination order is a strict total
# order with no gaps or duplicates regardless of msgtime collisions.
# ---------------------------------------------------------------------------


def _encode_message_cursor(msgtime: Optional[int], message_id: int) -> str:
    return f"{msgtime if msgtime is not None else 0}:{message_id}"


def _decode_message_cursor(cursor: str) -> tuple[int, int]:
    try:
        msgtime_str, id_str = cursor.split(":", 1)
        return int(msgtime_str), int(id_str)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Malformed pagination cursor")


# ---------------------------------------------------------------------------
# Routes — all protected by get_current_user
# ---------------------------------------------------------------------------


@router.get("/api/monitored-accounts", response_model=list[MonitoredAccountOut])
def get_monitored_accounts(
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Return all WeCom archive seats (monitored accounts) for this tenant —
    both the currently active seat and historical seats with archived
    records — sorted active-first, then by latest_message_time descending.

    See _collect_staff_ids() and the module docstring for how seats are
    identified in the absence of a formal seat-roster source.

    latest_message_time (and therefore active/history ranking) is computed
    from _latest_own_participation_time() — each seat's own sender/recipient
    rows only, never the group-room-expanded set _fetch_messages_for_entity()
    returns. conversation_count still uses the expanded set: that is a
    session-viewing concern (how many threads to show under this seat), not
    a classification concern (RND-132 QA fix — see _latest_own_participation_time
    docstring for why these two must stay separate).
    """
    _, tenant_id = auth
    staff_ids = _collect_staff_ids(db, tenant_id)
    if not staff_ids:
        return []

    display_names = _load_display_names(db, tenant_id)

    seats: list[dict] = []
    for sid in staff_ids:
        latest_message_time = _latest_own_participation_time(db, sid, tenant_id)
        if latest_message_time is None:
            # No direct participation at all for this identity — not a seat
            # worth surfacing (definition requires archived records where
            # the seat is literally the sender or a listed recipient).
            continue
        messages = _fetch_messages_for_entity(db, sid, tenant_id)
        recipients_map = _load_recipients_map(db, tenant_id, [m.id for m in messages])
        conversation_count = len(
            _build_conversation_list(messages, recipients_map, display_names, staff_ids)
        )
        seats.append(
            {
                "staff_id": sid,
                "latest_message_time": latest_message_time,
                "conversation_count": conversation_count,
            }
        )

    if not seats:
        return []

    seats.sort(key=lambda s: (s["latest_message_time"] or 0, s["staff_id"]), reverse=True)

    result = []
    for idx, seat in enumerate(seats):
        sid = seat["staff_id"]
        is_active = idx == 0
        result.append(
            MonitoredAccountOut(
                monitored_account_id=sid,
                staff_id=sid,
                raw_id=sid,
                display_name=resolve_person_display_name(sid, display_names.get(sid)),
                seat_status="active" if is_active else "history",
                is_active_archive_seat=is_active,
                latest_message_time=seat["latest_message_time"],
                conversation_count=seat["conversation_count"],
            )
        )
    return result


@router.get("/api/contacts", response_model=list[ContactOut])
def get_contacts(
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """Return all contacts (non-staff participants) observed in the tenant archive."""
    _, tenant_id = auth
    participant_ids = _collect_archive_participant_ids(db, tenant_id)
    staff_ids = _collect_staff_ids(db, tenant_id)
    contact_ids = participant_ids - staff_ids
    display_names = _load_display_names(db, tenant_id)
    return [
        ContactOut(
            contact_id=cid,
            display_name=resolve_person_display_name(cid, display_names.get(cid)),
            raw_id=cid,
        )
        for cid in sorted(contact_ids)
    ]


@router.get("/api/conversations", response_model=list[ConversationOut])
def get_conversations(
    mode: str = Query(..., description="'staff' or 'contact'"),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """Return conversations for a monitored account (mode=staff) or contact (mode=contact).

    Sorted by last activity descending.
    """
    _, tenant_id = auth

    if mode == "staff":
        if not staff_id:
            raise HTTPException(status_code=400, detail="staff_id is required when mode=staff")
        entity_id = staff_id
    elif mode == "contact":
        if not contact_id:
            raise HTTPException(
                status_code=400, detail="contact_id is required when mode=contact"
            )
        entity_id = contact_id
    else:
        raise HTTPException(status_code=400, detail="mode must be 'staff' or 'contact'")

    messages = _fetch_messages_for_entity(db, entity_id, tenant_id)
    if not messages:
        return []

    recipients_map = _load_recipients_map(db, tenant_id, [m.id for m in messages])
    display_names = _load_display_names(db, tenant_id)
    staff_ids = _collect_staff_ids(db, tenant_id)
    return _build_conversation_list(messages, recipients_map, display_names, staff_ids)


def _fetch_conversation_messages(
    db: Session, conversation_id: str, tenant_id: str
) -> list[ArchiveMessage]:
    """
    Return every ArchiveMessage row belonging to conversation_id, scoped to
    tenant_id. Shared by the timeline route and the media-serving route so
    both use identical, tenant-scoped membership rules — a message is only
    ever considered part of a conversation if this function says so.

    conversation_id formats:
      Group:  <roomid>                          e.g. "after_sales_group_001"
      Direct: "direct__<uid_a>___<uid_b>"       e.g. "direct__contact_zhangsan___staff_yingzi"
    """
    if conversation_id.startswith("direct__"):
        rest = conversation_id[len("direct__"):]
        parts = rest.split("___", 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail="Malformed direct conversation ID")
        uid_a, uid_b = parts

        # ArchiveMessageRecipient.tenant_id == tenant_id (in addition to
        # ArchiveMessage.tenant_id == tenant_id) is required here, not
        # optional: message_id is a global primary key, so a malformed
        # cross-tenant recipient row that happens to share a message_id and
        # receiver_userid with this tenant's data would otherwise let the
        # join manufacture false direct-conversation membership.
        msgs_a_to_b = (
            db.query(ArchiveMessage)
            .join(
                ArchiveMessageRecipient,
                ArchiveMessage.id == ArchiveMessageRecipient.message_id,
            )
            .filter(
                ArchiveMessage.sender == uid_a,
                ArchiveMessageRecipient.receiver_userid == uid_b,
                or_(ArchiveMessage.roomid.is_(None), ArchiveMessage.roomid == ""),
                ArchiveMessage.tenant_id == tenant_id,
                ArchiveMessageRecipient.tenant_id == tenant_id,
            )
            .all()
        )
        msgs_b_to_a = (
            db.query(ArchiveMessage)
            .join(
                ArchiveMessageRecipient,
                ArchiveMessage.id == ArchiveMessageRecipient.message_id,
            )
            .filter(
                ArchiveMessage.sender == uid_b,
                ArchiveMessageRecipient.receiver_userid == uid_a,
                or_(ArchiveMessage.roomid.is_(None), ArchiveMessage.roomid == ""),
                ArchiveMessage.tenant_id == tenant_id,
                ArchiveMessageRecipient.tenant_id == tenant_id,
            )
            .all()
        )
        seen: set[int] = set()
        messages = []
        for msg in msgs_a_to_b + msgs_b_to_a:
            if msg.id not in seen:
                seen.add(msg.id)
                messages.append(msg)
        return messages

    return (
        db.query(ArchiveMessage)
        .filter(
            ArchiveMessage.roomid == conversation_id,
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    )


@router.get(
    "/api/conversations/{conversation_id}/messages",
    response_model=ConversationMessagesOut,
)
def get_conversation_messages(
    conversation_id: str,
    limit: int = Query(20, ge=1, le=100, description="Max messages to return"),
    before: Optional[str] = Query(
        None,
        description=(
            "Opaque pagination cursor from a previous response's "
            "pagination.next_before — pass it back verbatim to load the "
            "next older page. Do not construct this value manually: it is "
            "a compound msgtime:id token (see _decode_message_cursor)."
        ),
    ),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Return a page of the message timeline for a conversation, scoped to the
    session tenant, always in ascending msgtime order (oldest first) so the
    UI can render it directly without re-sorting.

    Default (no `before`): the latest `limit` messages.
    With `before=<cursor>`: the `limit` messages immediately preceding that
    cursor — used to load older history without re-fetching what is already
    rendered. pagination.next_before is the cursor to pass for the next
    "load older" call; pagination.has_older tells the UI whether to show
    that control at all.

    The cursor is a compound (msgtime, id) pair, not a bare msgtime: several
    archive_messages rows can legitimately share the exact same msgtime, and
    a msgtime-only cursor with a strict "<" comparison would silently drop
    every row at the boundary once more than `limit` rows share it. See
    _encode_message_cursor / _decode_message_cursor.

    conversation_id formats:
      Group:  <roomid>                          e.g. "after_sales_group_001"
      Direct: "direct__<uid_a>___<uid_b>"       e.g. "direct__contact_zhangsan___staff_yingzi"
    """
    _, tenant_id = auth

    messages = _fetch_conversation_messages(db, conversation_id, tenant_id)

    if not messages:
        raise HTTPException(status_code=404, detail="Conversation not found")

    recipients_map = _load_recipients_map(db, tenant_id, [m.id for m in messages])
    display_names = _load_display_names(db, tenant_id)

    all_sorted_asc = sorted(messages, key=lambda m: (m.msgtime or 0, m.id))
    if before is not None:
        cursor = _decode_message_cursor(before)
        eligible = [m for m in all_sorted_asc if (m.msgtime or 0, m.id) < cursor]
    else:
        eligible = all_sorted_asc
    page = eligible[-limit:]
    has_older = len(eligible) > limit
    next_before = (
        _encode_message_cursor(page[0].msgtime, page[0].id) if page and has_older else None
    )

    media_files_map = _load_media_files_map(db, tenant_id, [m.id for m in page])

    result = []
    for msg in page:
        recipients = recipients_map.get(msg.id, [])
        media = classify_media(msg.msgtype, bool(getattr(msg, "sdkfileid", None)))

        media_url: Optional[str] = None
        if media.media_type == "image":
            media_file = media_files_map.get(msg.id)
            file_state = "missing"
            if media_file and media_file.download_status == "downloaded":
                # Provider resolved from the row's own storage_backend/
                # storage_ref (RND-174 QA fix) — never the deployment-wide
                # default provider, so a row's servability never changes
                # just because MEDIA_STORAGE_PROVIDER was switched for new
                # writes.
                #
                # Explicit per-exception mapping (RND-174 QA fix #2 — a
                # second QA pass flagged that folding MediaStorageUnavailable
                # into "missing" here misreported a transient Qiniu outage
                # as the media having actually disappeared, i.e.
                # media_file_missing_on_disk). A provider outage or
                # configuration problem degrades only this one row to
                # "unavailable" rather than failing the whole timeline
                # response for every message in the page — the conversation
                # still loads. MediaObjectNotFound (surfaced internally as a
                # "missing" tri-state result, not an exception here — see
                # resolve_image_file_state) is the only case mapped to
                # "missing". The dedicated media route (get_message_media)
                # independently returns 503 for the same outage, for a
                # request that is actually about this one object.
                try:
                    file_state = resolve_media_file_state(media_file)
                except MediaStorageUnavailable:
                    file_state = "unavailable"
                except MediaStorageConfigurationError:
                    # Covers both a genuinely invalid/missing Qiniu config
                    # and an unsupported/unknown storage_backend value on
                    # this row — the timeline has no "crash the whole page"
                    # option, so both degrade this one row to "unavailable"
                    # rather than a 500. The dedicated media route keeps its
                    # own, unchanged 500 behavior for the latter case.
                    file_state = "unavailable"
            media = resolve_image_media_status(
                media,
                media_file.download_status if media_file else None,
                file_state,
            )
            if media.media_status == "available":
                media_url = f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media"

        result.append(
            TimelineMessageOut(
                msgid=msg.msgid,
                sender=msg.sender,
                sender_display_name=(
                    resolve_person_display_name(msg.sender, display_names.get(msg.sender))
                    if msg.sender
                    else None
                ),
                sender_raw_id=msg.sender,
                recipients=recipients,
                recipient_display_names=[
                    resolve_person_display_name(r, display_names.get(r)) for r in recipients
                ],
                recipient_raw_ids=recipients,
                msgtime=msg.msgtime,
                msgtype=msg.msgtype,
                content_text=msg.content_text,
                roomid=msg.roomid,
                decrypt_status=msg.decrypt_status,
                media_type=media.media_type,
                media_status=media.media_status,
                unsupported_reason=media.unsupported_reason,
                media_url=media_url,
            )
        )
    return ConversationMessagesOut(
        messages=result,
        pagination=PaginationOut(has_older=has_older, next_before=next_before),
    )


@router.get("/api/conversations/{conversation_id}/messages/{msgid}/media")
def get_message_media(
    conversation_id: str,
    msgid: str,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Serve an already-downloaded image message's file content (RND-144).

    Authenticated (get_current_user), tenant-scoped (tenant_id comes only
    from the session, never a request param), and conversation-scoped (the
    message must actually belong to conversation_id per
    _fetch_conversation_messages — the same membership rules the timeline
    route uses). The media_files lookup is additionally filtered by
    tenant_id directly (RND-156) — a second, independent check on top of
    message ownership, so a media row can never be served on the strength
    of archive_message_id alone even if message/media tenant assignment
    were ever to diverge. The authorization sequence (authenticate ->
    resolve tenant -> tenant-scoped message/media lookup -> ownership
    check) completes in full *before* any storage provider is selected or
    called — the provider is never part of the authorization boundary
    (RND-174).

    The provider used to serve this row is resolved from the row's own
    storage_backend/storage_ref (RND-174 QA fix), never from the
    deployment-wide MEDIA_STORAGE_PROVIDER default — so this route keeps
    working correctly for both local- and Qiniu-backed rows in the same
    deployment, regardless of which provider is currently configured for
    new writes.

    Response codes:
      404  wrong tenant, wrong conversation, non-image message,
           missing/pending/failed media_files row, unsafe/missing storage
           reference, disallowed file extension, or a confirmed-missing
           remote object.
      503  the storage provider could not confirm the object's state
           (network timeout, auth failure, bucket error, SDK failure) —
           never reported as a plain 404 (RND-174 QA fix: an outage must
           not look like missing media).
      502  a storage operation otherwise failed against a provider that
           did respond.
      500  the row names a storage backend that is unset/unsupported/
           misconfigured.

    Never includes sdkfileid, local_path, storage_ref, oss_key, or any raw
    provider/SDK error detail in the response or in any log line.
    """
    _, tenant_id = auth

    messages = _fetch_conversation_messages(db, conversation_id, tenant_id)
    msg = next((m for m in messages if m.msgid == msgid), None)
    if msg is None:
        raise HTTPException(status_code=404, detail="Not found")

    if msg.msgtype != "image":
        raise HTTPException(status_code=404, detail="Not found")

    media_file = (
        db.query(MediaFile)
        .filter(
            MediaFile.tenant_id == tenant_id,
            MediaFile.archive_message_id == msg.id,
        )
        .first()
    )
    if media_file is None or media_file.download_status != "downloaded":
        raise HTTPException(status_code=404, detail="Not found")

    # Tenant authorization is fully resolved above this line. Only now does
    # provider/storage resolution begin, driven entirely by this row's own
    # fields (RND-174 QA fix — see resolve_effective_storage_reference for
    # the legacy local_path compatibility rule). getattr(..., None): a
    # MediaFile ORM row always has these columns, but duck-typed test
    # doubles may not.
    effective_backend, effective_ref = resolve_effective_storage_reference(
        getattr(media_file, "storage_backend", None),
        getattr(media_file, "storage_ref", None),
        getattr(media_file, "local_path", None),
    )
    if effective_backend is None:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        file_state = resolve_image_file_state(effective_ref, effective_backend)
    except MediaStorageConfigurationError:
        logger.error(
            "media route: storage configuration error (backend=%s)", effective_backend
        )
        raise HTTPException(status_code=500, detail="Media storage is misconfigured")
    except MediaStorageUnavailable:
        logger.warning(
            "media route: storage provider unavailable (backend=%s)", effective_backend
        )
        raise HTTPException(status_code=503, detail="Media storage temporarily unavailable")

    if file_state != "servable":
        raise HTTPException(status_code=404, detail="Not found")

    provider = get_media_storage_provider(effective_backend)

    if provider.supports_local_path():
        safe_path = resolve_servable_image_path(effective_ref, effective_backend)
        if safe_path is None:
            raise HTTPException(status_code=404, detail="Not found")
        content_type = detect_image_content_type(safe_path)
        return FileResponse(path=str(safe_path), media_type=content_type)

    # Cloud-backed media (RND-174): the bucket is private and this route is
    # still the only controlled access path (RND-187 signed-URL/CDN
    # delivery is out of scope here) — fetch the bytes through the provider
    # and proxy them back. The response shape (raw image bytes, same URL,
    # same content-type behavior) stays identical to the local case, so the
    # frontend needs no changes. No Qiniu URL or credential ever reaches
    # the client.
    content_type = detect_image_content_type_for_ref(effective_ref)
    try:
        data = provider.read_bytes(effective_ref)
    except MediaObjectNotFound:
        raise HTTPException(status_code=404, detail="Not found")
    except MediaStorageUnavailable:
        logger.warning(
            "media route: storage provider unavailable during read (backend=%s)",
            effective_backend,
        )
        raise HTTPException(status_code=503, detail="Media storage temporarily unavailable")
    except MediaStorageOperationError:
        logger.error(
            "media route: storage operation failed during read (backend=%s)", effective_backend
        )
        raise HTTPException(status_code=502, detail="Media storage operation failed")
    return Response(content=data, media_type=content_type)
