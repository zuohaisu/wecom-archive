"""Shared conversation timeline service module.

Functions here were precisely MOVED (not reimplemented) from
app.routers.conversations (RND-220) so that conversation resolution, cursor
pagination, timeline projection, revoke fold, and nested media enrichment
orchestration can be tested independently of FastAPI. Keep this file
behavior-equivalent to the original definitions: do NOT change response
shape, ordering, or tenant scoping here. The conversations router imports
resolve_timeline_page (and every helper below) rather than redefining them.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Optional, Tuple
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, load_only

from app.conversation_membership import (
    _fetch_conversation_messages,
    _fetch_conversation_messages_compact,
    _is_valid_roomid,
    _load_display_names,
    _load_display_names_for_ids,
    _load_recipients_map,
)
from app.db.group_chat_metadata import load_group_chat_display_names
from app.db.models import ArchiveMessage, MediaFile, MessageRevocation
from app.display_names import resolve_person_display_name, resolve_room_display_name
from app.media_classification import classify_media, resolve_downloadable_media_status
from app.media_download import NESTED_MEDIA_MSGTYPES, iter_nested_media_refs
from app.media_storage import (
    SUPPORTED_MIGRATION_MEDIA_TYPES,
    MediaStorageConfigurationError,
    MediaStorageUnavailable,
    detect_media_content_type_for_ref,
    resolve_downloadable_media_state,
    resolve_effective_storage_reference,
)
from app.message_type_registry import describe_message_type
from app.revoke_reconciliation import display_status as _revoke_display_status
from app.schemas.timeline import ConversationMessagesOut, PaginationOut, TimelineMessageOut


def attach_group_chat_display_name(
    db: Session,
    tenant_id: str,
    conversation_id: str,
    conversation_type: Optional[str],
    page: ConversationMessagesOut,
) -> ConversationMessagesOut:
    """Attach the authoritative group title/fallback to either timeline path.

    This wrapper intentionally runs after both the current and emergency
    legacy timeline resolvers, so an operational rollback flag cannot bypass
    the RND-340 display contract.
    """
    roomid = next(
        (
            message.roomid
            for message in page.messages
            if _is_valid_roomid(getattr(message, "roomid", None))
        ),
        None,
    )
    if roomid is None and conversation_type == "group":
        roomid = conversation_id
    if not _is_valid_roomid(roomid):
        return page
    display_names = load_group_chat_display_names(db, tenant_id, [roomid])
    page.room_display_name = resolve_room_display_name(roomid, display_names.get(roomid))
    return page


def _entity_context_query_string(
    mode: Optional[str],
    staff_id: Optional[str],
    contact_id: Optional[str],
    conversation_type: Optional[str],
) -> str:
    """RND-158 Phase 2 QA round 7 (blocker 3, media context propagation):
    single place that builds the `mode`/`staff_id`/`contact_id`/
    `conversation_type` query-string suffix shared by every URL that needs
    to carry entity context -- the timeline route's own generated
    `media_url`/`media_access_url` values (see get_conversation_messages)
    today, and any future call site that needs the same four params,
    instead of each one hand-rolling its own string concatenation. Values
    are urlencoded via urllib.parse.urlencode, never string-concatenated
    directly, so a userid containing reserved query characters (e.g. '&',
    '#', '%') round-trips correctly.

    Returns "" (no leading "?") when there is no context to carry (mode is
    None) -- callers append this directly after their own "?...&" query
    string construction, or as the entire query string if the base URL has
    no other params. Mirrors the frontend's timelineEntityQueryParams()
    (backend/app/main.py), which builds the same four params for the
    timeline-fetch call sites; kept as two separate implementations since
    one runs server-side (urlencode) and one client-side (browser
    encodeURIComponent), not because the param set or semantics differ.
    """
    if not mode:
        return ""
    params: dict[str, str] = {"mode": mode}
    if mode == "staff" and staff_id:
        params["staff_id"] = staff_id
    elif mode == "contact" and contact_id:
        params["contact_id"] = contact_id
    if conversation_type:
        params["conversation_type"] = conversation_type
    return "?" + urlencode(params)


def _resolve_entity_context(
    mode: Optional[str], staff_id: Optional[str], contact_id: Optional[str]
) -> Optional[str]:
    """Validate and resolve the optional mode/staff_id/contact_id query
    params into a single entity_id, or raise the same 400 both the
    timeline route and the two media routes have always raised for a
    malformed combination. Extracted (RND-158 Phase 2 QA round 7, blocker
    3) so get_conversation_messages, get_message_media, and
    get_message_media_access -- which all now accept this same optional
    context -- validate it identically instead of three copies of the
    same if/elif/else.
    """
    if mode is None:
        return None
    if mode == "staff":
        if not staff_id:
            raise HTTPException(status_code=400, detail="staff_id is required when mode=staff")
        return staff_id
    if mode == "contact":
        if not contact_id:
            raise HTTPException(status_code=400, detail="contact_id is required when mode=contact")
        return contact_id
    raise HTTPException(status_code=400, detail="mode must be 'staff' or 'contact'")


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


# ---------------------------------------------------------------------------
# Revoke association (RND-201)
#
# The original message's is_revoked/revoked_at live directly on
# ArchiveMessage (set once by app.revoke_reconciliation and never touched
# here), so no join is needed to know THAT a page's message was revoked.
# The MessageRevocation lookup below exists only to (a) recognize and fold
# a standalone "revoke" event row into the original it targets, and (b)
# surface the revoke event's own msgid (revoke_event_msgid) for audit
# purposes on the original's TimelineMessageOut. Status display aging
# (pending -> original_missing) is centralized in
# app.revoke_reconciliation.display_status -- not reimplemented here.
# ---------------------------------------------------------------------------


class _RevocationMaps:
    __slots__ = ("by_revoke_event_message_id", "by_original_message_id")

    def __init__(self) -> None:
        self.by_revoke_event_message_id: dict[int, MessageRevocation] = {}
        self.by_original_message_id: dict[int, MessageRevocation] = {}


def _load_revocations_map(
    db: Session, tenant_id: str, msg_ids: list[int]
) -> _RevocationMaps:
    """Batch-load MessageRevocation rows relevant to this page, keyed both
    ways: by the revoke event's own archive_message_id (to detect/fold a
    standalone revoke row into its original) and by the original
    message's archive_message_id (to attach revoke metadata to an
    already-revoked message). Tenant-scoped explicitly -- same
    defense-in-depth convention as _load_media_files_map: a
    message_revocations row must never be surfaced on the strength of a
    matching archive_message_id alone.

    If two linked revocations ever point at the same original_message_id
    (duplicate/equivalent revoke events, ticket 3.4), the one with the
    earliest revoke_event_msgtime is preferred for revoke_event_msgid
    attribution -- consistent with app.revoke_reconciliation's
    earliest-wins revoked_at rule.
    """
    maps = _RevocationMaps()
    if not msg_ids:
        return maps
    rows = (
        db.query(MessageRevocation)
        .filter(
            MessageRevocation.tenant_id == tenant_id,
            or_(
                MessageRevocation.revoke_event_message_id.in_(msg_ids),
                MessageRevocation.original_message_id.in_(msg_ids),
            ),
        )
        .all()
    )
    for row in rows:
        maps.by_revoke_event_message_id[row.revoke_event_message_id] = row
        if row.original_message_id is not None:
            current = maps.by_original_message_id.get(row.original_message_id)
            if current is None or (row.revoke_event_msgtime or 0) < (
                current.revoke_event_msgtime or 0
            ):
                maps.by_original_message_id[row.original_message_id] = row
    return maps


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(_ensure_aware(value).timestamp() * 1000)


def _load_media_files_by_sdkfileid_map(
    db: Session, tenant_id: str, sdkfileids: set
) -> dict:
    """Return {sdkfileid: MediaFile} for the given sdkfileids, scoped to
    tenant_id — the nested-media analogue of _load_media_files_map, keyed
    by sdkfileid (matching how nested media is actually resolved — see
    _resolve_authorized_nested_media) rather than archive_message_id, so
    one batch query covers every nested media reference across an entire
    timeline page regardless of which message(s) they belong to."""
    if not sdkfileids:
        return {}
    return {
        row.sdkfileid: row
        for row in db.query(MediaFile)
        .filter(MediaFile.tenant_id == tenant_id, MediaFile.sdkfileid.in_(sdkfileids))
        .all()
    }


def _with_variant_thumb(url: str) -> str:
    """Append variant=thumb to an access/proxy URL, preserving any existing
    query string (entity context). RND-207."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}variant=thumb"


