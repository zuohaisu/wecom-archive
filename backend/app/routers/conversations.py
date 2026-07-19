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

import copy
import logging
import re
from types import SimpleNamespace
from typing import Callable, Optional, Tuple
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from datetime import datetime, timedelta, timezone

from app.auth import get_current_user
from app.db.models import (
    AdminUser,
    ArchiveMessage,
    ArchiveMessageRecipient,
    Contact,
    MediaFile,
    MessageRevocation,
)
from app.db.session import get_db
from app.display_names import resolve_person_display_name, resolve_room_display_name
from app.media_classification import (
    classify_media,
    resolve_downloadable_media_status,
)
from app.media_download import NESTED_MEDIA_MSGTYPES, iter_nested_media_refs
from app.message_type_registry import describe_message_type
from app.revoke_reconciliation import display_status as _revoke_display_status
from app.media_storage import (
    SERVABLE_MEDIA_MSGTYPES,
    SUPPORTED_MIGRATION_MEDIA_TYPES,
    MediaObjectNotFound,
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageUnavailable,
    detect_media_content_type_for_ref,
    get_media_storage_provider,
    get_signed_url_ttl_seconds,
    object_key_tenant_prefix_matches,
    resolve_downloadable_media_file_state,
    resolve_downloadable_media_state,
    resolve_effective_storage_reference,
    resolve_servable_downloadable_media_path,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache-Control: no-store for the media access descriptor endpoint
#
# get_message_media_access() sets response.headers["Cache-Control"] on its
# own success path, but that mutation only reaches the client when the route
# body actually returns a value. Every error path — an HTTPException raised
# inside the route body (404/500/502/503), *and* a 401 raised by
# get_current_user() while resolving dependencies, which runs before the
# route body and therefore never touches that Response object at all — goes
# through FastAPI/Starlette's own exception-to-Response conversion instead,
# which builds a brand-new Response from scratch and knows nothing about
# headers set earlier on the request-scoped Response object.
#
# A try/except around the route body would still miss the get_current_user
# 401 case (it fails before the body starts). This middleware instead
# inspects the fully-built outgoing Response for every request — after
# Starlette's ExceptionMiddleware has already converted any exception
# (dependency-resolution or route-body) into a concrete status code — and
# adds/overwrites the header there. It is scoped by an exact path-pattern
# match so no other endpoint's caching behavior changes.
# ---------------------------------------------------------------------------

_MEDIA_ACCESS_PATH_RE = re.compile(
    r"^/api/conversations/[^/]+/messages/[^/]+/(media|nested-media/[^/]+)/access$"
)


class MediaAccessNoStoreMiddleware(BaseHTTPMiddleware):
    """Ensures Cache-Control: no-store on every response — success or error,
    any status code — for GET .../media/access and (RND-200/201) GET
    .../nested-media/{item_path}/access, which shares the exact same
    no-time-boxed-credential-must-never-be-cached rationale (RND-187). See
    the module comment above for why this can't be done from inside the
    route alone."""

    async def dispatch(self, request: Request, call_next: Callable):
        response = await call_next(request)
        if _MEDIA_ACCESS_PATH_RE.match(request.url.path):
            response.headers["Cache-Control"] = "no-store"
        return response


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


def _staff_ids_for_participants(
    db: Session, tenant_id: str, participant_ids: set[str]
) -> set[str]:
    """RND-158 Phase 2: scoped counterpart to _collect_staff_ids(), used
    ONLY by get_conversations(). Profiling showed _collect_staff_ids()'s two
    tenant-wide, unscoped ``.distinct()`` scans (over every ArchiveMessage
    sender and every ArchiveMessageRecipient receiver_userid in the tenant)
    dominate when the queried entity itself has few messages but the tenant
    has many contacts — see RND-158 Phase 2 benchmark notes.

    get_conversations() only ever calls is_staff() on ids that appear as a
    sender or recipient within the specific message set it just fetched
    (participant_ids) — never on any other id. Any such id is, by
    construction, already "observed as a sender/recipient in this tenant's
    archive" (it came from a tenant_id-scoped query against those very
    tables), so _collect_archive_participant_ids()'s tenant-wide scan is
    redundant work for this call site: checking admin_user_ids membership
    directly against participant_ids is equivalent to
    _collect_staff_ids()'s ``admin_user_ids & participant_ids`` (the global
    version), restricted to the ids that matter here, without ever paying
    for the full-tenant scan.
    """
    if not participant_ids:
        return set()
    prefix_ids = {p for p in participant_ids if _is_staff(p)}
    admin_user_rows = (
        db.query(AdminUser.wecom_user_id)
        .filter(
            AdminUser.tenant_id == tenant_id,
            AdminUser.wecom_user_id.in_(participant_ids),
        )
        .distinct()
        .all()
    )
    admin_ids = {row[0] for row in admin_user_rows if row[0]}
    return prefix_ids | (admin_ids & participant_ids)


def _load_display_names_for_ids(
    db: Session, tenant_id: str, wecom_userids: set[str]
) -> dict[str, Optional[str]]:
    """RND-158 Phase 2: scoped counterpart to _load_display_names(), used
    ONLY by get_conversations(). _load_display_names() loads every Contact
    row for the tenant on every call, unscoped to the entity being viewed —
    profiling showed this is material for tenants with many contacts (see
    RND-158 Phase 2 benchmark notes). get_conversations() only ever looks
    up display names for ids that are participants (sender/recipient) of
    the message set it just fetched, so scoping the query to exactly those
    ids returns the identical subset of {wecom_userid: name} entries
    _load_display_names() would have returned, just without materializing
    Contact rows for every other contact in the tenant.
    """
    if not wecom_userids:
        return {}
    return {
        c.wecom_userid: c.name
        for c in db.query(Contact)
        .filter(Contact.tenant_id == tenant_id, Contact.wecom_userid.in_(wecom_userids))
        .all()
    }


def _direct_conv_id(uid_a: str, uid_b: str) -> str:
    """Stable conversation ID for a 1:1 pair regardless of sender/receiver order."""
    a, b = sorted([uid_a, uid_b])
    return f"direct__{a}___{b}"


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


# ---------------------------------------------------------------------------
# Nested media (mixed/chatrecord, RND-200 QA fix / RND-201)
#
# A mixed/chatrecord message's structured_content.fields tree carries a
# "media": {"has_reference": bool} placeholder per node (see
# app.structured_message_parser) — enough to know a nested item HAS
# downloadable media, but not enough for a consumer to determine its
# status or actually fetch it. This section attaches a full, safe,
# request-time-computed media descriptor to the correct node in place
# (never a second array the frontend must index-correlate), and exposes a
# tenant-scoped, path-validated nested-media access route reusing every
# existing RND-199 storage/auth primitive.
# ---------------------------------------------------------------------------

# Path grammar: a dot-separated chain of small non-negative integers,
# e.g. "0", "0.1", "3.0.2" — exactly the same value
# app.structured_message_parser._parse_nested_item already assigns each
# node as its "path" field (and the same value app.media_download's
# media_refs entries key on), reused verbatim as the URL identifier
# rather than inventing a second addressing scheme (e.g. "items.2.
# children.0") for the same, already-homogeneous tree: every level of a
# mixed/chatrecord tree is a list of the same node shape, so the
# "items"/"children" key-name segments the ticket's example grammar uses
# carry no additional addressing information over a bare index chain.
#
# No eval, no arbitrary attribute traversal, no filesystem mapping: this
# regex is the ENTIRE grammar, and resolution (_find_nested_media_ref)
# is a single exact-string-equality lookup against the flat, already-
# computed media_refs list — the path string is never used to index into
# a live Python structure, walk an object graph, or build a filesystem
# path.
#
# Segment count is capped at _MIXED_MAX_DEPTH + 1 (one segment per
# recursion level 0..depth, inclusive) and each segment at 6 digits
# (comfortably above _MIXED_ITEM_CAP=200, the largest index that could
# ever legitimately appear, while still bounding absurd input) — both
# constants are mirrored locally (not imported) to avoid a new
# conversations.py -> structured_message_parser dependency; a dedicated
# test (test_nested_media_path_segment_cap_matches_parser_max_depth)
# keeps the two in sync instead.
_NESTED_MEDIA_MAX_PATH_SEGMENTS = 9  # mirrors structured_message_parser._MIXED_MAX_DEPTH + 1
_NESTED_MEDIA_MAX_PATH_LENGTH = 64
_NESTED_MEDIA_PATH_RE = re.compile(
    r"^\d{1,6}(?:\.\d{1,6}){0,8}$"  # {0,8} = up to 8 additional segments -> 9 total
)

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


def _validate_nested_media_path(raw: str) -> Optional[str]:
    """Validate a caller-supplied nested-media item path (untrusted URL
    input) against the strict grammar above, returning the path unchanged
    (it is already canonical — no normalization needed) or None for
    anything malformed: wrong characters (rejects path traversal — "..",
    "/", null bytes, letters — since only digits and "." ever match),
    too many segments (recursion-depth violation), or too long overall
    (defense in depth on top of the regex's own implicit bound).

    This function alone decides "well-formed enough to attempt a lookup"
    — it does NOT confirm the path resolves to an actual node (that is
    _find_nested_media_ref's job, against THIS message's own data only).
    """
    if not isinstance(raw, str) or not raw:
        return None
    if len(raw) > _NESTED_MEDIA_MAX_PATH_LENGTH:
        return None
    if raw.count(".") > _NESTED_MEDIA_MAX_PATH_SEGMENTS - 1:
        return None
    if not _NESTED_MEDIA_PATH_RE.match(raw):
        return None
    return raw


def _find_nested_media_ref(structured_content, path: str) -> Optional[dict]:
    """Resolve a validated path to its media_refs entry for THIS message
    only — {"path", "type", "sdkfileid"} — or None if the path does not
    exist, is not media-bearing, or belongs to a different message
    entirely. iter_nested_media_refs (app.media_download) already
    filters to only well-formed entries whose type is a downloadable
    category, so a single exact-match lookup here simultaneously
    satisfies every one of the ticket's "reject: node not found /
    non-media node / path beyond this message's content" requirements —
    there is no separate tree-walk needed. Resolution is also
    structurally unique by construction: _parse_nested_item visits each
    (recursion level, sibling index) pair at most once, so no two
    media_refs entries for one message can ever share a path (verified by
    test, not merely assumed).

    structured_content.media_refs is server-internal only (see
    app.structured_message_parser's module docstring) — this function's
    return value must never be serialized into an API response as-is;
    only sdkfileid-derived, already-authorized lookups (MediaFile status/
    mime_type/size) may reach the client.
    """
    refs = iter_nested_media_refs(structured_content)
    for ref in refs:
        if ref["path"] == path:
            return ref
    return None


def _resolve_authorized_nested_media(
    db: Session, conversation_id: str, msgid: str, item_path: str, tenant_id: str
) -> Tuple[ArchiveMessage, dict, MediaFile]:
    """Shared authorization + lookup for the nested mixed/chatrecord media
    routes (get_nested_message_media, get_nested_message_media_access) —
    the nested-item analogue of _resolve_authorized_media, sharing its
    exact authorization shape: conversation membership -> msgtype gate ->
    row lookup, all completed before any storage provider is touched.

    Differs from _resolve_authorized_media in one deliberate way: the
    MediaFile row is resolved by (tenant_id, sdkfileid), NOT by
    (tenant_id, archive_message_id). This is not a shortcut — it is the
    correct model for a nested reference: the same physical WeCom media
    object can be legitimately referenced by more than one logical
    message/node (e.g. the identical file re-forwarded into a second
    chatrecord digest), and this route answers "can this tenant read the
    physical object this logical node refers to", not "does this exact
    row happen to be owned by this exact message" (that ownership
    question only matters to the DOWNLOAD/write path — see
    app.media_download.get_or_reset_media_file — and is deliberately left
    unchanged here; see the RND-200 QA fix report's association-model
    section). Tenant isolation is unaffected: sdkfileid is never taken
    from the request — it is read server-side from THIS message's own
    already-tenant-scoped structured_content only.
    """
    messages = _fetch_conversation_messages(db, conversation_id, tenant_id)
    msg = next((m for m in messages if m.msgid == msgid), None)
    if msg is None:
        raise HTTPException(status_code=404, detail="Not found")

    if msg.msgtype not in NESTED_MEDIA_MSGTYPES:
        raise HTTPException(status_code=404, detail="Not found")

    validated_path = _validate_nested_media_path(item_path)
    if validated_path is None:
        raise HTTPException(status_code=400, detail="Malformed nested media path")

    ref = _find_nested_media_ref(getattr(msg, "structured_content", None), validated_path)
    if ref is None:
        raise HTTPException(status_code=404, detail="Not found")

    media_file = (
        db.query(MediaFile)
        .filter(MediaFile.tenant_id == tenant_id, MediaFile.sdkfileid == ref["sdkfileid"])
        .first()
    )
    if media_file is None or media_file.download_status != "downloaded":
        raise HTTPException(status_code=404, detail="Not found")

    return msg, ref, media_file


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


def _build_nested_media_descriptor(
    media_type: str, media_file: Optional[MediaFile], conversation_id: str, msgid: str, path: str
) -> dict:
    """Build the safe, public per-node media descriptor attached to a
    mixed/chatrecord structured_content node — {"status", "media_type",
    "mime_type", "size_bytes", "access_url"}. Never includes sdkfileid,
    any MediaFile database id, local_path, storage_ref/oss_key, or any
    storage credential — only a status string, the already-known nested
    item type, content-sniffed mime_type, byte size, and a same-origin
    API URL the caller can request an access descriptor from (mirroring
    TimelineMessageOut.media_access_url, which is null unless the
    underlying media is actually available).
    """
    status = "not_downloaded"
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    access_url: Optional[str] = None

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
            )

    return {
        "status": status,
        "media_type": media_type,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "access_url": access_url,
    }


