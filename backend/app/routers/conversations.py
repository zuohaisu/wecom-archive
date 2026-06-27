"""
Conversation aggregation APIs for the 365 WeCom Archive review console.

Aggregates raw archive_messages + archive_message_recipients into
first-class Conversation objects so the frontend never has to infer
conversations from raw messages.

Conversation identity:
  Group  → conversation_id = roomid
  Direct → conversation_id = "direct__<uid_a>___<uid_b>"  (sorted, triple-underscore separator)

Monitored-account detection:
  Any sender or recipient whose ID starts with "staff_" is a monitored account.
  All other participants are contacts.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact
from app.db.session import get_db

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_staff(uid: str) -> bool:
    return uid.startswith("staff_")


def _direct_conv_id(uid_a: str, uid_b: str) -> str:
    """Stable conversation ID for a 1:1 pair regardless of sender/receiver order."""
    a, b = sorted([uid_a, uid_b])
    return f"direct__{a}___{b}"


def _load_display_names(db: Session) -> dict[str, str]:
    """Return {wecom_userid: display_name} from the contacts table."""
    return {c.wecom_userid: (c.name or c.wecom_userid) for c in db.query(Contact).all()}


def _load_recipients_map(db: Session, msg_ids: list[int]) -> dict[int, list[str]]:
    """Return {message_id: [receiver_userid, ...]} for the given message primary-key IDs."""
    if not msg_ids:
        return {}
    result: dict[int, list[str]] = {}
    for r in (
        db.query(ArchiveMessageRecipient)
        .filter(ArchiveMessageRecipient.message_id.in_(msg_ids))
        .all()
    ):
        result.setdefault(r.message_id, []).append(r.receiver_userid)
    return result


def _fetch_messages_for_entity(db: Session, entity_id: str) -> list:
    """
    Return all messages that belong to conversations involving entity_id.

    Strategy:
    - Find message IDs where entity_id is sender or recipient.
    - From those, collect group roomids and expand to ALL messages in those rooms
      (for full group context even when entity isn't listed as recipient on every row).
    - Direct messages are included as-is (every direct message directly involves the entity).
    """
    # Message IDs where entity appears as sender
    sender_ids: set[int] = {
        row[0]
        for row in db.query(ArchiveMessage.id)
        .filter(ArchiveMessage.sender == entity_id)
        .all()
    }
    # Message IDs where entity appears as recipient
    recipient_ids: set[int] = {
        row[0]
        for row in db.query(ArchiveMessageRecipient.message_id)
        .filter(ArchiveMessageRecipient.receiver_userid == entity_id)
        .all()
    }
    seed_ids = sender_ids | recipient_ids
    if not seed_ids:
        return []

    # Load seed messages to identify group rooms
    seed_msgs = db.query(ArchiveMessage).filter(ArchiveMessage.id.in_(seed_ids)).all()
    group_rooms = {m.roomid for m in seed_msgs if m.roomid}

    # Build full message set
    final_ids: set[int] = set()

    if group_rooms:
        for row in (
            db.query(ArchiveMessage.id)
            .filter(ArchiveMessage.roomid.in_(group_rooms))
            .all()
        ):
            final_ids.add(row[0])

    # Direct messages are already fully described by seed
    direct_ids = {m.id for m in seed_msgs if not m.roomid}
    final_ids.update(direct_ids)

    if not final_ids:
        return []

    return db.query(ArchiveMessage).filter(ArchiveMessage.id.in_(final_ids)).all()


def _build_conversation_list(
    messages: list,
    recipients_map: dict[int, list[str]],
    display_names: dict[str, str],
) -> list[dict]:
    """
    Aggregate a flat message list into conversation summary objects.

    Returns a list sorted by last_message_time descending (most recent first).
    """
    convs: dict[str, dict] = {}

    for msg in messages:
        sender = msg.sender or ""
        roomid = msg.roomid or ""
        recipients = recipients_map.get(msg.id, [])
        all_parties = {p for p in ({sender} | set(recipients)) if p}
        staff_set = {p for p in all_parties if _is_staff(p)}
        contact_set = {p for p in all_parties if not _is_staff(p)}

        if roomid:
            conv_id = roomid
            conv_type = "group"
        else:
            if staff_set and contact_set:
                conv_id = _direct_conv_id(sorted(staff_set)[0], sorted(contact_set)[0])
            else:
                # Fallback: two non-staff or two staff talking directly
                parts = sorted(all_parties)
                if len(parts) >= 2:
                    conv_id = f"direct__{parts[0]}___{parts[1]}"
                else:
                    conv_id = f"direct__{parts[0] if parts else 'unknown'}"
            conv_type = "direct"

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

        if data["conversation_type"] == "group":
            display_name = data["roomid"] or conv_id
        else:
            cids = sorted(data["contact_ids"])
            if cids:
                display_name = display_names.get(cids[0], cids[0])
            else:
                sids = sorted(data["monitored_account_ids"])
                display_name = display_names.get(sids[0], sids[0]) if sids else conv_id

        result.append(
            {
                "conversation_id": conv_id,
                "conversation_type": data["conversation_type"],
                "display_name": display_name,
                "roomid": data["roomid"],
                "monitored_account_ids": sorted(data["monitored_account_ids"]),
                "contact_ids": sorted(data["contact_ids"]),
                "last_message_time": latest.msgtime,
                "last_message_text": (latest.content_text or "")[:200],
                "message_count": len(msgs_sorted),
                "latest_sender_id": latest.sender,
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


class ContactOut(BaseModel):
    contact_id: str
    display_name: str


class ConversationOut(BaseModel):
    conversation_id: str
    conversation_type: str
    display_name: str
    roomid: Optional[str] = None
    monitored_account_ids: list[str]
    contact_ids: list[str]
    last_message_time: Optional[int] = None
    last_message_text: Optional[str] = None
    message_count: int
    latest_sender_id: Optional[str] = None
    review_status: Optional[str] = None
    ai_status: Optional[str] = None
    ai_summary: Optional[str] = None


class TimelineMessageOut(BaseModel):
    msgid: str
    sender: Optional[str] = None
    recipients: list[str]
    msgtime: Optional[int] = None
    msgtype: Optional[str] = None
    content_text: Optional[str] = None
    roomid: Optional[str] = None
    decrypt_status: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/monitored-accounts", response_model=list[MonitoredAccountOut])
def get_monitored_accounts(db: Session = Depends(get_db)):
    """Return all monitored accounts inferred from archive data (sender/recipient IDs starting with 'staff_')."""
    sender_rows = (
        db.query(ArchiveMessage.sender)
        .filter(ArchiveMessage.sender.like("staff_%"))
        .distinct()
        .all()
    )
    recipient_rows = (
        db.query(ArchiveMessageRecipient.receiver_userid)
        .filter(ArchiveMessageRecipient.receiver_userid.like("staff_%"))
        .distinct()
        .all()
    )
    staff_ids = {row[0] for row in sender_rows + recipient_rows if row[0]}
    display_names = _load_display_names(db)
    return [
        MonitoredAccountOut(
            monitored_account_id=sid,
            display_name=display_names.get(sid, sid),
        )
        for sid in sorted(staff_ids)
    ]


@router.get("/api/contacts", response_model=list[ContactOut])
def get_contacts(db: Session = Depends(get_db)):
    """Return all contacts (non-staff participants) observed in the archive dataset."""
    sender_rows = (
        db.query(ArchiveMessage.sender)
        .filter(
            ArchiveMessage.sender.isnot(None),
            ~ArchiveMessage.sender.like("staff_%"),
        )
        .distinct()
        .all()
    )
    recipient_rows = (
        db.query(ArchiveMessageRecipient.receiver_userid)
        .filter(~ArchiveMessageRecipient.receiver_userid.like("staff_%"))
        .distinct()
        .all()
    )
    contact_ids = {row[0] for row in sender_rows + recipient_rows if row[0]}
    display_names = _load_display_names(db)
    return [
        ContactOut(
            contact_id=cid,
            display_name=display_names.get(cid, cid),
        )
        for cid in sorted(contact_ids)
    ]


@router.get("/api/conversations", response_model=list[ConversationOut])
def get_conversations(
    mode: str = Query(..., description="'staff' or 'contact'"),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    db: Session = Depends(get_db),
):
    """
    Return conversations for a monitored account (mode=staff) or contact (mode=contact).

    Sorted by last activity descending.
    """
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

    messages = _fetch_messages_for_entity(db, entity_id)
    if not messages:
        return []

    recipients_map = _load_recipients_map(db, [m.id for m in messages])
    display_names = _load_display_names(db)
    return _build_conversation_list(messages, recipients_map, display_names)


@router.get(
    "/api/conversations/{conversation_id}/messages",
    response_model=list[TimelineMessageOut],
)
def get_conversation_messages(conversation_id: str, db: Session = Depends(get_db)):
    """
    Return the ordered message timeline for a conversation.

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
            )
            .all()
        )
        seen: set[int] = set()
        messages = []
        for msg in msgs_a_to_b + msgs_b_to_a:
            if msg.id not in seen:
                seen.add(msg.id)
                messages.append(msg)
    else:
        messages = (
            db.query(ArchiveMessage)
            .filter(ArchiveMessage.roomid == conversation_id)
            .all()
        )

    if not messages:
        raise HTTPException(status_code=404, detail="Conversation not found")

    recipients_map = _load_recipients_map(db, [m.id for m in messages])

    return [
        TimelineMessageOut(
            msgid=msg.msgid,
            sender=msg.sender,
            recipients=recipients_map.get(msg.id, []),
            msgtime=msg.msgtime,
            msgtype=msg.msgtype,
            content_text=msg.content_text,
            roomid=msg.roomid,
            decrypt_status=msg.decrypt_status,
        )
        for msg in sorted(messages, key=lambda m: m.msgtime or 0)
    ]