def _with_variant_play(url: str) -> str:
    """Append variant=play while preserving entity-context query parameters."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}variant=play"


def _build_nested_media_descriptor(
    media_type: str,
    media_file: Optional[MediaFile],
    conversation_id: str,
    msgid: str,
    path: str,
    media_context_qs: str = "",
) -> dict:
    """Build the safe, public per-node media descriptor attached to a
    mixed/chatrecord structured_content node — {"status", "media_type",
    "mime_type", "size_bytes", "access_url", "thumbnail_access_url",
    "image_width", "image_height"}. Never includes sdkfileid, any MediaFile
    database id, local_path, storage_ref/oss_key, or any storage credential —
    only a status string, the already-known nested item type, content-sniffed
    mime_type, byte size, a same-origin API URL the caller can request an
    access descriptor from (mirroring TimelineMessageOut.media_access_url,
    which is null unless the underlying media is actually available), and
    (RND-207) the nested analogue of the top-level thumbnail fields:
    thumbnail_access_url is set only when a thumbnail was generated for this
    nested image, image_width/image_height only for layout reservation.

    media_context_qs (RND-226): the entity-context query-string suffix
    (mode/staff_id/contact_id/conversation_type) the timeline route
    resolved this conversation with, produced once by
    _entity_context_query_string. Appended to access_url (and, via
    _with_variant_thumb, thumbnail_access_url) so a client following a
    nested access_url preserves the SAME entity context the timeline was
    resolved with — the nested analogue of what
    TimelineMessageOut.media_access_url already does for top-level media.
    Empty ("") for a context-free request, in which case the emitted URLs
    are byte-identical to the pre-RND-226 shape. Only appended to the
    access_url once, before the thumbnail variant is derived, so the
    thumbnail URL correctly ends up as "...?<context>&variant=thumb".
    """
    status = "not_downloaded"
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    access_url: Optional[str] = None
    thumbnail_access_url: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None

    if media_file is not None:
        if media_file.download_status == "downloaded":
            try:
                file_state = resolve_downloadable_media_state(media_file)
            except (MediaStorageUnavailable, MediaStorageConfigurationError):
                file_state = "unavailable"
            status = _NESTED_MEDIA_STATUS_BY_FILE_STATE.get(file_state, "failed")
        elif media_file.download_status == "failed":
            status = "failed"
        # else: "pending" -- stays "not_downloaded", matching the
        # top-level media_status convention for a row that exists but
        # has not completed a download attempt yet.

        if status == "available":
            _backend, effective_ref = resolve_effective_storage_reference(
                getattr(media_file, "storage_backend", None),
                getattr(media_file, "storage_ref", None),
                getattr(media_file, "local_path", None),
            )
            mime_type = detect_media_content_type_for_ref(effective_ref)
            size_bytes = media_file.file_size
            access_url = (
                f"/api/conversations/{conversation_id}/messages/{msgid}"
                f"/nested-media/{path}/access"
                f"{media_context_qs}"
            )
            if (
                getattr(media_file, "thumbnail_status", None) == "generated"
                and getattr(media_file, "thumbnail_ref", None)
            ):
                thumbnail_access_url = _with_variant_thumb(access_url)
            image_width = getattr(media_file, "image_width", None)
            image_height = getattr(media_file, "image_height", None)

    return {
        "status": status,
        "media_type": media_type,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "access_url": access_url,
        "thumbnail_access_url": thumbnail_access_url,
        "image_width": image_width,
        "image_height": image_height,
    }


# resolve_downloadable_media_state()'s tri-state file_state -> the public
# nested-media status vocabulary. Deliberately reuses the SAME vocabulary
# app.media_classification already established for top-level media
# (available/failed/unavailable/not_downloaded) rather than inventing a
# new pending/downloaded/failed/unavailable set — "if the project already
# has a status enum, prefer reusing it" (ticket requirement). This is not
# routed through app.media_classification.resolve_downloadable_media_status
# itself because that function is a documented no-op for media_type
# "emotion" (excluded from SUPPORTED_MIGRATION_MEDIA_TYPES by design — see
# its own docstring) — a nested emotion/sticker reference is just as
# legitimately downloadable via this ticket's media_download extension as
# any other nested type, so this local mapping covers all five
# _NESTED_MEDIA_TYPES-equivalent categories uniformly instead of carrying
# that top-level-only exclusion into the nested contract.
_NESTED_MEDIA_STATUS_BY_FILE_STATE = {
    "servable": "available",
    "missing": "failed",
    "unsupported_type": "failed",
    "unavailable": "unavailable",
}


def _enrich_nested_media_fields(
    fields: dict,
    media_refs: list,
    media_files_by_sdkfileid: dict,
    conversation_id: str,
    msgid: str,
    mode: Optional[str] = None,
    staff_id: Optional[str] = None,
    contact_id: Optional[str] = None,
    roomid: Optional[str] = None,
) -> dict:
    """Return a deep copy of a mixed/chatrecord message's structured_content
    "fields" with every media-bearing node's "media" value replaced by
    the full descriptor from _build_nested_media_descriptor — preserving
    hierarchy, sibling order, sender/timestamp/type, and every other
    per-node key unchanged (never flattened into a second, separately-
    indexed array the caller must correlate).

    Always operates on a deep copy: the input `fields` value is the SAME
    object SQLAlchemy deserialized onto the ORM-mapped structured_content
    JSONB column — mutating it in place risks an unintended write-back on
    a session that later flushes/commits for an unrelated reason. This
    function must never mutate its input.

    mode/staff_id/contact_id (RND-226): the entity-context params the
    timeline request itself was resolved with (bucket/entity-scoped, so
    identical for every message in the page). Forwarded into the
    per-node access_url so a client following it resolves against the SAME
    entity the timeline used.

    conversation_type (RND-226 fix): NOT bucket-level. The timeline's
    bucket-level conversation_type ("group if any message has a roomid")
    cannot be stamped onto every node, because a collision bucket can
    contain BOTH a direct message (no roomid) AND a group message (roomid
    set) — stamping the bucket-level "group" onto a direct message's
    nested access_url makes the nested resolver (which validates
    conversation_type against THAT message's own roomid) correctly reject
    it. Instead conversation_type is derived PER MESSAGE from this
    message's own roomid ("group" if _is_valid_roomid(roomid), else
    "direct") — the exact rule _resolve_authorized_nested_media uses to
    validate the same param back. So a direct image's URL gets
    conversation_type=direct and a group video's gets conversation_type=
    group, both of which the resolver accepts. Computed once per call,
    not per node, then handed to _build_nested_media_descriptor as a
    fully-formed query string via _entity_context_query_string.
    """
    per_message_conversation_type = "group" if _is_valid_roomid(roomid) else "direct"
    media_context_qs = _entity_context_query_string(
        mode, staff_id, contact_id, per_message_conversation_type
    )
    refs_by_path = {r["path"]: r for r in media_refs}
    fields = copy.deepcopy(fields)

    def _walk(node: dict) -> dict:
        if isinstance(node.get("media"), dict):
            # Every media-bearing node is normalized to the full
            # descriptor shape unconditionally -- even when no matching
            # media_refs entry exists (e.g. a historical row decrypted
            # before this ticket's media_refs key existed, or any other
            # data inconsistency). Gating this on "a ref happens to
            # match" would leave such nodes on the OLD {"has_reference":
            # bool} placeholder shape, defeating the point of a stable,
            # single contract shape for every media-bearing node
            # regardless of data vintage (RND-200 QA fix regression
            # guard: test_old_style_structured_content_without_media_
            # refs_key_degrades_safely).
            ref = refs_by_path.get(node.get("path"))
            media_file = (
                media_files_by_sdkfileid.get(ref["sdkfileid"]) if ref is not None else None
            )
            node["media"] = _build_nested_media_descriptor(
                node.get("type"), media_file, conversation_id, msgid, node.get("path"),
                media_context_qs,
            )
        children = node.get("children")
        if isinstance(children, list):
            node["children"] = [_walk(child) for child in children if isinstance(child, dict)]
        return node

    items = fields.get("items")
    if isinstance(items, list):
        fields["items"] = [_walk(item) for item in items if isinstance(item, dict)]
    return fields


# ---------------------------------------------------------------------------
# RND-210 QA FAIL round 2 — public-field projection for structured_content.
# ---------------------------------------------------------------------------
# The server persists the internal `sdkfileid` (and related media references)
# for the media-download path, but these must NEVER reach the public timeline
# JSON. We enforce this with (a) a global deny-set safety net that strips any
# internal-only key from every message's structured fields, and (b) an explicit
# per-type public-field whitelist for the audio types introduced by RND-210 so
# the public payload contains only documented, non-sensitive fields.
INTERNAL_STRUCTURED_FIELD_KEYS = frozenset({"sdkfileid", "corpid", "media_key"})

PUBLIC_STRUCTURED_FIELD_ALLOWLIST = {
    # audio_archive (meeting_voice_call / meetingvoicecall): only the opaque
    # call id, end time, and any filtered shared-doc metadata — never sdkfileid.
    "audio_archive": frozenset({"voiceid", "endtime", "shared_doc"}),
    # audio_doc (voip_doc_share / voipdocshare): shared-document metadata only.
    "audio_doc": frozenset({"title", "url", "docid"}),
}


def _project_public_structured_fields(normalized_type: Optional[str], fields: Any) -> Any:
    """Strip internal-only keys and enforce the per-type public-field whitelist.

    Returns `fields` unchanged when it is not a dict. The internal deny-set is
    always applied (defense-in-depth); the per-type allowlist narrows the
    output further for the audio types so only documented, non-sensitive
    fields reach clients. The server still persists `sdkfileid` internally —
    it is only removed from the *public* serialization here.
    """
    if not isinstance(fields, dict):
        return fields
    projected = {
        k: v for k, v in fields.items() if k not in INTERNAL_STRUCTURED_FIELD_KEYS
    }
    allow = PUBLIC_STRUCTURED_FIELD_ALLOWLIST.get(normalized_type or "")
    if allow is not None:
        projected = {k: v for k, v in projected.items() if k in allow}
    return projected


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


def _thumbnail_timeline_fields(
    media_file: Optional[MediaFile], media_access_url: str
) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """RND-207: derive (thumbnail_access_url, image_width, image_height) for a
    timeline image/emotion row. thumbnail_access_url is set only when a
    thumbnail was actually generated (thumbnail_status="generated" with a
    thumbnail_ref) — otherwise None, so the frontend falls back to the
    original. Dimensions are surfaced whenever recorded, independently of
    thumbnail availability, purely for layout reservation."""
    if media_file is None:
        return None, None, None
    thumbnail_access_url: Optional[str] = None
    if (
        getattr(media_file, "thumbnail_status", None) == "generated"
        and getattr(media_file, "thumbnail_ref", None)
    ):
        thumbnail_access_url = _with_variant_thumb(media_access_url)
    return (
        thumbnail_access_url,
        getattr(media_file, "image_width", None),
        getattr(media_file, "image_height", None),
    )


def _hydrate_timeline_page_messages(
    db: Session, tenant_id: str, page_ids: list[int]
) -> list[ArchiveMessage]:
    """RND-191: fetch full ArchiveMessage rows for exactly the ids on this
    timeline page (typically `limit`, e.g. 20), projected via load_only to
    the columns resolve_timeline_page actually reads. Deliberately excludes
    raw_encrypted_payload/encrypt_random_key/encrypt_chat_msg/
    decrypted_payload -- the encrypted-envelope and raw-decrypt columns
    nothing in the timeline rendering path below reads (content_text/
    structured_content are the already-extracted fields it uses instead).
    Ordered to match page_compact's (msgtime, id) ascending order, since
    callers rely on page[0] for the pagination cursor and on `page`'s
    overall order being oldest-first."""
    if not page_ids:
        return []
    return (
        db.query(ArchiveMessage)
        .options(
            load_only(
                ArchiveMessage.id,
                ArchiveMessage.msgid,
                ArchiveMessage.sender,
                ArchiveMessage.roomid,
                ArchiveMessage.msgtime,
                ArchiveMessage.msgtype,
                ArchiveMessage.content_text,
                ArchiveMessage.decrypt_status,
                ArchiveMessage.sdkfileid,
                ArchiveMessage.structured_content,
                ArchiveMessage.is_revoked,
                ArchiveMessage.revoked_at,
            )
        )
        .filter(
            ArchiveMessage.id.in_(page_ids),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .order_by(ArchiveMessage.msgtime.asc(), ArchiveMessage.id.asc())
        .all()
    )


def resolve_timeline_page(
    db: Session,
    tenant_id: str,
    conversation_id: str,
    *,
    limit: int = 20,
    before: Optional[str] = None,
    mode: Optional[str] = None,
    staff_id: Optional[str] = None,
    contact_id: Optional[str] = None,
    conversation_type: Optional[str] = None,
) -> ConversationMessagesOut:
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

    conversation_type policy (RND-158 Phase 2 QA round 7, blocker 5):
    when supplied, conversation_type is validated as exactly 'direct' or
    'group' (400 otherwise), then checked against the actually-resolved
    bucket's type once messages are fetched -- a mismatch is a 400 (see
    below). It is never used to select or filter messages itself.

    tenant_id is a required explicit parameter, always sourced by the
    caller from get_current_user -- never accepted from a request param.
    """
    # RND-158 Phase 2 (API-contract round): optional entity context,
    # validated with the exact same rule GET /api/conversations already
    # uses (mode given without its matching id is a 400, same message
    # style) -- see _fetch_conversation_messages docstring for how this
    # disambiguates a genuine direct/group collision. Shared validation
    # helper (RND-158 Phase 2 QA round 7): the two media routes below
    # accept this same context and validate it identically.
    entity_id = _resolve_entity_context(mode, staff_id, contact_id)

    # RND-158 Phase 2 QA round 7 (blocker 5, conversation_type policy):
    # validate the value itself up front, independent of whether any
    # messages are found -- garbage input is always a 400, never silently
    # ignored.
    if conversation_type is not None and conversation_type not in ("direct", "group"):
        raise HTTPException(
            status_code=400, detail="conversation_type must be 'direct' or 'group'"
        )

    # RND-191: resolve membership as a compact (id, sender, roomid, msgtime)
    # projection, not full ArchiveMessage rows -- see
    # _fetch_conversation_messages_compact's module note. A conversation
    # with thousands of messages no longer means materializing thousands of
    # rows' raw_encrypted_payload/decrypted_payload/structured_content on
    # every single page request; only the `limit`-sized page actually
    # rendered below is ever hydrated to a full row (_hydrate_timeline_page_
    # messages, after pagination has already picked which ids those are).
    compact_messages = _fetch_conversation_messages_compact(
        db, conversation_id, tenant_id, mode=mode, entity_id=entity_id
    )

    if not compact_messages:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # RND-158 Phase 2 QA round 7 (blocker 5, conversation_type policy):
    # cross-check the caller-supplied hint against the type
    # _fetch_conversation_messages_compact actually resolved for this
    # id/entity context -- group if any resolved message carries a real
    # roomid (_is_valid_roomid, the same truthiness rule
    # _derive_conversation_membership uses), direct otherwise. A mismatch
    # means the caller's cached/stale conversation_type no longer matches
    # this bucket (e.g. an entity-scoped collision resolved to the other
    # side) -- surfaced as 400 rather than silently served under the
    # wrong assumption.
    resolved_conversation_type = (
        "group" if any(_is_valid_roomid(m.roomid) for m in compact_messages) else "direct"
    )
    if conversation_type is not None and conversation_type != resolved_conversation_type:
        raise HTTPException(
            status_code=400,
            detail=(
                f"conversation_type mismatch: requested '{conversation_type}' but this "
                f"conversation resolved to '{resolved_conversation_type}'"
            ),
        )

    # RND-158 Phase 2 QA round 7 (blocker 3, media context propagation):
    # the entity-context query-string suffix (mode/staff_id/contact_id)
    # carried by every media URL this response generates is derived PER
    # MESSAGE below, not once here. conversation_type is DELIBERATELY NOT
    # included in that per-message derivation (RND-226 fix): it must come
    # from each message's own roomid ("group" if _is_valid_roomid(roomid)
    # else "direct"), because a collision bucket can contain both a direct
    # message and a group message and stamping the bucket-level value onto
    # every node would make the resolver (which checks conversation_type
    # against that specific message's roomid) reject the mismatched side.
    # The per-message conversation_type is attached at each media-URL
    # construction site below via _entity_context_query_string(mode,
    # staff_id, contact_id, per_message_type). Empty ("") when no entity
    # context was supplied on this request -- generated media URLs are then
    # byte-identical to previous rounds' behavior.

    all_sorted_asc = sorted(compact_messages, key=lambda m: (m.msgtime or 0, m.id))
    if before is not None:
        cursor = _decode_message_cursor(before)
        eligible = [m for m in all_sorted_asc if (m.msgtime or 0, m.id) < cursor]
    else:
        eligible = all_sorted_asc

    page_compact = eligible[-limit:] if len(eligible) > limit else eligible

    has_older = len(eligible) > limit
    next_before = (
        _encode_message_cursor(page_compact[0].msgtime, page_compact[0].id)
        if page_compact and has_older
        else None
    )

    page = _hydrate_timeline_page_messages(db, tenant_id, [m.id for m in page_compact])

    # RND-191: recipients_map/display_names are scoped to this page's
    # participants only (senders, recipients, and any 名片/business-card
    # referenced contact -- see the card check below), not every
    # participant in the whole conversation -- the whole-conversation
    # unscoped versions this replaced cost more the larger the conversation
    # or the tenant's contact list, for output that only ever needs
    # `limit`-many messages' worth of names.
    recipients_map = _load_recipients_map(db, tenant_id, [m.id for m in page])
    participant_ids: set[str] = set()
    for msg in page:
        if msg.sender:
            participant_ids.add(msg.sender)
        participant_ids.update(recipients_map.get(msg.id, []))
        if msg.msgtype == "card":
            raw_structured = getattr(msg, "structured_content", None)
            if isinstance(raw_structured, dict):
                card_fields = raw_structured.get("fields")
                if isinstance(card_fields, dict) and card_fields.get("userid"):
                    participant_ids.add(card_fields["userid"])
    display_names = _load_display_names_for_ids(db, tenant_id, participant_ids)

    media_files_map = _load_media_files_map(db, tenant_id, [m.id for m in page])
    revocations = _load_revocations_map(db, tenant_id, [m.id for m in page])

    # RND-200 QA fix: batch-load every MediaFile a mixed/chatrecord message
    # on this page could reference via a nested item, keyed by sdkfileid
    # (see _resolve_authorized_nested_media for why sdkfileid, not
    # archive_message_id, is the correct key) — one query for the whole
    # page instead of one per nested message.
    nested_sdkfileids: set = set()
    for msg in page:
        if msg.msgtype in NESTED_MEDIA_MSGTYPES:
            nested_sdkfileids.update(
                ref["sdkfileid"] for ref in iter_nested_media_refs(getattr(msg, "structured_content", None))
            )
    nested_media_files_map = _load_media_files_by_sdkfileid_map(db, tenant_id, nested_sdkfileids)

    result = []
    for msg in page:
        # RND-201: a "revoke" event row that has been successfully linked
        # to its original is never returned as its own timeline row --
        # the original row below carries all the revoke metadata a
        # client needs (is_revoked/revoked_at/revoke_event_msgid). This
        # is the only place a message is dropped from `result` (never for
        # any other msgtype), and it never mutates or deletes the
        # underlying archive_messages row -- the next page fetch still
        # sees it via _fetch_conversation_messages.
        #
        # Pagination note (round 2 QA finding, reviewed and left as-is):
        # `page` above is sliced to `limit` BEFORE this fold happens, so
        # a page containing a linked revoke row can return fewer than
        # `limit` visible messages -- has_older/next_before are computed
        # from `page`'s position in `eligible`, not from the post-fold
        # visible count, and are therefore unaffected and still correct.
        # This is a deliberate, accepted trade-off, not a bug: no message
        # is ever duplicated, skipped, or reordered across a full
        # page-walk (see test_revoke_timeline_api.py's
        # test_pagination_never_duplicates_or_skips_messages_when_a_page_
        # boundary_folds_a_revoke_row). Backfilling each page to always
        # return exactly `limit` visible rows would require turning this
        # fixed-size slice into an unbounded backward scan over
        # `eligible` (new worst-case cost, new cursor-computation edge
        # cases) -- an architectural pagination change judged out of
        # scope for RND-201.
        own_revocation = revocations.by_revoke_event_message_id.get(msg.id)
        if msg.msgtype == "revoke" and own_revocation is not None and own_revocation.status == "linked":
            continue

        recipients = recipients_map.get(msg.id, [])
        media = classify_media(msg.msgtype, bool(getattr(msg, "sdkfileid", None)))
        type_meta = describe_message_type(msg.msgtype)

        structured_content_out: Optional[dict] = None
        raw_structured = getattr(msg, "structured_content", None)
        if isinstance(raw_structured, dict):
            out_fields = raw_structured.get("fields")
            if msg.msgtype in NESTED_MEDIA_MSGTYPES and isinstance(out_fields, dict):
                # Attach safe, request-time media status/access info to
                # every nested node with a media reference — replacing
                # the persisted {"has_reference": bool} placeholder.
                # raw_structured["media_refs"] (the sdkfileid-bearing
                # list) is read here, server-side only, and never itself
                # copied into out_fields/structured_content_out.
                out_fields = _enrich_nested_media_fields(
                    out_fields,
                    raw_structured.get("media_refs") or [],
                    nested_media_files_map,
                    conversation_id,
                    msg.msgid,
                    mode=mode,
                    staff_id=staff_id,
                    contact_id=contact_id,
                    roomid=getattr(msg, "roomid", None),
                )
            # RND-210 QA FAIL round 2: strip internal-only fields (e.g.
            # sdkfileid) and enforce the per-type public-field whitelist so the
            # public timeline JSON NEVER leaks server-internal data. The server
            # still persists sdkfileid (used by the media-download path) — it
            # is only removed from the *public* serialization here.
            if isinstance(out_fields, dict):
                _normalized = (type_meta or {}).get("normalized_type")
                out_fields = _project_public_structured_fields(_normalized, out_fields)
            structured_content_out = {
                "fields": out_fields,
                "parse_warnings": raw_structured.get("parse_warnings", []),
            }

        # RND-210 (+ QA FAIL remediation): resolve a 名片 (business card)
        # contact's display name from the tenant's contacts registry so the
        # timeline shows the real person name (e.g. "张三") instead of the
        # raw WeCom userid (e.g. "contact_zhangsan"). display_names is the
        # tenant-scoped {wecom_userid: name} map already loaded above (line
        # ~1841) for participant labels — reused here, so no extra query.
        # When no Contact row matches, contact_name is left absent and the
        # frontend falls back to the raw userid (never fabricated). A shallow
        # copy avoids mutating the ORM-loaded structured_content attribute.
        if (
            msg.msgtype == "card"
            and isinstance(out_fields, dict)
            and isinstance(display_names, dict)
        ):
            _card_userid = out_fields.get("userid")
            _card_name = display_names.get(_card_userid) if _card_userid else None
            if _card_name:
                out_fields = {**out_fields, "contact_name": _card_name}
                structured_content_out = {
                    "fields": out_fields,
                    "parse_warnings": raw_structured.get("parse_warnings", []),
                }

        media_url: Optional[str] = None
        media_access_url: Optional[str] = None
        # RND-207: populated below only for image/emotion rows that have a
        # generated thumbnail / recorded dimensions; every other message
        # leaves them None.
        thumbnail_access_url: Optional[str] = None
        image_width: Optional[int] = None
        image_height: Optional[int] = None
        if media.media_type in SUPPORTED_MIGRATION_MEDIA_TYPES:
            # RND-199: generalized from "== 'image'" to every media_type
            # classify_media() preserves as media_type — image/video/voice/
            # file (see app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES).
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
                # resolve_downloadable_media_file_state) is the only case
                # mapped to "missing". The dedicated media route
                # (get_message_media) independently returns 503 for the
                # same outage, for a request that is actually about this
                # one object.
                try:
                    file_state = resolve_downloadable_media_state(media_file)
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
            media = resolve_downloadable_media_status(
                media,
                media_file.download_status if media_file else None,
                file_state,
            )
            if media.media_status == "available":
                per_message_type = "group" if _is_valid_roomid(getattr(msg, "roomid", None)) else "direct"
                url_context_qs = _entity_context_query_string(
                    mode, staff_id, contact_id, per_message_type
                )
                media_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media"
                    f"{url_context_qs}"
                )
                media_access_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media/access"
                    f"{url_context_qs}"
                )
                thumbnail_access_url, image_width, image_height = _thumbnail_timeline_fields(
                    media_file, media_access_url
                )
        elif msg.msgtype == "emotion":
            # RND-206: emotion bytes are already servable through the same
            # /media and /media/access routes (see
            # app.media_storage.SERVABLE_MEDIA_MSGTYPES, which includes
            # "emotion" precisely so the media routes could serve it once a
            # renderer existed) — only the timeline response never pointed
            # a client at them, deferred by RND-199 explicitly to this
            # ticket. classify_media() intentionally still leaves
            # media.media_type/media_status as "unsupported" for emotion
            # (unchanged, still tested by
            # test_classify_media_unsupported_msgtype) — `media` itself is
            # not reassigned here. The frontend detects an emotion preview
            # by msgtype=="emotion" plus media_access_url being present,
            # not by media_status.
            media_file = media_files_map.get(msg.id)
            file_state = "missing"
            if media_file and media_file.download_status == "downloaded":
                try:
                    file_state = resolve_downloadable_media_state(media_file)
                except (MediaStorageUnavailable, MediaStorageConfigurationError):
                    file_state = "unavailable"
            if file_state == "servable":
                per_message_type = "group" if _is_valid_roomid(getattr(msg, "roomid", None)) else "direct"
                url_context_qs = _entity_context_query_string(
                    mode, staff_id, contact_id, per_message_type
                )
                media_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media"
                    f"{url_context_qs}"
                )
                media_access_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media/access"
                    f"{url_context_qs}"
                )
                thumbnail_access_url, image_width, image_height = _thumbnail_timeline_fields(
                    media_file, media_access_url
                )

        action: Optional[str] = None
        if msg.msgtype == "sys":
            action = getattr(msg, "action", None)

        # RND-201: revoke association fields. Two, mutually exclusive
        # cases populate these (see TimelineMessageOut docstring) — every
        # other message leaves all four at their None/False default.
        is_revoked = False
        revoked_at: Optional[int] = None
        revoke_event_msgid: Optional[str] = None
        revoke_association_status: Optional[str] = None
        if getattr(msg, "is_revoked", False):
            # This row IS the original message, already marked revoked by
            # app.revoke_reconciliation — content_text/structured_content
            # above are that message's real, unmodified content.
            is_revoked = True
            revoked_at = _datetime_to_epoch_ms(msg.revoked_at)
            revoke_association_status = "linked"
            linking_revocation = revocations.by_original_message_id.get(msg.id)
            if linking_revocation is not None:
                revoke_event_msgid = linking_revocation.revoke_event_msgid
        elif msg.msgtype == "revoke" and own_revocation is not None:
            # This row IS the revoke event itself, surfaced standalone
            # only because it could not be linked to an original (see the
            # skip-and-continue above for the successfully-linked case).
            revoke_event_msgid = msg.msgid
            revoked_at = msg.msgtime
            revoke_association_status = _revoke_display_status(own_revocation)

        result.append(
            TimelineMessageOut(
                msgid=msg.msgid,
                action=action,
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
                media_access_url=media_access_url,
                thumbnail_access_url=thumbnail_access_url,
                image_width=image_width,
                image_height=image_height,
                normalized_type=type_meta["normalized_type"],
                category=type_meta["category"],
                support_status=type_meta["support_status"],
                renderer_strategy=type_meta["renderer_strategy"],
                display_label_key=type_meta["display_label_key"],
                structured_content=structured_content_out,
                is_revoked=is_revoked,
                revoked_at=revoked_at,
                revoke_event_msgid=revoke_event_msgid,
                revoke_association_status=revoke_association_status,
            )
        )
    return ConversationMessagesOut(
        messages=result,
        pagination=PaginationOut(has_older=has_older, next_before=next_before),
    )


