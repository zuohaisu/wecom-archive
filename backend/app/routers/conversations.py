"""
Conversation aggregation APIs for the Crowntime WeCom Archive review console.

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
import os
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, load_only

from app.auth import get_current_user
from app.db.group_chat_metadata import load_group_chat_display_names
from app.db.models import (
    AdminUser,
    ArchiveMessage,
)
from app.db.session import get_db
from app.conversation_membership import (
    _entity_seed_ids,
    # RND-220: no longer called directly in this module (the timeline
    # service now loads these itself) but kept importable from here —
    # test_tenant_isolation.py/test_staff_seats.py import _load_recipients_map
    # directly, and test_conversation_membership_service.py's anti-divergence
    # guard asserts both stay bound to the exact same object as the service.
    _load_recipients_map,  # noqa: F401
    _load_display_names,  # noqa: F401
    # RND-221: no longer called directly in this module either (the media
    # routes that used to call these two moved to app.services.media_access,
    # which imports them directly from app.conversation_membership) but kept
    # importable from here — test_tenant_isolation.py/test_staff_seats.py
    # import _fetch_conversation_messages directly, and
    # test_conversation_membership_service.py's anti-divergence guard
    # asserts both stay bound to the exact same object as the service.
    _fetch_conversation_messages,  # noqa: F401
    _is_valid_roomid,  # noqa: F401
    # Not called directly in this module anymore, but
    # test_conversation_membership_service.py asserts these stay gettable
    # from this module and bound to the exact same object as the service
    # (anti-divergence guard predating RND-219) — keep re-exporting.
    _is_staff,  # noqa: F401
    _derive_conversation_membership,  # noqa: F401
    # Backward-compatible re-exports owned by the shared service. Tests and
    # older call sites import these names from the router module; re-exporting
    # them (instead of redefining) keeps the router and service bound to the
    # *same* function objects, so behavior can never diverge. None of these
    # four are called directly in this module anymore (RND-219 moved their
    # only call sites into app.services.listing_service), but they must stay
    # importable from here for existing direct-import test call sites.
    _collect_staff_ids,  # noqa: F401
    _collect_archive_participant_ids,  # noqa: F401
    _load_display_names_for_ids,  # noqa: F401
    _staff_ids_for_participants,  # noqa: F401
    _direct_conv_id,  # noqa: F401
    _resolve_conversation_message_ids,
)
from app.schemas.listing import (
    ContactOut,
    ConversationDetailOut,
    ConversationOut,
    ConversationParticipantOut,
    MonitoredAccountOut,
)
from app.services import listing_service
from app.services.external_contact_identity import external_contact_display_names
from app.services.listing_service import (
    # RND-219: monitored-accounts / contacts / conversation-list logic moved
    # to app.services.listing_service; the three thin routes below call
    # listing_service.list_* directly. These six names are re-exported
    # (not redefined) purely for backward compatibility with existing
    # direct-import test call sites — no code in this module calls them.
    #
    # Archive Console v2 (design import): get_conversation_detail below is
    # a FOURTH caller of _build_conversation_list -- reused, not
    # reimplemented, to derive the 会话信息 panel's participants from the
    # exact same staff/contact aggregation GET /api/conversations already
    # uses, scoped to one already-resolved conversation's messages.
    _build_conversation_list,  # noqa: F401
    _compact_entity_messages,  # noqa: F401
    _count_entity_conversations,  # noqa: F401
    _fetch_compact_messages_for_entity,  # noqa: F401
    _latest_own_participation_time,  # noqa: F401
    _load_recipients_map_compact,  # noqa: F401
)
from app.schemas.timeline import (
    ConversationMessagesOut,
    PaginationOut,  # noqa: F401
    TimelineMessageOut,  # noqa: F401
)
from app.services import timeline_service
from app.services.timeline_service import (
    # RND-220: conversation resolution, cursor pagination, timeline
    # projection, revoke fold, and nested media enrichment orchestration
    # moved to app.services.timeline_service. resolve_timeline_page is
    # called directly by the thin get_conversation_messages handler below;
    # every other name here is re-exported (not redefined) purely for
    # backward compatibility with existing direct-import test call sites —
    # same convention as the conversation_membership / listing_service
    # re-exports above. (RND-221: the media routes that used to also
    # consume some of these moved to app.routers.media, which now imports
    # them directly from app.services.timeline_service instead.)
    INTERNAL_STRUCTURED_FIELD_KEYS,  # noqa: F401
    PUBLIC_STRUCTURED_FIELD_ALLOWLIST,  # noqa: F401
    _build_nested_media_descriptor,  # noqa: F401
    _datetime_to_epoch_ms,  # noqa: F401
    _decode_message_cursor,  # noqa: F401
    _encode_message_cursor,  # noqa: F401
    _enrich_nested_media_fields,  # noqa: F401
    _ensure_aware,  # noqa: F401
    _load_media_files_by_sdkfileid_map,  # noqa: F401
    _load_media_files_map,  # noqa: F401
    _load_revocations_map,  # noqa: F401
    _project_public_structured_fields,  # noqa: F401
    _RevocationMaps,  # noqa: F401
    _thumbnail_timeline_fields,  # noqa: F401
    attach_group_chat_display_name,
    resolve_timeline_page,
)
from app.schemas.media import (
    # RND-221: moved to app.schemas.media (the response_model for the two
    # media access routes now defined in app.routers.media). Re-exported
    # (not redefined) purely for backward compatibility with existing
    # direct-import test call sites.
    MediaAccessOut,  # noqa: F401
    NestedMediaAccessOut,  # noqa: F401
)
from app.services.media_access import (
    # RND-221: unified media authorization/provider-resolution/byte-response/
    # descriptor service backing app.routers.media. Re-exported (not
    # redefined) purely for backward compatibility with existing
    # direct-import test call sites.
    _NESTED_MEDIA_MAX_PATH_SEGMENTS,  # noqa: F401
    _find_nested_media_ref,  # noqa: F401
    _validate_nested_media_path,  # noqa: F401
)

router = APIRouter()

logger = logging.getLogger(__name__)

# _load_media_files_by_sdkfileid_map, _build_nested_media_descriptor,
# _enrich_nested_media_fields moved to app.services.timeline_service
# (RND-220; imported above, re-exported for the tests that import them
# directly from this module).

# _is_valid_roomid moved to conversation_membership service (imported)


# _compact_entity_messages, _count_entity_conversations,
# _latest_own_participation_time, _fetch_compact_messages_for_entity,
# _load_recipients_map_compact, and _build_conversation_list moved to
# app.services.listing_service (imported above).


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

    RND-219: this function has no call sites in this file (or anywhere else
    in production code) — it predates the RND-158 compact-projection
    rewrite and is kept only because tests still import/compare against it
    (see test_staff_seats.py's _authoritative_count). Left in place
    unchanged rather than moved into app.services.listing_service, since it
    is not exclusive to (or even used by) the three listing endpoints that
    ticket moved.
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


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

# MonitoredAccountOut, ContactOut, ConversationOut moved to
# app.schemas.listing (imported above) — RND-219.
# TimelineMessageOut, PaginationOut, ConversationMessagesOut moved to
# app.schemas.timeline (imported above) — RND-220.
# MediaAccessOut, NestedMediaAccessOut moved to app.schemas.media (imported
# above) — RND-221.

# _encode_message_cursor, _decode_message_cursor moved to
# app.services.timeline_service (RND-220; imported above, re-exported).


# ---------------------------------------------------------------------------
# Routes — all protected by get_current_user
# ---------------------------------------------------------------------------


@router.get("/api/monitored-accounts", response_model=list[MonitoredAccountOut])
def get_monitored_accounts(
    include_conversation_count: bool = Query(
        True,
        description=(
            "Whether to calculate the full conversation count for every monitored account. "
            "The review-console picker sets this false because it does not display counts."
        ),
    ),
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

    Query/aggregation logic lives in app.services.listing_service (RND-219).
    """
    _, tenant_id = auth
    return listing_service.list_monitored_accounts(
        db,
        tenant_id,
        include_conversation_count=include_conversation_count,
    )


