"""Response schemas for the monitored-accounts / contacts / conversation-list
endpoints (RND-219 — moved verbatim out of app.routers.conversations, no
field/type/default changes), plus ConversationParticipantOut/
ConversationDetailOut (Archive Console v2 design import — additive)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class MonitoredAccountOut(BaseModel):
    monitored_account_id: str
    display_name: str
    staff_id: str
    raw_id: str
    seat_status: str  # "active" | "history" | "unknown"
    is_active_archive_seat: bool
    latest_message_time: Optional[int] = None
    # The review console does not render this value on its initial member
    # picker, so that latency-sensitive path may explicitly omit the costly
    # aggregate.  The default API path still returns an integer.
    conversation_count: Optional[int] = None


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


class ConversationParticipantOut(BaseModel):
    """One participant INFERRED from this conversation's archived messages
    (every distinct sender/recipient observed among them) -- never a live
    WeCom room roster, which this system has no sync for. See
    GET /api/conversations/{conversation_id}/detail."""

    id: str
    raw_id: str
    display_name: str
    role: str  # "staff" | "contact"


class ConversationDetailOut(BaseModel):
    """Archive Console v2 (design import) -- conversation-level stats and
    inferred participants for the review console's 会话信息 panel. Additive:
    does not change ConversationOut or any other existing response shape."""

    conversation_id: str
    message_count: int
    decrypted_percent: float
    participants: list[ConversationParticipantOut]