# ---------------------------------------------------------------------------
# DEPRECATED short-term rollback facade (RND-220).
#
# Byte-identical snapshot of resolve_timeline_page's body as it existed the
# moment this ticket extracted it out of app.routers.conversations.
# get_conversation_messages switches to this path only when the
# WEARCHIVE_LEGACY_TIMELINE environment variable is set, so a production
# regression in the new call path can be reverted instantly (env var +
# restart, no redeploy). This function is NOT maintained in parallel with
# resolve_timeline_page going forward -- it exists solely as an emergency
# rollback target and should be deleted by a future cleanup ticket once
# resolve_timeline_page has been stable in production.
# ---------------------------------------------------------------------------


def _resolve_timeline_page_legacy(
    db: Session,
    tenant_id: str,
    conversation_id: str,
    *,
    limit: int = 20,
    before: Optional[str] = None,
    mode: Optional[str] = None,
    staff_id: Optional[str] = None,
    contact_id: Optional[str] = None,
    conversation_type: Optional[str] = None,
) -> ConversationMessagesOut:
    """DEPRECATED rollback snapshot -- see module note above. Behavior must
    stay byte-identical to resolve_timeline_page; do not evolve this
    function independently."""
    entity_id = _resolve_entity_context(mode, staff_id, contact_id)

    if conversation_type is not None and conversation_type not in ("direct", "group"):
        raise HTTPException(
            status_code=400, detail="conversation_type must be 'direct' or 'group'"
        )

    messages = _fetch_conversation_messages(
        db, conversation_id, tenant_id, mode=mode, entity_id=entity_id
    )

    if not messages:
        raise HTTPException(status_code=404, detail="Conversation not found")

    resolved_conversation_type = (
        "group" if any(_is_valid_roomid(m.roomid) for m in messages) else "direct"
    )
    if conversation_type is not None and conversation_type != resolved_conversation_type:
        raise HTTPException(
            status_code=400,
            detail=(
                f"conversation_type mismatch: requested '{conversation_type}' but this "
                f"conversation resolved to '{resolved_conversation_type}'"
            ),
        )

    recipients_map = _load_recipients_map(db, tenant_id, [m.id for m in messages])
    display_names = _load_display_names(db, tenant_id)

    all_sorted_asc = sorted(messages, key=lambda m: (m.msgtime or 0, m.id))
    if before is not None:
        cursor = _decode_message_cursor(before)
        eligible = [m for m in all_sorted_asc if (m.msgtime or 0, m.id) < cursor]
    else:
        eligible = all_sorted_asc

    page = eligible[-limit:] if len(eligible) > limit else eligible

    has_older = len(eligible) > limit
    next_before = (
        _encode_message_cursor(page[0].msgtime, page[0].id) if page and has_older else None
    )

    media_files_map = _load_media_files_map(db, tenant_id, [m.id for m in page])
    revocations = _load_revocations_map(db, tenant_id, [m.id for m in page])

    nested_sdkfileids: set = set()
    for msg in page:
        if msg.msgtype in NESTED_MEDIA_MSGTYPES:
            nested_sdkfileids.update(
                ref["sdkfileid"] for ref in iter_nested_media_refs(getattr(msg, "structured_content", None))
            )
    nested_media_files_map = _load_media_files_by_sdkfileid_map(db, tenant_id, nested_sdkfileids)

    result = []
    for msg in page:
        own_revocation = revocations.by_revoke_event_message_id.get(msg.id)
        if msg.msgtype == "revoke" and own_revocation is not None and own_revocation.status == "linked":
            continue

        recipients = recipients_map.get(msg.id, [])
        media = classify_media(msg.msgtype, bool(getattr(msg, "sdkfileid", None)))
        type_meta = describe_message_type(msg.msgtype)

        structured_content_out: Optional[dict] = None
        raw_structured = getattr(msg, "structured_content", None)
        if isinstance(raw_structured, dict):
            out_fields = raw_structured.get("fields")
            if msg.msgtype in NESTED_MEDIA_MSGTYPES and isinstance(out_fields, dict):
                out_fields = _enrich_nested_media_fields(
                    out_fields,
                    raw_structured.get("media_refs") or [],
                    nested_media_files_map,
                    conversation_id,
                    msg.msgid,
                    mode=mode,
                    staff_id=staff_id,
                    contact_id=contact_id,
                    roomid=getattr(msg, "roomid", None),
                )
            if isinstance(out_fields, dict):
                _normalized = (type_meta or {}).get("normalized_type")
                out_fields = _project_public_structured_fields(_normalized, out_fields)
            structured_content_out = {
                "fields": out_fields,
                "parse_warnings": raw_structured.get("parse_warnings", []),
            }

        if (
            msg.msgtype == "card"
            and isinstance(out_fields, dict)
            and isinstance(display_names, dict)
        ):
            _card_userid = out_fields.get("userid")
            _card_name = display_names.get(_card_userid) if _card_userid else None
            if _card_name:
                out_fields = {**out_fields, "contact_name": _card_name}
                structured_content_out = {
                    "fields": out_fields,
                    "parse_warnings": raw_structured.get("parse_warnings", []),
                }

        media_url: Optional[str] = None
        media_access_url: Optional[str] = None
        thumbnail_access_url: Optional[str] = None
        image_width: Optional[int] = None
        image_height: Optional[int] = None
        if media.media_type in SUPPORTED_MIGRATION_MEDIA_TYPES:
            media_file = media_files_map.get(msg.id)
            file_state = "missing"
            if media_file and media_file.download_status == "downloaded":
                try:
                    file_state = resolve_downloadable_media_state(media_file)
                except MediaStorageUnavailable:
                    file_state = "unavailable"
                except MediaStorageConfigurationError:
                    file_state = "unavailable"
            media = resolve_downloadable_media_status(
                media,
                media_file.download_status if media_file else None,
                file_state,
            )
            if media.media_status == "available":
                per_message_type = "group" if _is_valid_roomid(getattr(msg, "roomid", None)) else "direct"
                url_context_qs = _entity_context_query_string(
                    mode, staff_id, contact_id, per_message_type
                )
                media_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media"
                    f"{url_context_qs}"
                )
                media_access_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media/access"
                    f"{url_context_qs}"
                )
                thumbnail_access_url, image_width, image_height = _thumbnail_timeline_fields(
                    media_file, media_access_url
                )
        elif msg.msgtype == "emotion":
            media_file = media_files_map.get(msg.id)
            file_state = "missing"
            if media_file and media_file.download_status == "downloaded":
                try:
                    file_state = resolve_downloadable_media_state(media_file)
                except (MediaStorageUnavailable, MediaStorageConfigurationError):
                    file_state = "unavailable"
            if file_state == "servable":
                per_message_type = "group" if _is_valid_roomid(getattr(msg, "roomid", None)) else "direct"
                url_context_qs = _entity_context_query_string(
                    mode, staff_id, contact_id, per_message_type
                )
                media_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media"
                    f"{url_context_qs}"
                )
                media_access_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media/access"
                    f"{url_context_qs}"
                )
                thumbnail_access_url, image_width, image_height = _thumbnail_timeline_fields(
                    media_file, media_access_url
                )

        action: Optional[str] = None
        if msg.msgtype == "sys":
            action = getattr(msg, "action", None)

        is_revoked = False
        revoked_at: Optional[int] = None
        revoke_event_msgid: Optional[str] = None
        revoke_association_status: Optional[str] = None
        if getattr(msg, "is_revoked", False):
            is_revoked = True
            revoked_at = _datetime_to_epoch_ms(msg.revoked_at)
            revoke_association_status = "linked"
            linking_revocation = revocations.by_original_message_id.get(msg.id)
            if linking_revocation is not None:
                revoke_event_msgid = linking_revocation.revoke_event_msgid
        elif msg.msgtype == "revoke" and own_revocation is not None:
            revoke_event_msgid = msg.msgid
            revoked_at = msg.msgtime
            revoke_association_status = _revoke_display_status(own_revocation)

        result.append(
            TimelineMessageOut(
                msgid=msg.msgid,
                action=action,
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
                media_access_url=media_access_url,
                thumbnail_access_url=thumbnail_access_url,
                image_width=image_width,
                image_height=image_height,
                normalized_type=type_meta["normalized_type"],
                category=type_meta["category"],
                support_status=type_meta["support_status"],
                renderer_strategy=type_meta["renderer_strategy"],
                display_label_key=type_meta["display_label_key"],
                structured_content=structured_content_out,
                is_revoked=is_revoked,
                revoked_at=revoked_at,
                revoke_event_msgid=revoke_event_msgid,
                revoke_association_status=revoke_association_status,
            )
        )
    return ConversationMessagesOut(
        messages=result,
        pagination=PaginationOut(has_older=has_older, next_before=next_before),
    )