@router.get("/api/contacts", response_model=list[ContactOut])
def get_contacts(
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """Return all contacts (non-staff participants) observed in the tenant archive.

    Query logic lives in app.services.listing_service (RND-219).
    """
    _, tenant_id = auth
    return listing_service.list_contacts(db, tenant_id)


@router.get("/api/conversations", response_model=list[ConversationOut])
def get_conversations(
    mode: str = Query(..., description="'staff' or 'contact'"),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    include_participant_metadata: bool = Query(
        True,
        description=(
            "Whether to return inferred participant metadata. The staff console's "
            "conversation-card list sets this false because it does not render it."
        ),
    ),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """Return conversations for a monitored account (mode=staff) or contact (mode=contact).

    Sorted by last activity descending.

    Query/aggregation logic lives in app.services.listing_service (RND-219).
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

    return listing_service.list_conversations(
        db,
        tenant_id,
        entity_id,
        # Compact summaries are intentionally a staff-console optimization.
        # Contact cards render their related monitored-account badge, so they
        # retain the complete participant metadata even if a caller sends the
        # optional flag on that mode.
        include_participant_metadata=(
            include_participant_metadata or mode != "staff"
        ),
    )


# INTERNAL_STRUCTURED_FIELD_KEYS, PUBLIC_STRUCTURED_FIELD_ALLOWLIST,
# _project_public_structured_fields moved to app.services.timeline_service
# (RND-220; imported above, re-exported).


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

    Aggregation, pagination, structured-field projection, revoke fold, and
    nested media enrichment all live in app.services.timeline_service
    (RND-220) -- this handler only validates/converts HTTP input and
    dispatches to resolve_timeline_page. See that function's docstring for
    the full behavior contract (cursor shape, conversation_id formats,
    conversation_type policy).

    WEARCHIVE_LEGACY_TIMELINE (short-term rollback facade, RND-220): when
    this environment variable is set (any truthy string), dispatches to
    _resolve_timeline_page_legacy -- a byte-identical pre-extraction
    snapshot -- instead. Exists purely as an instant (env var + restart,
    no redeploy) rollback path if the new service path ever regresses in
    production; not intended to diverge from resolve_timeline_page and
    should be removed by a future cleanup ticket once the new path has
    been stable.
    """
    _, tenant_id = auth
    resolver = (
        timeline_service._resolve_timeline_page_legacy
        if os.environ.get("WEARCHIVE_LEGACY_TIMELINE")
        else resolve_timeline_page
    )
    page = resolver(
        db,
        tenant_id,
        conversation_id,
        limit=limit,
        before=before,
        mode=mode,
        staff_id=staff_id,
        contact_id=contact_id,
        conversation_type=conversation_type,
    )
    return attach_group_chat_display_name(
        db,
        tenant_id,
        conversation_id,
        conversation_type,
        page,
    )


# get_message_media, get_message_media_access, get_nested_message_media,
# get_nested_message_media_access -- and the MediaAccessNoStoreMiddleware
# that guarantees Cache-Control: no-store on the two /access routes --
# moved to app.routers.media (RND-221). Authorization, storage-backend/
# provider resolution, byte-response serving, and access-descriptor
# construction are unified there in app.services.media_access.


@router.get(
    "/api/conversations/{conversation_id}/detail",
    response_model=ConversationDetailOut,
)
def get_conversation_detail(
    conversation_id: str,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Archive Console v2 (design import) -- conversation-level stats and
    inferred participants for the review console's 会话信息 panel. Additive:
    does not change GET /api/conversations or any other existing endpoint.

    Reuses _resolve_conversation_message_ids (RND-240: an id-only mirror of
    _fetch_conversation_messages's exact tenant-scoped membership
    resolution -- see its docstring for why this endpoint can't just call
    _fetch_conversation_messages directly and still be fast) and
    _build_conversation_list (the exact staff/contact aggregation
    GET /api/conversations uses) — see app.conversation_membership /
    app.services.listing_service — instead of a parallel reimplementation.

    `participants` is explicitly an INFERENCE over archived messages (every
    distinct sender/recipient observed among this conversation's resolved
    messages), never a live WeCom room roster — this system has no such
    sync, and the frontend labels it accordingly (never presented as
    ground truth). `decrypted_percent` is computed only from
    ArchiveMessage.decrypt_status among those same resolved messages, via a
    SQL aggregate rather than materializing every row.

    RND-240: for a conversation with thousands of messages, the previous
    implementation (`_fetch_conversation_messages` -> full ArchiveMessage
    ORM rows, including the large raw_encrypted_payload/decrypted_payload/
    structured_content JSONB columns nothing here ever reads) was the
    dominant cost of loading this panel. This resolves membership as bare
    ids first (`_resolve_conversation_message_ids`), computes
    `decrypted_percent` with a COUNT aggregate, and only re-fetches
    `ArchiveMessage` rows through a `load_only` projection restricted to
    the columns `_build_conversation_list` actually reads
    (id/sender/roomid/msgtime/content_text) to build `participants`.
    """
    _, tenant_id = auth
    ids = _resolve_conversation_message_ids(db, conversation_id, tenant_id)
    if not ids:
        raise HTTPException(status_code=404, detail="Conversation not found")

    total = len(ids)
    decrypted_count = (
        db.query(func.count(ArchiveMessage.id))
        .filter(ArchiveMessage.id.in_(ids), ArchiveMessage.decrypt_status == "success")
        .scalar()
        or 0
    )
    decrypted_percent = round(decrypted_count * 100.0 / total, 1)

    slim_messages = (
        db.query(ArchiveMessage)
        .options(
            load_only(
                ArchiveMessage.id,
                ArchiveMessage.sender,
                ArchiveMessage.roomid,
                ArchiveMessage.msgtime,
                ArchiveMessage.content_text,
            )
        )
        .filter(ArchiveMessage.id.in_(ids))
        .all()
    )

    recipients_map = _load_recipients_map(db, tenant_id, ids)
    participant_ids: set[str] = {m.sender for m in slim_messages if m.sender}
    for recipient_ids in recipients_map.values():
        participant_ids.update(recipient_ids)
    display_names = _load_display_names_for_ids(db, tenant_id, participant_ids)
    staff_ids = _staff_ids_for_participants(db, tenant_id, participant_ids)
    # This detail route has no selected employee context. Do not select one
    # employee's remark for a shared customer; use real nickname/fallback.
    display_names.update(external_contact_display_names(db, tenant_id, participant_ids))

    room_display_names = load_group_chat_display_names(
        db, tenant_id, (message.roomid for message in slim_messages)
    )
    buckets = _build_conversation_list(
        slim_messages,
        recipients_map,
        display_names,
        staff_ids,
        room_display_names,
    )
    bucket = next(
        (b for b in buckets if b["conversation_id"] == conversation_id),
        buckets[0],
    )

    participants = [
        ConversationParticipantOut(id=sid, raw_id=sid, display_name=name, role="staff")
        for sid, name in zip(
            bucket["monitored_account_ids"], bucket["monitored_account_display_names"]
        )
    ] + [
        ConversationParticipantOut(id=cid, raw_id=cid, display_name=name, role="contact")
        for cid, name in zip(bucket["contact_ids"], bucket["contact_display_names"])
    ]

    return ConversationDetailOut(
        conversation_id=conversation_id,
        message_count=total,
        decrypted_percent=decrypted_percent,
        participants=participants,
    )
