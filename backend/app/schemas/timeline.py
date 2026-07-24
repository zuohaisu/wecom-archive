"""Response schemas for the conversation message timeline endpoint (RND-220
— moved verbatim out of app.routers.conversations, no field/type/default
changes).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class TimelineMessageOut(BaseModel):
    msgid: str
    action: Optional[str] = None  # RND-198: WeCom ChatData action field (used for sys msgtype)
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
    media_access_url: Optional[str] = None
    # RND-207: list/timeline thumbnail. thumbnail_access_url is the same
    # authenticated /media/access endpoint with ?variant=thumb, present only
    # when a generated thumbnail exists for this image/emotion row — the
    # frontend loads it in the list and falls back to media_access_url (the
    # original) when it is null; the viewer always opens media_access_url.
    # image_width/image_height are the original's post-EXIF pixel dimensions,
    # used only to reserve an aspect-ratio box before the thumbnail loads
    # (layout-shift fix); null on rows with no recorded dimensions.
    thumbnail_access_url: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    # RND-197: Message Type Registry metadata, exposed so the frontend can
    # dispatch to a structured card renderer without re-deriving any of
    # this from msgtype itself (see app.message_type_registry, the single
    # source of truth for all five fields below).
    normalized_type: str
    category: str
    support_status: str
    renderer_strategy: str
    display_label_key: str
    # Parsed fields only (structured_message_parser.py's "fields" +
    # "parse_warnings") -- the type-specific raw sub-payload is preserved
    # server-side (ArchiveMessage.structured_content.raw) but never
    # serialized here; the raw WeCom payload is not exposed to frontend
    # users as normal content (ticket security requirement).
    structured_content: Optional[dict] = None
    # RND-201: revoke association. is_revoked/revoked_at are set ONLY on
    # an ORIGINAL message row that a matching revoke event has been
    # linked to -- content_text/structured_content above remain that
    # message's real, preserved content; nothing about this message's
    # rendering path changes. revoke_association_status is set on BOTH
    # kinds of row this can appear on:
    #   - an original message that has been revoked: "linked" (the only
    #     value is_revoked=True ever pairs with).
    #   - a standalone "revoke" event row whose target could not be
    #     resolved: "pending" (target not archived yet, still being
    #     retried), "original_missing" (same, but past the display-only
    #     aging threshold), or "malformed" (the event's own payload had
    #     no usable target reference). A standalone revoke event that WAS
    #     successfully linked is never returned as its own row -- it is
    #     folded into the original above, and revoke_event_msgid there is
    #     how a client can still discover/audit which event revoked it.
    # A message untouched by any revoke event has all four None/False.
    is_revoked: bool = False
    revoked_at: Optional[int] = None
    revoke_event_msgid: Optional[str] = None
    revoke_association_status: Optional[str] = None


class PaginationOut(BaseModel):
    has_older: bool
    next_before: Optional[str] = None


class ConversationMessagesOut(BaseModel):
    messages: list[TimelineMessageOut]
    pagination: PaginationOut