def _enrich_nested_media_fields(
    fields: dict, media_refs: list, media_files_by_sdkfileid: dict, conversation_id: str, msgid: str
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
    """
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
                node.get("type"), media_file, conversation_id, msgid, node.get("path")
            )
        children = node.get("children")
        if isinstance(children, list):
            node["children"] = [_walk(child) for child in children if isinstance(child, dict)]
        return node

    items = fields.get("items")
    if isinstance(items, list):
        fields["items"] = [_walk(item) for item in items if isinstance(item, dict)]
    return fields


def _is_valid_roomid(roomid: Optional[str]) -> bool:
    """Return True if roomid should be treated as a group conversation
    identifier. Matches _derive_conversation_membership's truthiness rule
    (``roomid = roomid or ""`` then ``if roomid:``), so that this function
    and the authoritative builder agree on every input:
      - None/empty roomid → direct message (falsy after ``or ""``)
      - non-empty roomid (including whitespace-only) → group (truthy)
    """
    return bool(roomid)


def _compact_entity_messages(
    db: Session, entity_id: str, tenant_id: str
) -> Tuple[list, dict[int, list[str]]]:
    """Fetch a compact projection of the messages expanded from entity_id's
    participation, producing the same message set _fetch_messages_for_entity
    returns for well-formed data, but only the columns strictly required for
    conversation-key derivation:

      - message.id, message.sender, message.roomid
      - per-message recipient userid list

    Uses the same seed-ID logic and group-room expansion rules as
    _fetch_messages_for_entity, so the intermediate message set is the same
    under normal data. For malformed inputs (whitespace-only roomids, etc.)
    the intermediate expansion may differ by a marginal number of rows, but
    that does not affect the final canonical-count guarantee: the compact
    projection always derives keys through the exact same
    _derive_conversation_membership function the authoritative builder uses.

    No ArchiveMessage ORM objects are materialized — only lightweight tuples
    are built from column values, avoiding the content_text, msgtype, sdkfileid,
    decrypted_payload, etc. fields that make full ORM fetches expensive.

    Returns (compact_msgs, recipients_map) where:
      compact_msgs: list of SimpleNamespace with .id, .sender, .roomid
      recipients_map: {message.id: [receiver_userid, ...]}
    """
    # 1. Find seed message IDs where entity_id is sender or recipient.
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
        return [], {}

    # 2. Compact projection of seed messages — only (id, sender, roomid).
    seed_rows = (
        db.query(ArchiveMessage.id, ArchiveMessage.sender, ArchiveMessage.roomid)
        .filter(
            ArchiveMessage.id.in_(seed_ids),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    )
    # Convert to SimpleNamespace to match the interface _derive_conversation_membership
    # expects (msg.sender, msg.roomid, msg.id).
    seed_msgs = [
        SimpleNamespace(id=r[0], sender=r[1], roomid=r[2])
        for r in seed_rows
    ]

    # 3. Collect group rooms and expand to ALL messages in those rooms.
    group_rooms = {m.roomid for m in seed_msgs if _is_valid_roomid(m.roomid)}

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

    # 4. Direct messages from seed are included as-is.
    direct_ids = {m.id for m in seed_msgs if not _is_valid_roomid(m.roomid)}
    final_ids.update(direct_ids)

    if not final_ids:
        return [], {}

    # 5. Compact projection of final message set.
    final_rows = (
        db.query(ArchiveMessage.id, ArchiveMessage.sender, ArchiveMessage.roomid)
        .filter(
            ArchiveMessage.id.in_(final_ids),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    )
    compact_msgs = [
        SimpleNamespace(id=r[0], sender=r[1], roomid=r[2])
        for r in final_rows
    ]

    # 6. Batch-load recipients for all final messages.
    recipients_map: dict[int, list[str]] = {}
    for r in (
        db.query(ArchiveMessageRecipient.message_id, ArchiveMessageRecipient.receiver_userid)
        .filter(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessageRecipient.message_id.in_(final_ids),
        )
        .all()
    ):
        recipients_map.setdefault(r[0], []).append(r[1])

    return compact_msgs, recipients_map


def _count_entity_conversations(
    db: Session, entity_id: str, tenant_id: str, staff_ids: Optional[set[str]] = None
) -> int:
    """Count distinct conversations for an entity, reproducing the exact
    canonical conversation keys from _build_conversation_list /
    _derive_conversation_membership without materializing full ArchiveMessage
    ORM objects.

    Fetches a compact projection (message.id, message.sender, message.roomid)
    plus per-message recipient userids — avoids loading content_text, msgtype,
    sdkfileid, media payloads, and every other heavy column on ArchiveMessage.

    staff_ids: when provided, determines staff/contact classification
    matching _build_conversation_list's behavior. When None, falls back to
    the legacy "staff_" prefix check.
    """
    compact_msgs, recipients_map = _compact_entity_messages(db, entity_id, tenant_id)
    if not compact_msgs:
        return 0

    # Define is_staff matching _build_conversation_list's rule.
    def is_staff(uid: str) -> bool:
        if staff_ids is not None:
            return uid in staff_ids
        return uid.startswith("staff_")

    # Derive canonical conversation keys using the exact same
    # _derive_conversation_membership function the authoritative
    # _build_conversation_list uses.
    canonical_keys: set[str] = set()
    for msg in compact_msgs:
        recipients = recipients_map.get(msg.id, [])
        conv_id, conv_type, _staff_set, _contact_set = _derive_conversation_membership(
            msg.sender, msg.roomid, recipients, is_staff
        )
        canonical_keys.add(conv_id)

    return len(canonical_keys)


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


def _entity_seed_ids(db: Session, entity_id: str, tenant_id: str) -> set[int]:
    """Return the set of ArchiveMessage ids where entity_id is literally the
    sender, or a listed recipient, tenant-scoped -- WITHOUT any group-room
    expansion.

    This is the exact entity-participation test _fetch_messages_for_entity
    and _fetch_compact_messages_for_entity have always used to seed their
    message sets before expanding through shared group rooms. Extracted as
    a standalone helper (RND-158 Phase 2 contract round) so the message
    timeline endpoint's entity-scoped collision resolution (see
    _fetch_conversation_messages) can apply the IDENTICAL per-message
    participation test to a collision bucket's direct-side and group-side
    messages, instead of a third, divergence-prone reimplementation. Pure
    behavior-preserving extraction -- the two callers below produce exactly
    the same seed_ids set as before.
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
    return sender_ids | recipient_ids


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
    seed_ids = _entity_seed_ids(db, entity_id, tenant_id)
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
        # RND-158 SQL ordering contract: explicit ORDER BY msgtime ASC, id
        # ASC. `.in_()` filters give no row-order guarantee from the DB, so
        # this makes the query itself deterministic rather than relying on
        # incidental DB default order. Ascending order matches how
        # _build_conversation_list aggregates messages: each later message
        # (chronologically) simply replaces the running "latest" state for
        # its conversation bucket, so processing in ascending (msgtime, id)
        # order is the natural fit. `id` breaks ties for equal msgtime
        # values deterministically (id is the ArchiveMessage primary key:
        # non-null, unique, immutable — see uq_archive_messages_tenant_id_id
        # in app/db/models.py). This exact ORDER BY is mirrored in
        # _fetch_compact_messages_for_entity below so both the
        # authoritative and optimized paths receive identically-ordered
        # input. Note: which message _build_conversation_list treats as
        # "latest" is governed independently by its own (msgtime, id) sort
        # over the aggregated bucket (see there) — this ORDER BY affects
        # first-touch/aggregation order only, not latest-message selection.
        .order_by(ArchiveMessage.msgtime.asc(), ArchiveMessage.id.asc())
        .all()
    )


