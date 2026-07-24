# Shared conversation membership service module
#
# Functions here were precisely MOVED (not reimplemented) from
# app.routers.conversations so that the conversations router AND the
# reachability audit replay the exact same membership logic from a single
# source of truth. Keep this file behavior-equivalent to the original
# definitions: do NOT change query semantics (and_/or_ composition, tenant
# scoping) here. The conversations router imports these names; it must not
# redefine them.

from __future__ import annotations

from typing import Optional, Tuple, Callable

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient, AdminUser, Contact
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



def _load_display_names(db: Session, tenant_id: str) -> dict[str, Optional[str]]:
    """Return {wecom_userid: name} raw from Contact.name, scoped to tenant.

    Values are never blank (Contact rows are only ever created with a
    non-blank name — see upsert_contact_display_name), but a given ID may
    simply be absent from the dict if no Contact row exists yet. Callers
    resolve the final display label via resolve_person_display_name, which
    supplies the raw-ID fallback for absent/blank entries.

    RND-219: moved here (unchanged) because it is shared by both the
    monitored-accounts/contacts listing endpoints (app.services.listing_service)
    and the message-timeline endpoint (app.routers.conversations), so neither
    side may redefine it.
    """
    return {
        c.wecom_userid: c.name
        for c in db.query(Contact).filter(Contact.tenant_id == tenant_id).all()
    }


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



def _is_valid_roomid(roomid: Optional[str]) -> bool:
    """Return True if roomid should be treated as a group conversation
    identifier. Matches _derive_conversation_membership's truthiness rule
    (``roomid = roomid or ""`` then ``if roomid:``), so that this function
    and the authoritative builder agree on every input:
      - None/empty roomid → direct message (falsy after ``or ""``)
      - non-empty roomid (including whitespace-only) → group (truthy)

    RND-219: moved here (unchanged) because it is shared by both the
    listing service's compact-projection helpers and the message-timeline /
    media routes left in app.routers.conversations.
    """
    return bool(roomid)


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