def _fetch_compact_messages_for_entity(
    db: Session, entity_id: str, tenant_id: str
) -> list:
    """RND-158 Phase 2: compact-projection counterpart to
    _fetch_messages_for_entity(), used ONLY by GET /api/conversations
    (get_conversations). Produces the same message set (same seed-ID +
    group-room-expansion rules), but selects only the columns
    _build_conversation_list actually reads: id, sender, roomid, msgtime,
    content_text — never the heavy columns (raw_encrypted_payload,
    encrypt_random_key, encrypt_chat_msg, decrypted_payload,
    structured_content, sdkfileid, tolist, msgtype, decrypt_status,
    is_revoked, revoked_at, msgid, created_at) that made full ArchiveMessage
    ORM materialization the dominant cost in profiling (see RND-158 Phase 2
    benchmark notes). content_text itself is still fetched in full (only
    _build_conversation_list's final `[:200]` slice truncates it) — every
    other heavy column is dropped from the projection.

    _fetch_messages_for_entity() itself is left untouched and unused after
    this change: it is kept because removing a function nothing calls is
    out of scope for a profiling-driven perf change, and other code may
    come to depend on it.

    Returns a list of SimpleNamespace(id, sender, roomid, msgtime,
    content_text) — the exact attribute surface _build_conversation_list
    and _derive_conversation_membership read.
    """
    seed_ids = _entity_seed_ids(db, entity_id, tenant_id)
    if not seed_ids:
        return []

    seed_rows = (
        db.query(ArchiveMessage.id, ArchiveMessage.sender, ArchiveMessage.roomid)
        .filter(
            ArchiveMessage.id.in_(seed_ids),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    )
    group_rooms = {r[2] for r in seed_rows if _is_valid_roomid(r[2])}

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

    direct_ids = {r[0] for r in seed_rows if not _is_valid_roomid(r[2])}
    final_ids.update(direct_ids)

    if not final_ids:
        return []

    final_rows = (
        db.query(
            ArchiveMessage.id,
            ArchiveMessage.sender,
            ArchiveMessage.roomid,
            ArchiveMessage.msgtime,
            ArchiveMessage.content_text,
        )
        .filter(
            ArchiveMessage.id.in_(final_ids),
            ArchiveMessage.tenant_id == tenant_id,
        )
        # RND-158 SQL ordering contract: identical ORDER BY msgtime ASC, id
        # ASC as _fetch_messages_for_entity's final query above — kept
        # symmetric so the authoritative and optimized paths always
        # process messages in the same order and stay byte-for-byte
        # equivalent (see test_full_equiv_* / _assert_full_equivalence in
        # test_staff_seats.py). See the comment on that ORDER BY for the
        # full rationale. conversation_type/roomid collision resolution in
        # _build_conversation_list no longer depends on row order at all
        # (group-wins, order-independent — see its docstring), so this
        # ORDER BY can be added safely without the asymmetry concern that
        # previously blocked it.
        .order_by(ArchiveMessage.msgtime.asc(), ArchiveMessage.id.asc())
        .all()
    )
    return [
        SimpleNamespace(
            id=r[0], sender=r[1], roomid=r[2], msgtime=r[3], content_text=r[4]
        )
        for r in final_rows
    ]


def _load_recipients_map_compact(
    db: Session, tenant_id: str, msg_ids: list[int]
) -> dict[int, list[str]]:
    """RND-158 Phase 2: compact-column counterpart to _load_recipients_map(),
    used ONLY by get_conversations(). Selects (message_id, receiver_userid)
    tuple columns instead of full ArchiveMessageRecipient ORM rows, avoiding
    per-row ORM object construction for what can be thousands of recipient
    rows. ArchiveMessageRecipient columns are all light already, but ORM
    instantiation overhead itself was measured as material (see RND-158
    Phase 2 benchmark notes) — this returns the identical
    {message_id: [receiver_userid, ...]} shape _load_recipients_map()
    returns, so it is a drop-in replacement for this call site only.

    _load_recipients_map() itself is deliberately left untouched: it is
    still used by the message-timeline endpoint
    (get_conversation_messages/_fetch_conversation_messages), which is
    explicitly out of scope for this phase and must not change behavior or
    performance.
    """
    if not msg_ids:
        return {}
    result: dict[int, list[str]] = {}
    for message_id, receiver_userid in (
        db.query(
            ArchiveMessageRecipient.message_id, ArchiveMessageRecipient.receiver_userid
        )
        .filter(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessageRecipient.message_id.in_(msg_ids),
        )
        .all()
    ):
        result.setdefault(message_id, []).append(receiver_userid)
    return result


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

    Collision-priority contract (RND-158 SQL-ordering fix round): a single
    conversation_id can be produced by two structurally different
    messages — an inferred direct-pair key (`_direct_conv_id`) and an
    actual roomid — colliding into the same `convs[conv_id]` bucket (see
    test_full_equiv_direct_group_key_collision and its
    _group_first/_direct_first order-independence pair). Which
    conversation_type/roomid the bucket ends up with must NOT depend on
    which message happened to be aggregated first (that would make the
    result depend on incidental SQL row order). The contract is:
    "group" always wins over "direct": an explicit non-empty roomid on a
    message is stronger structural evidence of a real group chat than an
    inferred direct-pair key, so a bucket's conversation_type is "group"
    if ANY aggregated message was group-shaped, and "direct" only if
    EVERY aggregated message was direct-shaped — regardless of processing
    order. This is enforced by only ever upgrading direct -> group below,
    never downgrading group -> direct once set, which is a commutative,
    order-independent reduction over the message list. This is
    independent of latest-message selection: the (msgtime, id) sort below
    still picks "latest" purely from data["msgs"], untouched by which
    message set conversation_type.
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
        elif conv_type == "group" and convs[conv_id]["conversation_type"] == "direct":
            # Group-wins collision upgrade: a later-processed group-shaped
            # message overrides an earlier direct-shaped one that landed
            # in the same bucket. Never the reverse (group is never
            # downgraded to direct).
            convs[conv_id]["conversation_type"] = "group"
            convs[conv_id]["roomid"] = msg.roomid if roomid else None

        convs[conv_id]["monitored_account_ids"].update(staff_set)
        convs[conv_id]["contact_ids"].update(contact_set)
        convs[conv_id]["msgs"].append(msg)

    result = []
    for conv_id, data in convs.items():
        # RND-158 tie-break fix: deterministic message recency contract —
        # the "latest" message within a conversation is the one with the
        # highest (msgtime, id) tuple, both descending. msgtime alone is
        # not sufficient: multiple messages can share the same msgtime
        # (same-second bulk sends, clock granularity, etc.), and without a
        # secondary key the "latest" pick would depend on whatever order
        # messages happened to arrive in `data["msgs"]` — itself a
        # function of non-deterministic DB row order upstream. `id` (the
        # ArchiveMessage integer primary key, non-null/unique/immutable,
        # already present on every message object built by both the full
        # ORM path and the compact-projection path) breaks ties
        # deterministically and reproducibly. This Python-side sort is
        # authoritative on its own — it does not rely on the incoming
        # list already being DB-ordered.
        msgs_sorted = sorted(data["msgs"], key=lambda m: (m.msgtime or 0, m.id))
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

    # RND-158 tie-break fix: deterministic conversation ordering contract —
    # sort by (last_message_time, conversation_id), both descending.
    # conversation_id is the canonical, stable identifier already present
    # on every result row (unlike display_name/room_display_name, which
    # are mutable/localized and unsuitable as a tie-break key). Python
    # tuple comparison is lexicographic, so sorting on the
    # (last_message_time, conversation_id) tuple with reverse=True yields
    # descending order on BOTH elements: ties on last_message_time fall
    # back to conversation_id descending, verified below in
    # test_staff_seats.py. No negation trick is needed since
    # conversation_id (a string) compares/reverses correctly as the tuple
    # secondary key.
    result.sort(key=lambda x: (x["last_message_time"] or 0, x["conversation_id"]), reverse=True)
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


class MediaAccessOut(BaseModel):
    """Unified media access descriptor (RND-187) — the one shape the
    frontend consumes regardless of storage_backend, so it never needs to
    understand Qiniu vs local storage details, read storage_ref, or
    construct a media.crowntime.cn URL itself.

    access_type="signed_url": url is a short-lived, browser-usable Qiniu
    signed URL good until expires_at; the browser fetches it directly, no
    FastAPI proxying of image bytes.
    access_type="proxy": url is this backend's own authenticated route
    (unchanged local-media behavior); expires_at is None since the URL
    itself carries no time-boxed credential — the session cookie is what
    authorizes each request.
    """

    media_id: int
    storage_backend: str
    access_type: str
    url: str
    expires_at: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None


class NestedMediaAccessOut(BaseModel):
    """Public access descriptor for a nested mixed/chatrecord media item
    (RND-200 QA fix — security remediation). Deliberately a SEPARATE model
    from MediaAccessOut, not that model reused: MediaAccessOut.media_id
    exposes the internal MediaFile primary key, which was an accepted,
    pre-existing exposure for the single-media-per-message top-level
    contract (see get_message_media_access) but is NOT part of the
    approved nested-media public contract — the parent message + nested
    node path is the only public handle nested media is ever looked up
    by, and no internal database identifier may accompany it.

    Every other field mirrors MediaAccessOut's meaning exactly
    (access_type="proxy" vs "signed_url", expires_at only set for the
    latter) so nested and top-level media remain trivially similar for
    any consumer that already understands one of the two shapes — only
    the internal-id field is omitted, and mime_type/filename are named to
    match the per-node structured_content descriptor's own field names
    (see _build_nested_media_descriptor) rather than MediaAccessOut's
    content_type, for consistency within the nested contract itself.
    filename is always None today (no media type in this system carries
    one — see _build_nested_media_descriptor's docstring) but is kept as
    an explicit, documented field rather than omitted, matching the
    shape independent QA approved.
    """

    storage_backend: str
    access_type: str
    url: str
    expires_at: Optional[str] = None
    mime_type: Optional[str] = None
    filename: Optional[str] = None
    size_bytes: Optional[int] = None


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
        # RND-158: avoid full ArchiveMessage ORM object materialization for
        # conversation_count. The old code fetched every message as a full
        # ORM object (all columns including content_text, msgtype, sdkfileid,
        # decrypted_payload, etc.) then aggregated them through
        # _build_conversation_list — just to compute the display-badge
        # conversation_count. The revised path fetches a compact projection
        # (message.id, message.sender, message.roomid + recipient userids)
        # and derives canonical conversation keys using the exact same
        # _derive_conversation_membership function the authoritative builder
        # uses, avoiding full ORM object construction while preserving exact
        # count semantics. Per-seat query scaling remains unchanged.
        # The authoritative conversation list endpoint (/api/conversations)
        # still uses the full _fetch_messages_for_entity +
        # _build_conversation_list path for correctness.
        conversation_count = _count_entity_conversations(db, sid, tenant_id, staff_ids)
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

    # RND-158 Phase 2: compact-projection fetch — see
    # _fetch_compact_messages_for_entity / _load_recipients_map_compact
    # docstrings. Avoids full ArchiveMessage/ArchiveMessageRecipient ORM
    # materialization, which profiling showed was the dominant cost of this
    # endpoint. _build_conversation_list only ever reads .id/.sender/
    # .roomid/.msgtime/.content_text off each message and message_id/
    # receiver_userid off recipients, so this is behavior-preserving.
    messages = _fetch_compact_messages_for_entity(db, entity_id, tenant_id)
    if not messages:
        return []

    recipients_map = _load_recipients_map_compact(db, tenant_id, [m.id for m in messages])

    # RND-158 Phase 2: scope display-name lookup and staff classification to
    # only the ids that actually appear in this message set — see
    # _load_display_names_for_ids / _staff_ids_for_participants docstrings
    # for why this is exactly equivalent to the tenant-wide versions for
    # every id _build_conversation_list ever queries.
    participant_ids: set[str] = {m.sender for m in messages if m.sender}
    for recipient_ids in recipients_map.values():
        participant_ids.update(recipient_ids)

    display_names = _load_display_names_for_ids(db, tenant_id, participant_ids)
    staff_ids = _staff_ids_for_participants(db, tenant_id, participant_ids)
    return _build_conversation_list(messages, recipients_map, display_names, staff_ids)


def _fetch_direct_pair_messages(
    db: Session, uid_a: str, uid_b: str, tenant_id: str
) -> list[ArchiveMessage]:
    """
    Return every ArchiveMessage row that is a direct (non-group) message
    between uid_a and uid_b, scoped to tenant_id. Extracted from
    _fetch_conversation_messages so the RND-158 Phase 2 collision fix (see
    that function's docstring) can call this query in isolation, in
    addition to the group-roomid query, without duplicating the
    join/filter logic.

    ArchiveMessageRecipient.tenant_id == tenant_id (in addition to
    ArchiveMessage.tenant_id == tenant_id) is required here, not optional:
    message_id is a global primary key, so a malformed cross-tenant
    recipient row that happens to share a message_id and receiver_userid
    with this tenant's data would otherwise let the join manufacture false
    direct-conversation membership.
    """
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


def _fetch_group_room_messages(
    db: Session, roomid: str, tenant_id: str
) -> list[ArchiveMessage]:
    """Return every ArchiveMessage row with ArchiveMessage.roomid == roomid,
    scoped to tenant_id. Extracted from _fetch_conversation_messages so the
    collision-merge path (see that function's docstring) can run this
    exact query — the same one the plain group branch has always used —
    against a `direct__`-shaped conversation_id string to check whether it
    ALSO happens to be a real roomid.
    """
    return (
        db.query(ArchiveMessage)
        .filter(
            ArchiveMessage.roomid == roomid,
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    )


def _fetch_null_sender_candidate_messages(
    db: Session,
    conversation_id: str,
    tokens: "str | list[str]",
    tenant_id: str,
    *,
    require_null_sender: bool = False,
) -> list[ArchiveMessage]:
    """RND-158 Phase 2 (prefix/null-sender round): resolve a "direct__"-shaped
    conversation_id whose remainder does not split into a uid_a___uid_b pair
    (or does, but no real direct pair/group exists for it) and does not
    match a real group room. This exact shape — a single bare token with no
    "___" separator — is what `_derive_conversation_membership`'s one-sided
    fallback emits for a message with only one non-empty participant (see
    that function's `else` branch, `len(parts) < 2` case): e.g. a message
    with an empty/null sender and exactly one recipient, or a sender with no
    recipients at all.

    `tokens`: either a single token (the one-sided/orphan caller, step 5 of
    `_fetch_conversation_messages`) or a list of tokens (the null-sender
    multi-recipient direct-pair gap fix, which used to issue one call per
    token and merges them into a single round trip here instead — see
    `require_null_sender` below).

    `require_null_sender` (RND-158 Phase 2 QA round 7, blocker 2 — query
    narrowing): when True, the candidate query is additionally restricted
    to messages whose `sender` is null/empty, and only matches via a
    recipient row (the `sender == token` alternative is dropped, since it
    can never be true once sender must be null/empty). This is set by the
    null-sender-multi-recipient-gap-fix call site in
    `_fetch_conversation_messages`, which previously ran two broad,
    unrestricted candidate scans (one per token of an ordinary 2-part
    direct pair) on EVERY direct timeline request, even when no
    null-sender message was involved at all — the scan matched any message
    where the token was sender OR recipient, i.e. effectively every
    message either party ever sent or received. Restricting to
    sender IS NULL/'' makes the query cheap and selective for the common
    case (a normal direct conversation with no null-sender messages
    returns zero candidates almost immediately), while still finding every
    genuine null-sender candidate. The one-sided/orphan caller (default,
    require_null_sender=False) keeps the original broader match, since a
    one-sided candidate can legitimately have a real (non-null) sender
    equal to the token (e.g. a message with a sender and zero recipients).

    Candidate-then-verify (same style as `_compact_entity_messages`'s
    seed-then-expand and `_resolve_authorized_nested_media`'s
    lookup-then-validate): first run a cheap, bounded, indexed-column-equality
    query for messages where a token appears as sender OR recipient (or,
    with require_null_sender, just as recipient of a null-sender message),
    tenant-scoped, excluding group messages (a real group room match was
    already ruled out by the caller before this is invoked). Then recompute
    each candidate's canonical conversation_id via
    `_derive_conversation_membership` itself — the exact function
    `_build_conversation_list` uses to build the list — and keep only the
    candidates whose recomputed id matches conversation_id exactly. This
    guarantees the returned set is EXACTLY the bucket the list would have
    grouped under this id, never a superset from a partial string match.

    is_staff classification (RND-158 Phase 2 QA round 7, blocker 1 fix):
    a real, tenant-scoped is_staff classifier is now used here, computed
    via `_staff_ids_for_participants` over the union of every candidate's
    sender + recipients — NOT a stub that always returns False. A
    previous round's docstring here justified a `_no_staff` stub by
    arguing "the one-sided/orphan branch is only reachable when at most
    one non-empty party exists on the message, so is_staff classification
    never changes which branch fires." That reasoning holds only for
    messages with <= 1 participant. It breaks for a 2-or-more participant
    null-sender message (reachable via the require_null_sender=True gap
    fix, or any candidate that happens to have >= 2 distinct
    participants): `_derive_conversation_membership` takes its two-party
    branch (`staff_set` and `contact_set` both non-empty) only when the
    REAL classifier is used; the `_no_staff` stub forces staff_set to
    always be empty, so it instead falls into the all-same-classification
    `else` branch and computes a DIFFERENT conversation_id — one that
    silently drops any third participant and never matches what
    `_build_conversation_list` (which always uses the real classifier)
    actually grouped the message under. Using the same real classifier
    here as the list uses is the only way this function can stay a
    faithful re-verification of list membership for any participant
    count, not just <= 1.
    """
    token_list = [tokens] if isinstance(tokens, str) else list(tokens)
    token_list = [t for t in token_list if t]
    if not token_list:
        return []

    filters = [
        ArchiveMessage.tenant_id == tenant_id,
        or_(ArchiveMessage.roomid.is_(None), ArchiveMessage.roomid == ""),
    ]
    if require_null_sender:
        filters.append(or_(ArchiveMessage.sender.is_(None), ArchiveMessage.sender == ""))
        filters.append(
            and_(
                ArchiveMessageRecipient.receiver_userid.in_(token_list),
                ArchiveMessageRecipient.tenant_id == tenant_id,
            )
        )
    else:
        filters.append(
            or_(
                ArchiveMessage.sender.in_(token_list),
                and_(
                    ArchiveMessageRecipient.receiver_userid.in_(token_list),
                    ArchiveMessageRecipient.tenant_id == tenant_id,
                ),
            )
        )

    candidates = (
        db.query(ArchiveMessage)
        .outerjoin(
            ArchiveMessageRecipient,
            ArchiveMessage.id == ArchiveMessageRecipient.message_id,
        )
        .filter(*filters)
        .distinct()
        .all()
    )
    if not candidates:
        return []

    recipients_map = _load_recipients_map(db, tenant_id, [m.id for m in candidates])

    participant_ids: set[str] = set()
    for msg in candidates:
        if msg.sender:
            participant_ids.add(msg.sender)
        participant_ids.update(recipients_map.get(msg.id, []))
    staff_ids = _staff_ids_for_participants(db, tenant_id, participant_ids)

    def _is_staff(uid: str) -> bool:
        return uid in staff_ids

    matched = []
    for msg in candidates:
        recipients = recipients_map.get(msg.id, [])
        conv_id, _conv_type, _staff_set, _contact_set = _derive_conversation_membership(
            msg.sender, msg.roomid, recipients, _is_staff
        )
        if conv_id == conversation_id:
            matched.append(msg)
    return matched


def _fetch_conversation_messages(
    db: Session,
    conversation_id: str,
    tenant_id: str,
    *,
    mode: Optional[str] = None,
    entity_id: Optional[str] = None,
) -> list[ArchiveMessage]:
    """
    Return every ArchiveMessage row belonging to conversation_id, scoped to
    tenant_id. Shared by the timeline route and the media-serving route so
    both use identical, tenant-scoped membership rules — a message is only
    ever considered part of a conversation if this function says so.

    mode/entity_id: optional entity context (RND-158 Phase 2 API-contract
    round), forwarded by the timeline route from its new optional
    mode/staff_id/contact_id query params. The two media-serving call
    sites intentionally never pass these (out of scope for this round) and
    keep the legacy, entity-blind resolution behavior described below.
    When absent (the default), behavior is unchanged from the previous
    round EXCEPT that a genuinely ambiguous direct/group collision now
    raises 400 instead of silently merging both sides — see the
    "Genuine direct/group collision" section further down.

    conversation_id formats:
      Group:  <roomid>                          e.g. "after_sales_group_001"
      Direct: "direct__<uid_a>___<uid_b>"       e.g. "direct__contact_zhangsan___staff_yingzi"
      Null-sender/one-sided: "direct__<token>"  e.g. "direct__contact_orphan"
        (no "___" separator — see _fetch_null_sender_candidate_messages)

    RND-158 Phase 2 collision fix: `_build_conversation_list` (via
    `_derive_conversation_membership`) already implements a "group wins"
    collision rule when a direct-pair's canonical key
    (`direct__<uid_a>___<uid_b>`) happens to collide with an actual
    group's roomid string — i.e. some message literally has
    `roomid == "direct__<uid_a>___<uid_b>"`. In that case the list reports
    a single merged bucket with conversation_type="group" and
    message_count covering BOTH the direct and group messages (see that
    function's "Collision-priority contract" docstring — this is the one
    authoritative place documenting the rule; this function only
    re-applies it for member-set *fetching*, it does not redefine it).

    RND-158 Phase 2 (prefix/null-sender round) resolution order: for ANY
    "direct__"-prefixed conversation_id, in this exact precedence, all
    branches tenant-scoped:

      1. Exact tenant-scoped room membership: unconditionally check
         `_fetch_group_room_messages(db, conversation_id, tenant_id)` —
         regardless of whether the remainder parses into a uid_a___uid_b
         pair. This is what fixes the original bug: a real group room
         literally named "direct__room_only_group" (no "___" in it) used
         to 400 before this check ever ran.
      2. Collision membership: if the room check found hits AND the
         remainder also parses into a 2-part pair with existing direct
         messages, merge (union, de-duplicated by message id) — unchanged
         from the previously-accepted collision logic.
      3. Pure group: if the room check found hits but there's no matching
         direct pair (either the remainder isn't a 2-part shape at all, or
         it is but no direct messages exist for it), return the group
         messages only. This is what makes a room like
         "direct__fake___shape" resolve as a room, not a failed direct
         parse.
      4. Standard direct identity: if the room check found nothing and the
         remainder parses into exactly 2 parts, run
         `_fetch_direct_pair_messages` — today's standard direct path,
         byte-identical to previous behavior when no collision exists.
      5. Null-sender/one-sided direct identity: if the room check found
         nothing and the remainder does NOT parse into exactly 2 parts (no
         "___" separator, or empty), resolve it via
         `_fetch_null_sender_candidate_messages` — the candidate-then-verify
         path that reuses `_derive_conversation_membership` as the single
         source of truth, so the list and timeline can never diverge on
         what counts as this bucket.
      6. Invalid ID: only once none of the above resolve anything real —
         an empty remainder, or a non-2-part remainder that matches
         neither a room nor any candidate message — raise 400.

    A plain (non "direct__"-prefixed) conversation_id is always treated as
    a roomid and queried via the group branch only, exactly as before —
    group-shaped conversation_ids can never collide with a direct pair
    the other direction, since _derive_conversation_membership only ever
    emits a "direct__" key when there is no roomid on the message.

    RND-158 Phase 2 (API-contract round) changes, both additive fixes on
    top of the above, controlled by the new optional `mode`/`entity_id`
    parameters:

      * Null-sender multi-recipient direct pairs (step 4 gap):
        `_fetch_direct_pair_messages` only matches messages where `sender`
        is literally uid_a or uid_b. A message with a null/empty sender
        and BOTH uid_a and uid_b as recipients (e.g. two contacts, two
        staff, or one staff + one contact, with no sender) still
        canonicalizes to this exact "direct__<a>___<b>" id via
        `_derive_conversation_membership` (all_parties has 2 members, no
        sender to break the tie) — but `_fetch_direct_pair_messages` can
        never find it, since neither `sender == uid_a` nor `sender ==
        uid_b` holds. Fixed by additionally running the existing
        candidate-then-verify path (`_fetch_null_sender_candidate_messages`)
        once per token (uid_a, uid_b) and merging any verified hits into
        `direct_messages`, de-duplicated by id. This is purely additive:
        every message `_fetch_direct_pair_messages` already found is
        untouched, and the only new messages ever added are ones whose
        recomputed canonical id via `_derive_conversation_membership`
        (the single source of truth) provably equals this conversation_id.

      * Genuine direct/group collision, entity-aware resolution: when the
        caller supplies entity context (`mode` + `entity_id`, resolved by
        the route from `mode`/`staff_id`/`contact_id`), a collision bucket
        (both `group_messages` and `direct_messages` non-empty) is
        resolved per-entity instead of always merging: the direct side is
        included only if the entity participates in at least one
        direct-side message (`sender == entity_id` or a recipient row for
        entity_id), and the group side is included only if the entity
        participates in at least one group-side message — the exact same
        seed test `_fetch_messages_for_entity`/`_fetch_compact_messages_for_entity`
        use to build the list (see `_entity_seed_ids`). If the entity
        participates in both sides, both are merged (matching the list's
        merged count for that entity). If only one side, only that side is
        returned. When NO entity context is supplied (legacy ID-only
        request) and the collision is genuine, merging both sides
        silently would be a real over-inclusion bug for whichever entity
        actually navigated here (see RND-158 Phase 2 contract round
        report) — so this now raises HTTPException(400) instead, asking
        the caller to disambiguate via mode/staff_id/contact_id. This is
        the one intentional behavior change versus the previous round;
        every other ID-only case above (steps 1, 3, 4, 5, 6) is unchanged.
    """
    entity_scoped = bool(mode and entity_id)

    if conversation_id.startswith("direct__"):
        rest = conversation_id[len("direct__"):]
        parts = rest.split("___", 1)

        # Step 1: exact tenant-scoped room membership, unconditional.
        group_messages = _fetch_group_room_messages(db, conversation_id, tenant_id)

        direct_messages: list[ArchiveMessage] = []
        if len(parts) == 2:
            uid_a, uid_b = parts
            direct_messages = _fetch_direct_pair_messages(db, uid_a, uid_b, tenant_id)

            # Null-sender multi-recipient gap fix (see docstring above):
            # additionally pick up messages whose sender is null/empty but
            # whose recipients include both uid_a and uid_b, verified via
            # _derive_conversation_membership so only true members of this
            # exact bucket are added.
            # RND-158 Phase 2 QA round 7 (blocker 2, query narrowing): a
            # single merged, sender-null-restricted candidate query instead
            # of two broad, unrestricted per-token scans -- see
            # _fetch_null_sender_candidate_messages's require_null_sender
            # docstring section for why this is both cheaper (selective
            # predicate) and correct (still finds every genuine
            # null-sender candidate for either token).
            null_sender_extra = _fetch_null_sender_candidate_messages(
                db, conversation_id, [uid_a, uid_b], tenant_id, require_null_sender=True
            )
            if null_sender_extra:
                seen_direct: set[int] = {m.id for m in direct_messages}
                for msg in null_sender_extra:
                    if msg.id not in seen_direct:
                        seen_direct.add(msg.id)
                        direct_messages.append(msg)

        if group_messages and direct_messages:
            # Step 2: genuine collision.
            if entity_scoped:
                # RND-158 Phase 2 QA round 8 (blocker 1): filter each side
                # to the messages that ACTUALLY involve entity_id, not just
                # gate inclusion of the whole side on "does the entity
                # appear in at least one message on this side". The same
                # canonical id can bucket together messages with genuinely
                # different real participants (see the per-message filter
                # note on Step 4 below) -- direct_messages here can itself
                # be a mix of an ordinary direct message and an unrelated
                # null-sender multi-recipient message, so an any()-gate
                # that then returns the WHOLE side is exactly the same
                # over-inclusion bug Step 4 had, just latent (no fixture
                # exercised a real group-room collision with a mismatched
                # null-sender message until now). Apply the identical
                # per-message _entity_seed_ids filter to both sides.
                seed_ids = _entity_seed_ids(db, entity_id, tenant_id)
                entity_direct = [m for m in direct_messages if m.id in seed_ids]
                entity_group = [m for m in group_messages if m.id in seed_ids]
                if entity_direct and entity_group:
                    seen: set[int] = set()
                    messages = []
                    for msg in entity_direct + entity_group:
                        if msg.id not in seen:
                            seen.add(msg.id)
                            messages.append(msg)
                    return messages
                if entity_direct:
                    return entity_direct
                if entity_group:
                    return entity_group
                # Entity context was supplied but the entity participates
                # in neither side of this bucket -- nothing to return; the
                # route treats an empty list as 404, same as any other
                # entity navigating to a conversation it has no part in.
                return []

            # No entity context: this id is genuinely ambiguous (it
            # matches both a direct pair and a real group room) -- merging
            # both sides here would silently over-include messages for
            # whichever entity is actually viewing this timeline. Ask the
            # caller to disambiguate instead of guessing.
            raise HTTPException(
                status_code=400,
                detail=(
                    "Conversation ID is ambiguous: it matches both a direct "
                    "conversation and a group room. Provide mode and "
                    "staff_id/contact_id to resolve it unambiguously."
                ),
            )

        if group_messages:
            # Step 3: the string only looks direct-shaped (or parses into a
            # 2-part pair with no matching messages); it is actually a real
            # roomid. Pure group.
            return group_messages

        if len(parts) == 2:
            # Step 4: standard direct path (today's behavior, now with the
            # RND-158 Phase 2 QA round 8 (blocker 1) entity-membership
            # filter). direct_messages here can legitimately mix TWO kinds
            # of messages that collapse to the same canonical bucket id but
            # have different actual participants: (a) an ordinary direct
            # message whose sender/recipient IS exactly (uid_a, uid_b), and
            # (b) a null-sender multi-recipient message whose recipients
            # merely *include* both uid_a and uid_b alongside a third
            # participant _derive_conversation_membership's lossy
            # canonicalization silently drops from the id text (see that
            # function's docstring). Both are correctly members of this
            # bucket for the LIST (which has no single asking entity), but
            # when a specific entity_id is asking, only messages that
            # entity actually sent or received should be returned -- the
            # same required-membership rule the collision branch above
            # already applies. When no entity context is supplied (legacy
            # ID-only request), skip this filter entirely: return exactly
            # what previous rounds returned, unchanged, preserving the
            # legacy fast path and its existing performance profile.
            if entity_scoped:
                seed_ids = _entity_seed_ids(db, entity_id, tenant_id)
                return [m for m in direct_messages if m.id in seed_ids]
            return direct_messages

        # Step 5: no room hit, and the remainder isn't a uid_a___uid_b
        # pair — try the null-sender/one-sided candidate-then-verify path.
        if rest:
            orphan_messages = _fetch_null_sender_candidate_messages(
                db, conversation_id, rest, tenant_id
            )
            if orphan_messages:
                return orphan_messages

        # Step 6: nothing resolved this id at all.
        raise HTTPException(status_code=400, detail="Malformed direct conversation ID")

    return _fetch_group_room_messages(db, conversation_id, tenant_id)


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
    mode: Optional[str] = Query(
        None,
        description=(
            "Optional entity context: 'staff' or 'contact'. Required to "
            "unambiguously resolve a conversation_id that collides between "
            "a direct pair and a group room -- without it, a genuine "
            "collision returns 400. Same semantics as GET /api/conversations."
        ),
    ),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    conversation_type: Optional[str] = Query(
        None,
        description=(
            "Optional, validated consistency check: 'direct' or 'group'. "
            "conversation_id shape plus entity context alone still "
            "determine which messages are resolved -- this value never "
            "changes that resolution. When supplied, it must be exactly "
            "'direct' or 'group' (400 otherwise) and must match the "
            "actually-resolved bucket's type -- group if any resolved "
            "message has a real roomid, direct otherwise (400 on "
            "mismatch, e.g. a stale list row pointing at a bucket that "
            "has since become a group/direct collision resolved the "
            "other way for this entity). Also echoed back onto any "
            "media_url/media_access_url this response generates, so a "
            "client following those URLs carries the same context."
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

    conversation_type policy (RND-158 Phase 2 QA round 7, blocker 5):
    when supplied, conversation_type is validated as exactly 'direct' or
    'group' (400 otherwise), then checked against the actually-resolved
    bucket's type once messages are fetched -- a mismatch is a 400 (see
    below). It is never used to select or filter messages itself.
    """
    _, tenant_id = auth

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

    messages = _fetch_conversation_messages(
        db, conversation_id, tenant_id, mode=mode, entity_id=entity_id
    )

    if not messages:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # RND-158 Phase 2 QA round 7 (blocker 5, conversation_type policy):
    # cross-check the caller-supplied hint against the type
    # _fetch_conversation_messages actually resolved for this id/entity
    # context -- group if any resolved message carries a real roomid
    # (_is_valid_roomid, the same truthiness rule
    # _derive_conversation_membership uses), direct otherwise. A mismatch
    # means the caller's cached/stale conversation_type no longer matches
    # this bucket (e.g. an entity-scoped collision resolved to the other
    # side) -- surfaced as 400 rather than silently served under the
    # wrong assumption.
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

    # RND-158 Phase 2 QA round 7 (blocker 3, media context propagation):
    # precompute the query-string suffix carrying this request's entity
    # context (mode/staff_id/contact_id/conversation_type), appended to
    # every media_url/media_access_url this response generates below so a
    # client following one of those URLs preserves the same context the
    # timeline itself was resolved with. Empty ("") when no entity context
    # was supplied on this request -- generated media URLs are then
    # byte-identical to previous rounds' behavior.
    media_context_qs = _entity_context_query_string(
        mode, staff_id, contact_id, conversation_type
    )

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
                )
            structured_content_out = {
                "fields": out_fields,
                "parse_warnings": raw_structured.get("parse_warnings", []),
            }

        media_url: Optional[str] = None
        media_access_url: Optional[str] = None
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
                media_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media"
                    f"{media_context_qs}"
                )
                media_access_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media/access"
                    f"{media_context_qs}"
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
                media_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media"
                    f"{media_context_qs}"
                )
                media_access_url = (
                    f"/api/conversations/{conversation_id}/messages/{msg.msgid}/media/access"
                    f"{media_context_qs}"
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


def _resolve_authorized_media(
    db: Session,
    conversation_id: str,
    msgid: str,
    tenant_id: str,
    *,
    mode: Optional[str] = None,
    entity_id: Optional[str] = None,
    conversation_type: Optional[str] = None,
) -> Tuple[ArchiveMessage, MediaFile]:
    """
    Shared authorization + lookup for both media-serving routes
    (get_message_media, get_message_media_access).

    conversation_type (RND-158 Phase 2 QA round 8, blocker 2): optional
    consistency check, mirroring the exact policy
    get_conversation_messages already enforces (see that route's
    docstring, "conversation_type policy"). The caller has already
    validated this is exactly "direct"/"group" or None before reaching
    here (garbage input is a 400 independent of whether any message is
    found). Once a message is resolved below, this checks it against that
    ONE message's own actual type -- "group" if it carries a real roomid
    (_is_valid_roomid, the same truthiness rule
    _derive_conversation_membership and the timeline route use), "direct"
    otherwise. A message belongs to exactly one side, so no aggregation
    across a message set is needed here the way the timeline route
    aggregates across a whole page. Implemented once, here, rather than
    duplicated in both route bodies, so a consistency fix automatically
    applies to both callers.

    Authenticated (get_current_user, by every caller), tenant-scoped
    (tenant_id comes only from the session, never a request param), and
    conversation-scoped (the message must actually belong to
    conversation_id per _fetch_conversation_messages — the same membership
    rules the timeline route uses). The media_files lookup is additionally
    filtered by tenant_id directly (RND-156) — a second, independent check
    on top of message ownership, so a media row can never be served on the
    strength of archive_message_id alone even if message/media tenant
    assignment were ever to diverge.

    mode/entity_id (RND-158 Phase 2 QA round 7, blocker 3): optional entity
    context, forwarded verbatim into `_fetch_conversation_messages` — the
    exact same parameters and semantics the timeline route
    (get_conversation_messages) already uses. tenant_id still comes only
    from the session, never from these or any other request param, so
    passing entity context here never weakens tenant isolation.

    Passing this context through means a media request for a message that
    only exists on one side of a genuine direct/group collision resolves
    (and authorizes) against the same entity-scoped message set the
    timeline used to generate this URL — an entity that does not
    participate in that side gets a 404 here (the message simply is not
    in `_fetch_conversation_messages`'s filtered result), same as it would
    for any other message it has no part in. A legacy ID-only request
    (mode/entity_id both None) on a genuinely ambiguous collision raises
    the same 400 `_fetch_conversation_messages` already raises for the
    ID-only timeline case — this function does not catch or convert it.

    This authorization sequence completes in full *before* any storage
    provider, URL, or signing operation is touched (RND-174/RND-187) — both
    media routes share this exact function so a security fix here
    automatically applies to both.
    """
    messages = _fetch_conversation_messages(
        db, conversation_id, tenant_id, mode=mode, entity_id=entity_id
    )
    msg = next((m for m in messages if m.msgid == msgid), None)
    if msg is None:
        raise HTTPException(status_code=404, detail="Not found")

    # RND-158 Phase 2 QA round 8 (blocker 2, media conversation_type
    # consistency): mirror get_conversation_messages's own policy exactly
    # -- validated for shape by the caller already, cross-checked here
    # against this specific message's own actual type once resolved.
    if conversation_type is not None:
        resolved_conversation_type = (
            "group" if _is_valid_roomid(msg.roomid) else "direct"
        )
        if conversation_type != resolved_conversation_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"conversation_type mismatch: requested '{conversation_type}' but "
                    f"this conversation resolved to '{resolved_conversation_type}'"
                ),
            )

    if msg.msgtype not in SERVABLE_MEDIA_MSGTYPES:
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

    return msg, media_file


def _resolve_servable_backend_and_ref(media_file: MediaFile, route_label: str) -> Tuple[str, str]:
    """
    Resolve (effective_backend, effective_ref) for an already-authorized
    media_file row (see _resolve_authorized_media) and confirm it is
    actually servable, raising the shared 404/500/503 HTTPException
    taxonomy both media routes use.

    route_label only selects the log-line prefix (e.g. "media route" vs
    "media access route") — never included in the HTTP response.

    getattr(..., None): a MediaFile ORM row always has these columns, but
    duck-typed test doubles may not.
    """
    effective_backend, effective_ref = resolve_effective_storage_reference(
        getattr(media_file, "storage_backend", None),
        getattr(media_file, "storage_ref", None),
        getattr(media_file, "local_path", None),
    )
    if effective_backend is None:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        file_state = resolve_downloadable_media_file_state(effective_ref, effective_backend)
    except MediaStorageConfigurationError:
        logger.error(
            "%s: storage configuration error (backend=%s)", route_label, effective_backend
        )
        raise HTTPException(status_code=500, detail="Media storage is misconfigured")
    except MediaStorageUnavailable:
        logger.warning(
            "%s: storage provider unavailable (backend=%s)", route_label, effective_backend
        )
        raise HTTPException(status_code=503, detail="Media storage temporarily unavailable")

    if file_state != "servable":
        raise HTTPException(status_code=404, detail="Not found")

    return effective_backend, effective_ref


@router.get("/api/conversations/{conversation_id}/messages/{msgid}/media")
def get_message_media(
    conversation_id: str,
    msgid: str,
    mode: Optional[str] = Query(
        None,
        description=(
            "Optional entity context: 'staff' or 'contact'. Same semantics "
            "as GET .../messages -- required to unambiguously authorize a "
            "message that only exists on one side of a direct/group "
            "collision. Without it, a genuinely ambiguous conversation_id "
            "returns 400, same as the timeline route."
        ),
    ),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    conversation_type: Optional[str] = Query(
        None,
        description=(
            "Optional consistency check, 'direct' or 'group' -- same "
            "validation as GET .../messages. Never used to select or "
            "filter the message itself."
        ),
    ),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Serve an already-downloaded image message's file content (RND-144).

    mode/staff_id/contact_id/conversation_type (RND-158 Phase 2 QA round
    7, blocker 3): optional entity context, identical params/validation to
    GET .../messages, forwarded into _resolve_authorized_media so a media
    request generated from an entity-scoped timeline resolves against the
    same entity-scoped message set the timeline used -- see
    _resolve_authorized_media's docstring for the collision/authorization
    contract this establishes.

    Kept unchanged by RND-187 for backward compatibility: local-backed rows
    still only serve this way, and this is still a valid (if no longer the
    primary) access path for Qiniu-backed rows too — see
    get_message_media_access for the RND-187 signed-URL descriptor route
    the frontend now calls first for image messages.

    See _resolve_authorized_media's docstring for the full authorization-
    boundary rationale (RND-174/RND-156): the sequence completes in full
    *before* any storage provider is selected or called — the provider is
    never part of the authorization boundary.

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
    if conversation_type is not None and conversation_type not in ("direct", "group"):
        raise HTTPException(
            status_code=400, detail="conversation_type must be 'direct' or 'group'"
        )
    entity_id = _resolve_entity_context(mode, staff_id, contact_id)

    _msg, media_file = _resolve_authorized_media(
        db,
        conversation_id,
        msgid,
        tenant_id,
        mode=mode,
        entity_id=entity_id,
        conversation_type=conversation_type,
    )
    effective_backend, effective_ref = _resolve_servable_backend_and_ref(
        media_file, "media route"
    )

    provider = get_media_storage_provider(effective_backend)

    if provider.supports_local_path():
        safe_path = resolve_servable_downloadable_media_path(effective_ref, effective_backend)
        if safe_path is None:
            raise HTTPException(status_code=404, detail="Not found")
        content_type = detect_media_content_type_for_ref(str(safe_path))
        return FileResponse(path=str(safe_path), media_type=content_type)

    # Cloud-backed media (RND-174): still a valid controlled access path —
    # fetch the bytes through the provider and proxy them back. The
    # response shape (raw bytes, same URL, same content-type behavior) is
    # unchanged. No Qiniu URL or credential ever reaches the client
    # through this route. detect_media_content_type_for_ref covers every
    # RND-199 supported media category (image/video/voice/file), not
    # image only.
    content_type = detect_media_content_type_for_ref(effective_ref)
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


@router.get(
    "/api/conversations/{conversation_id}/messages/{msgid}/media/access",
    response_model=MediaAccessOut,
)
def get_message_media_access(
    conversation_id: str,
    msgid: str,
    response: Response,
    mode: Optional[str] = Query(
        None,
        description=(
            "Optional entity context: 'staff' or 'contact'. Same semantics "
            "as GET .../messages -- required to unambiguously authorize a "
            "message that only exists on one side of a direct/group "
            "collision. Without it, a genuinely ambiguous conversation_id "
            "returns 400, same as the timeline route."
        ),
    ),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    conversation_type: Optional[str] = Query(
        None,
        description=(
            "Optional consistency check, 'direct' or 'group' -- same "
            "validation as GET .../messages. Never used to select or "
            "filter the message itself."
        ),
    ),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Return a unified media access descriptor (RND-187) instead of proxying
    image bytes. The frontend calls this first for every image message,
    then loads the actual image from descriptor.url — it never needs to
    know which storage_backend served it.

    mode/staff_id/contact_id/conversation_type (RND-158 Phase 2 QA round
    7, blocker 3): optional entity context, identical params/validation to
    GET .../messages, forwarded into _resolve_authorized_media. Also
    echoed onto the returned proxy url (access_type="proxy" / local
    backend) so a client following it preserves the same context.

    access_type="proxy" (local-backed rows): url is the existing
    get_message_media route, unchanged. expires_at is None — the URL
    carries no time-boxed credential of its own; the session cookie
    authorizes each request to it, same as before RND-187.

    access_type="signed_url" (Qiniu-backed rows): url is a short-lived
    Qiniu signed URL, valid for get_signed_url_ttl_seconds() seconds
    (MEDIA_SIGNED_URL_TTL_SECONDS, default 900, bounded [60, 3600]). Minted
    only after this route completes the exact same authorization sequence
    as get_message_media (_resolve_authorized_media /
    _resolve_servable_backend_and_ref) *plus* an explicit check that the
    object key's own "tenants/{tenant_id}/" prefix agrees with this row's
    authenticated tenant_id (object_key_tenant_prefix_matches) — so a
    corrupted/mistagged row can never mint a signed URL for a different
    tenant's object on the strength of the tenant_id column alone. The
    signed URL itself is never logged or included in any exception.

    Cache-Control: no-store on every response this endpoint can produce —
    success or error, any status code, including a 401 raised by
    get_current_user() before this function body even runs. The header set
    below covers the success path (FastAPI copies headers mutated on an
    injected Response parameter onto the final response for a returned
    Pydantic model); MediaAccessNoStoreMiddleware (registered on the app in
    main.py) independently guarantees the same header on every error path,
    since an exception's Response is built fresh by FastAPI/Starlette and
    never sees this function's local `response` mutation. Both mechanisms
    target the same header value, so keeping this line is redundant but
    harmless on the success path, not dead code.

    Response codes: same 404/500/503 taxonomy and meaning as
    get_message_media (wrong tenant/conversation, non-image, missing row,
    unservable file, misconfigured backend, provider outage, object-key
    tenant-prefix mismatch). 502 additionally covers a confirmed signed-URL
    generation failure against a provider that did respond.
    """
    response.headers["Cache-Control"] = "no-store"
    _, tenant_id = auth
    if conversation_type is not None and conversation_type not in ("direct", "group"):
        raise HTTPException(
            status_code=400, detail="conversation_type must be 'direct' or 'group'"
        )
    entity_id = _resolve_entity_context(mode, staff_id, contact_id)

    _msg, media_file = _resolve_authorized_media(
        db,
        conversation_id,
        msgid,
        tenant_id,
        mode=mode,
        entity_id=entity_id,
        conversation_type=conversation_type,
    )
    effective_backend, effective_ref = _resolve_servable_backend_and_ref(
        media_file, "media access route"
    )
    content_type = detect_media_content_type_for_ref(effective_ref)
    proxy_context_qs = _entity_context_query_string(mode, staff_id, contact_id, conversation_type)

    if effective_backend == "local":
        return MediaAccessOut(
            media_id=media_file.id,
            storage_backend="local",
            access_type="proxy",
            url=f"/api/conversations/{conversation_id}/messages/{msgid}/media{proxy_context_qs}",
            expires_at=None,
            content_type=content_type,
            size_bytes=media_file.file_size,
        )

    if effective_backend == "qiniu_kodo":
        if not object_key_tenant_prefix_matches(effective_ref, tenant_id):
            logger.error(
                "media access route: object key tenant prefix mismatch (media_id=%s)",
                media_file.id,
            )
            raise HTTPException(status_code=404, detail="Not found")

        try:
            ttl_seconds = get_signed_url_ttl_seconds()
        except MediaStorageConfigurationError:
            logger.error("media access route: invalid signed url TTL configuration")
            raise HTTPException(status_code=500, detail="Media storage is misconfigured")

        provider = get_media_storage_provider(effective_backend)
        try:
            signed_url = provider.get_download_url(effective_ref, expires_in=ttl_seconds)
        except MediaObjectNotFound:
            raise HTTPException(status_code=404, detail="Not found")
        except MediaStorageConfigurationError:
            logger.error(
                "media access route: signed url configuration error (media_id=%s)",
                media_file.id,
            )
            raise HTTPException(status_code=500, detail="Media storage is misconfigured")
        except MediaStorageOperationError:
            logger.error(
                "media access route: signed url generation failed (media_id=%s)", media_file.id
            )
            raise HTTPException(status_code=502, detail="Media storage operation failed")

        if not signed_url:
            logger.error(
                "media access route: signed url generation returned empty (media_id=%s)",
                media_file.id,
            )
            raise HTTPException(status_code=502, detail="Media storage operation failed")

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        logger.info(
            "media access route: signed url issued (media_id=%s, tenant_id=%s, "
            "backend=qiniu_kodo, ttl=%s)",
            media_file.id,
            tenant_id,
            ttl_seconds,
        )
        return MediaAccessOut(
            media_id=media_file.id,
            storage_backend="qiniu_kodo",
            access_type="signed_url",
            url=signed_url,
            expires_at=expires_at,
            content_type=content_type,
            size_bytes=media_file.file_size,
        )

    logger.error(
        "media access route: unsupported backend for access descriptor (backend=%s)",
        effective_backend,
    )
    raise HTTPException(status_code=500, detail="Media storage is misconfigured")


@router.get("/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}")
def get_nested_message_media(
    conversation_id: str,
    msgid: str,
    item_path: str,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Serve an already-downloaded nested mixed/chatrecord media item's file
    content (RND-200 QA fix) — the nested-item analogue of
    get_message_media, reusing every storage/content-type primitive it
    uses. Not a second implementation: only _resolve_authorized_media is
    swapped for _resolve_authorized_nested_media (which additionally
    validates item_path and resolves via sdkfileid instead of
    archive_message_id — see its docstring); every step after that is
    identical to get_message_media, including the exact response-code
    taxonomy and the "never log/return sdkfileid, local_path, storage_ref,
    oss_key" guarantee.

    item_path is untrusted URL input, strictly validated against a fixed
    grammar (see _validate_nested_media_path) before ever being used —
    never eval'd, never used as a filesystem path, never used to index an
    arbitrary object graph. A malformed path is rejected with 400; a
    well-formed path that does not resolve to a media-bearing node on
    THIS message is rejected with 404 — the same 404 as "wrong
    conversation" or "wrong tenant", so a caller cannot distinguish
    "path doesn't exist" from "you can't see this message" by response
    shape alone.
    """
    _, tenant_id = auth

    _msg, _ref, media_file = _resolve_authorized_nested_media(
        db, conversation_id, msgid, item_path, tenant_id
    )
    effective_backend, effective_ref = _resolve_servable_backend_and_ref(
        media_file, "nested media route"
    )

    provider = get_media_storage_provider(effective_backend)

    if provider.supports_local_path():
        safe_path = resolve_servable_downloadable_media_path(effective_ref, effective_backend)
        if safe_path is None:
            raise HTTPException(status_code=404, detail="Not found")
        content_type = detect_media_content_type_for_ref(str(safe_path))
        return FileResponse(path=str(safe_path), media_type=content_type)

    content_type = detect_media_content_type_for_ref(effective_ref)
    try:
        data = provider.read_bytes(effective_ref)
    except MediaObjectNotFound:
        raise HTTPException(status_code=404, detail="Not found")
    except MediaStorageUnavailable:
        logger.warning(
            "nested media route: storage provider unavailable during read (backend=%s)",
            effective_backend,
        )
        raise HTTPException(status_code=503, detail="Media storage temporarily unavailable")
    except MediaStorageOperationError:
        logger.error(
            "nested media route: storage operation failed during read (backend=%s)",
            effective_backend,
        )
        raise HTTPException(status_code=502, detail="Media storage operation failed")
    return Response(content=data, media_type=content_type)


@router.get(
    "/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}/access",
    response_model=NestedMediaAccessOut,
)
def get_nested_message_media_access(
    conversation_id: str,
    msgid: str,
    item_path: str,
    response: Response,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Return a public access descriptor for one nested mixed/chatrecord
    media item (RND-200 QA fix) — the nested-item analogue of
    get_message_media_access, reusing the exact same signed-URL/proxy
    branching, TTL config, and object-key tenant-prefix check. Not a
    second storage-access implementation: only the authorization/lookup
    step differs (_resolve_authorized_nested_media instead of
    _resolve_authorized_media) — see its docstring for why resolution is
    keyed by sdkfileid rather than archive_message_id.

    Returns NestedMediaAccessOut, NOT MediaAccessOut (RND-200 QA
    security fix, second round): MediaAccessOut.media_id exposes the
    internal MediaFile primary key, which independent QA correctly
    rejected as a leak for the nested contract — the approved public
    handle for nested media is (conversation_id, msgid, item_path) only,
    never a database id. media_file.id is still used locally in this
    function (log lines only, never returned to a caller) for
    operator-facing diagnostics, exactly as before.

    access_type="proxy": url is get_nested_message_media (this same
    conversation_id/msgid/item_path), not the top-level get_message_media
    route — a nested item's bytes are never served through the single-
    media parent route, since a mixed/chatrecord message can hold more
    than one media item and the parent route has no way to disambiguate
    which one is meant.

    Cache-Control: no-store on every response — see
    MediaAccessNoStoreMiddleware, whose path pattern covers this route
    too.
    """
    response.headers["Cache-Control"] = "no-store"
    _, tenant_id = auth

    _msg, _ref, media_file = _resolve_authorized_nested_media(
        db, conversation_id, msgid, item_path, tenant_id
    )
    effective_backend, effective_ref = _resolve_servable_backend_and_ref(
        media_file, "nested media access route"
    )
    content_type = detect_media_content_type_for_ref(effective_ref)

    if effective_backend == "local":
        return NestedMediaAccessOut(
            storage_backend="local",
            access_type="proxy",
            url=(
                f"/api/conversations/{conversation_id}/messages/{msgid}"
                f"/nested-media/{item_path}"
            ),
            expires_at=None,
            mime_type=content_type,
            filename=None,
            size_bytes=media_file.file_size,
        )

    if effective_backend == "qiniu_kodo":
        if not object_key_tenant_prefix_matches(effective_ref, tenant_id):
            logger.error(
                "nested media access route: object key tenant prefix mismatch (media_id=%s)",
                media_file.id,
            )
            raise HTTPException(status_code=404, detail="Not found")

        try:
            ttl_seconds = get_signed_url_ttl_seconds()
        except MediaStorageConfigurationError:
            logger.error("nested media access route: invalid signed url TTL configuration")
            raise HTTPException(status_code=500, detail="Media storage is misconfigured")

        provider = get_media_storage_provider(effective_backend)
        try:
            signed_url = provider.get_download_url(effective_ref, expires_in=ttl_seconds)
        except MediaObjectNotFound:
            raise HTTPException(status_code=404, detail="Not found")
        except MediaStorageConfigurationError:
            logger.error(
                "nested media access route: signed url configuration error (media_id=%s)",
                media_file.id,
            )
            raise HTTPException(status_code=500, detail="Media storage is misconfigured")
        except MediaStorageOperationError:
            logger.error(
                "nested media access route: signed url generation failed (media_id=%s)",
                media_file.id,
            )
            raise HTTPException(status_code=502, detail="Media storage operation failed")

        if not signed_url:
            logger.error(
                "nested media access route: signed url generation returned empty (media_id=%s)",
                media_file.id,
            )
            raise HTTPException(status_code=502, detail="Media storage operation failed")

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        logger.info(
            "nested media access route: signed url issued (media_id=%s, tenant_id=%s, "
            "backend=qiniu_kodo, ttl=%s)",
            media_file.id,
            tenant_id,
            ttl_seconds,
        )
        return NestedMediaAccessOut(
            storage_backend="qiniu_kodo",
            access_type="signed_url",
            url=signed_url,
            expires_at=expires_at,
            mime_type=content_type,
            filename=None,
            size_bytes=media_file.file_size,
        )

    logger.error(
        "nested media access route: unsupported backend for access descriptor (backend=%s)",
        effective_backend,
    )
    raise HTTPException(status_code=500, detail="Media storage is misconfigured")
