"""Listing service (RND-219) — monitored-accounts / contacts / conversation-list.

Functions here were moved (not reimplemented) out of
app.routers.conversations so that the three listing endpoints
(get_monitored_accounts, get_contacts, get_conversations) reduce to thin
HTTP wrappers: parameter validation -> call a function here -> return.

Scope note: only helpers reachable EXCLUSIVELY from those three endpoints
live here. Helpers shared with the message-timeline / media endpoints
(_load_display_names, _is_valid_roomid, and everything already in
app.conversation_membership) stay in their existing shared module and are
imported from there, never redefined here, so the router and this service
can never diverge on shared logic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.conversation_membership import (
    _collect_archive_participant_ids,
    _collect_staff_ids,
    _derive_conversation_membership,
    _entity_seed_ids,
    _is_staff,
    _is_valid_roomid,
    _load_display_names_for_ids,
    _staff_ids_for_participants,
)
from app.db.group_chat_metadata import load_group_chat_display_names
from app.db.models import ArchiveMessage, ArchiveMessageRecipient
from app.display_names import resolve_person_display_name, resolve_room_display_name
from app.schemas.listing import ContactOut, MonitoredAccountOut
from app.services.avatar_sync import (
    external_avatar_presentations,
    internal_avatar_presentations,
)
from app.services.external_contact_identity import external_contact_display_names


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


def _batch_latest_own_participation_time(
    db: Session, tenant_id: str, entity_ids: set[str]
) -> dict[str, int]:
    """Batch counterpart to _latest_own_participation_time: the identical
    max(sender-side msgtime, recipient-side msgtime) result for every id in
    entity_ids, computed with two GROUP BY queries total instead of two
    queries PER id. Used ONLY by list_monitored_accounts (RND-191), which
    previously called _latest_own_participation_time once per seat -- 2*S
    queries for S seats. Absent ids (no participation at all) are simply
    missing from the returned dict, matching the singular function's None
    return for that case."""
    if not entity_ids:
        return {}
    entity_list = list(entity_ids)
    result: dict[str, int] = {}
    for uid, max_time in (
        db.query(ArchiveMessage.sender, func.max(ArchiveMessage.msgtime))
        .filter(
            ArchiveMessage.sender.in_(entity_list),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .group_by(ArchiveMessage.sender)
        .all()
    ):
        if max_time is not None:
            result[uid] = max_time
    for uid, max_time in (
        db.query(ArchiveMessageRecipient.receiver_userid, func.max(ArchiveMessage.msgtime))
        .join(ArchiveMessage, ArchiveMessage.id == ArchiveMessageRecipient.message_id)
        .filter(
            ArchiveMessageRecipient.receiver_userid.in_(entity_list),
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessage.tenant_id == tenant_id,
        )
        .group_by(ArchiveMessageRecipient.receiver_userid)
        .all()
    ):
        if max_time is not None and (uid not in result or max_time > result[uid]):
            result[uid] = max_time
    return result


def _batch_count_entity_conversations(
    db: Session,
    tenant_id: str,
    entity_ids: set[str],
    staff_ids: Optional[set[str]] = None,
) -> dict[str, int]:
    """Batch counterpart to _count_entity_conversations: the identical
    canonical-conversation count for every id in entity_ids, computed with
    a small constant number of queries instead of the ~5-6 queries PER id
    _count_entity_conversations/_compact_entity_messages issue. Used ONLY
    by list_monitored_accounts (RND-191), which previously called
    _count_entity_conversations once per seat -- roughly 5-6*S queries for
    S seats.

    Mirrors _compact_entity_messages's seed-then-expand-then-derive logic
    (same canonical _derive_conversation_membership call, same group-room
    expansion rule) exactly, but computed for every entity in one pass: a
    group room shared by several seats is expanded once here, not once per
    seat that happens to touch it.
    """
    if not entity_ids:
        return {}
    entity_list = list(entity_ids)

    seed_ids_by_entity: dict[str, set[int]] = {eid: set() for eid in entity_list}
    for mid, sender in (
        db.query(ArchiveMessage.id, ArchiveMessage.sender)
        .filter(
            ArchiveMessage.sender.in_(entity_list),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    ):
        seed_ids_by_entity[sender].add(mid)
    for mid, uid in (
        db.query(ArchiveMessageRecipient.message_id, ArchiveMessageRecipient.receiver_userid)
        .filter(
            ArchiveMessageRecipient.receiver_userid.in_(entity_list),
            ArchiveMessageRecipient.tenant_id == tenant_id,
        )
        .all()
    ):
        seed_ids_by_entity[uid].add(mid)

    all_seed_ids: set[int] = set()
    for ids in seed_ids_by_entity.values():
        all_seed_ids.update(ids)
    if not all_seed_ids:
        return {eid: 0 for eid in entity_list}

    # (sender, roomid) for every seed message, one query -- extended below
    # with every group-room message's (sender, roomid) too, so this one map
    # covers every message id either loop below needs to classify.
    row_by_id: dict[int, tuple[Optional[str], Optional[str]]] = {
        r[0]: (r[1], r[2])
        for r in db.query(ArchiveMessage.id, ArchiveMessage.sender, ArchiveMessage.roomid)
        .filter(
            ArchiveMessage.id.in_(all_seed_ids),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .all()
    }

    group_rooms_by_entity: dict[str, set[str]] = {}
    all_group_rooms: set[str] = set()
    for eid, ids in seed_ids_by_entity.items():
        rooms = {
            row_by_id[mid][1]
            for mid in ids
            if mid in row_by_id and _is_valid_roomid(row_by_id[mid][1])
        }
        group_rooms_by_entity[eid] = rooms
        all_group_rooms.update(rooms)

    room_ids_by_room: dict[str, set[int]] = {room: set() for room in all_group_rooms}
    if all_group_rooms:
        for mid, sender, roomid in (
            db.query(ArchiveMessage.id, ArchiveMessage.sender, ArchiveMessage.roomid)
            .filter(
                ArchiveMessage.roomid.in_(all_group_rooms),
                ArchiveMessage.tenant_id == tenant_id,
            )
            .all()
        ):
            room_ids_by_room[roomid].add(mid)
            row_by_id[mid] = (sender, roomid)

    final_ids_by_entity: dict[str, set[int]] = {}
    all_final_ids: set[int] = set()
    for eid in entity_list:
        direct_ids = {
            mid
            for mid in seed_ids_by_entity.get(eid, set())
            if mid in row_by_id and not _is_valid_roomid(row_by_id[mid][1])
        }
        expanded_ids: set[int] = set()
        for room in group_rooms_by_entity.get(eid, set()):
            expanded_ids.update(room_ids_by_room.get(room, set()))
        final_ids = direct_ids | expanded_ids
        final_ids_by_entity[eid] = final_ids
        all_final_ids.update(final_ids)

    if not all_final_ids:
        return {eid: 0 for eid in entity_list}

    recipients_map = _load_recipients_map_compact(db, tenant_id, list(all_final_ids))

    def is_staff(uid: str) -> bool:
        if staff_ids is not None:
            return uid in staff_ids
        return uid.startswith("staff_")

    result: dict[str, int] = {}
    for eid in entity_list:
        canonical_keys: set[str] = set()
        for mid in final_ids_by_entity[eid]:
            sender, roomid = row_by_id.get(mid, (None, None))
            recipients = recipients_map.get(mid, [])
            conv_id, _conv_type, _staff_set, _contact_set = _derive_conversation_membership(
                sender, roomid, recipients, is_staff
            )
            canonical_keys.add(conv_id)
        result[eid] = len(canonical_keys)
    return result


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
    benchmark notes). The SQL projection limits content_text to its first
    200 characters, exactly the amount the response can expose; every other
    heavy column is dropped from the projection.

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

    direct_ids = {r[0] for r in seed_rows if not _is_valid_roomid(r[2])}
    final_filters = []
    if group_rooms:
        final_filters.append(ArchiveMessage.roomid.in_(group_rooms))
    if direct_ids:
        final_filters.append(ArchiveMessage.id.in_(direct_ids))
    if not final_filters:
        return []

    final_rows = (
        db.query(
            ArchiveMessage.id,
            ArchiveMessage.sender,
            ArchiveMessage.roomid,
            ArchiveMessage.msgtime,
            # The list response exposes only the first 200 characters of
            # the latest message.  Truncate in SQL so a long archived body
            # never crosses the DB/Python boundary just to be sliced below.
            # SQLite and PostgreSQL both support the three-argument substr
            # form and count text characters, matching Python's [:200].
            func.substr(ArchiveMessage.content_text, 1, 200),
        )
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            or_(*final_filters),
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


def _fetch_group_conversation_summaries(
    db: Session, tenant_id: str, group_rooms: set[str]
) -> list:
    """Return one compact latest-row summary per requested group room.

    The review console's initial staff view renders a group as one card; it
    does not render the full inferred participant roster on that card.  Do
    the count and latest-row selection in SQL so a long group history is not
    materialized as one Python object per message before it can become one
    conversation summary.

    ``coalesce(msgtime, 0), id`` deliberately mirrors
    _build_conversation_list's deterministic latest-message key, including
    rows whose archive timestamp is NULL.  Window functions used here are
    supported by both PostgreSQL and the SQLite versions this project tests.
    """
    if not group_rooms:
        return []

    ranked = (
        db.query(
            ArchiveMessage.id.label("latest_message_id"),
            ArchiveMessage.roomid.label("roomid"),
            ArchiveMessage.sender.label("latest_sender_id"),
            ArchiveMessage.msgtime.label("last_message_time"),
            func.substr(ArchiveMessage.content_text, 1, 200).label("last_message_text"),
            func.count(ArchiveMessage.id)
            .over(partition_by=ArchiveMessage.roomid)
            .label("message_count"),
            func.row_number()
            .over(
                partition_by=ArchiveMessage.roomid,
                order_by=(
                    func.coalesce(ArchiveMessage.msgtime, 0).desc(),
                    ArchiveMessage.id.desc(),
                ),
            )
            .label("recency_rank"),
        )
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.roomid.in_(group_rooms),
        )
        .subquery()
    )
    return (
        db.query(
            ranked.c.latest_message_id,
            ranked.c.roomid,
            ranked.c.latest_sender_id,
            ranked.c.last_message_time,
            ranked.c.last_message_text,
            ranked.c.message_count,
        )
        .filter(ranked.c.recency_rank == 1)
        .all()
    )


def _build_conversation_list(
    messages: list,
    recipients_map: dict[int, list[str]],
    display_names: dict[str, str],
    staff_ids: Optional[set[str]] = None,
    room_display_names: Optional[dict[str, str]] = None,
    *,
    include_participant_metadata: bool = True,
    include_internal_latest_message_id: bool = False,
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
    independent of latest-message selection: the (msgtime, id) running max
    below still picks "latest" purely from the messages in each bucket,
    untouched by which message set conversation_type.
    """
    convs: dict[str, dict] = {}
    room_display_names = room_display_names or {}

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
                # Preserve only the running latest message and count.  The
                # previous implementation retained every message in each
                # bucket and sorted each bucket just to determine its latest
                # row.  A max over the same (msgtime, id) key is equivalent,
                # while keeping the list endpoint linear in its message set.
                "latest": None,
                "latest_sort_key": None,
                "message_count": 0,
            }
        elif conv_type == "group" and convs[conv_id]["conversation_type"] == "direct":
            # Group-wins collision upgrade: a later-processed group-shaped
            # message overrides an earlier direct-shaped one that landed
            # in the same bucket. Never the reverse (group is never
            # downgraded to direct).
            convs[conv_id]["conversation_type"] = "group"
            convs[conv_id]["roomid"] = msg.roomid if roomid else None

        # A compact staff-console response needs direct-conversation
        # participants to resolve its title, but a group card needs only its
        # room metadata.  Avoid accumulating every observed group member
        # when participant metadata is deliberately omitted from the API
        # response.
        if include_participant_metadata or conv_type == "direct":
            convs[conv_id]["monitored_account_ids"].update(staff_set)
            convs[conv_id]["contact_ids"].update(contact_set)
        data = convs[conv_id]
        message_sort_key = (msg.msgtime or 0, msg.id)
        if data["latest_sort_key"] is None or message_sort_key > data["latest_sort_key"]:
            data["latest"] = msg
            data["latest_sort_key"] = message_sort_key
        data["message_count"] += 1

    result = []
    for conv_id, data in convs.items():
        # Deterministic message recency contract: the running max above uses
        # the same (msgtime, id) key the previous per-bucket sort used.
        # `id` makes equal-timestamp messages reproducible without relying
        # on incidental database row order.
        latest = data["latest"]

        sids = sorted(data["monitored_account_ids"])
        cids = sorted(data["contact_ids"])

        if data["conversation_type"] == "group":
            room_raw_id = data["roomid"] or conv_id
            room_display_name = resolve_room_display_name(
                room_raw_id, room_display_names.get(room_raw_id)
            )
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

        row = {
                "conversation_id": conv_id,
                "conversation_type": data["conversation_type"],
                "display_name": display_name,
                "raw_id": raw_id,
                "roomid": data["roomid"],
                "monitored_account_ids": sids if include_participant_metadata else [],
                "monitored_account_raw_ids": sids if include_participant_metadata else [],
                "monitored_account_display_names": (
                    [resolve_person_display_name(sid, display_names.get(sid)) for sid in sids]
                    if include_participant_metadata
                    else []
                ),
                "contact_ids": cids if include_participant_metadata else [],
                "contact_raw_ids": cids if include_participant_metadata else [],
                "contact_display_names": (
                    [resolve_person_display_name(cid, display_names.get(cid)) for cid in cids]
                    if include_participant_metadata
                    else []
                ),
                "room_display_name": room_display_name,
                "room_raw_id": room_raw_id,
                "last_message_time": latest.msgtime,
                "last_message_text": (latest.content_text or "")[:200],
                "message_count": data["message_count"],
                "latest_sender_id": latest_sender_id,
                "latest_sender_raw_id": latest_sender_id,
                "latest_sender_display_name": latest_sender_display_name,
                "review_status": None,
                "ai_status": None,
                "ai_summary": None,
            }
        if include_internal_latest_message_id:
            row["_latest_message_id"] = latest.id
        result.append(row)

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


def _list_compact_staff_conversations(
    db: Session, tenant_id: str, entity_id: str
) -> list[dict]:
    """Fast staff-console listing without group participant metadata.

    Direct messages retain the established Python aggregation because their
    canonical IDs depend on sender/recipient membership.  Group rooms have a
    stable conversation ID already, so their count and latest preview can be
    summarized in SQL.  This preserves the existing group-wins collision
    policy while avoiding group-history and group-recipient materialization.
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
    group_rooms = {roomid for _id, _sender, roomid in seed_rows if _is_valid_roomid(roomid)}
    direct_ids = {message_id for message_id, _sender, roomid in seed_rows if not _is_valid_roomid(roomid)}

    direct_messages = []
    if direct_ids:
        direct_rows = (
            db.query(
                ArchiveMessage.id,
                ArchiveMessage.sender,
                ArchiveMessage.roomid,
                ArchiveMessage.msgtime,
                func.substr(ArchiveMessage.content_text, 1, 200),
            )
            .filter(
                ArchiveMessage.id.in_(direct_ids),
                ArchiveMessage.tenant_id == tenant_id,
            )
            .order_by(ArchiveMessage.msgtime.asc(), ArchiveMessage.id.asc())
            .all()
        )
        direct_messages = [
            SimpleNamespace(
                id=row[0], sender=row[1], roomid=row[2], msgtime=row[3], content_text=row[4]
            )
            for row in direct_rows
        ]

    recipients_map = _load_recipients_map_compact(
        db, tenant_id, [message.id for message in direct_messages]
    )
    direct_participant_ids: set[str] = {
        message.sender for message in direct_messages if message.sender
    }
    for recipient_ids in recipients_map.values():
        direct_participant_ids.update(recipient_ids)

    display_names = _load_display_names_for_ids(db, tenant_id, direct_participant_ids)
    staff_ids = _staff_ids_for_participants(db, tenant_id, direct_participant_ids)
    follow_userid = entity_id if entity_id in staff_ids else None
    display_names.update(
        external_contact_display_names(
            db,
            tenant_id,
            direct_participant_ids,
            follow_userid=follow_userid,
        )
    )
    direct_conversations = _build_conversation_list(
        direct_messages,
        recipients_map,
        display_names,
        staff_ids,
        include_participant_metadata=False,
        include_internal_latest_message_id=True,
    )

    room_display_names = load_group_chat_display_names(db, tenant_id, group_rooms)
    conversations_by_id = {
        conversation["conversation_id"]: conversation for conversation in direct_conversations
    }
    for summary in _fetch_group_conversation_summaries(db, tenant_id, group_rooms):
        roomid = summary.roomid
        group_conversation = {
            "conversation_id": roomid,
            "conversation_type": "group",
            "display_name": resolve_room_display_name(
                roomid, room_display_names.get(roomid)
            ),
            "raw_id": roomid,
            "roomid": roomid,
            "monitored_account_ids": [],
            "monitored_account_raw_ids": [],
            "monitored_account_display_names": [],
            "contact_ids": [],
            "contact_raw_ids": [],
            "contact_display_names": [],
            "room_display_name": resolve_room_display_name(
                roomid, room_display_names.get(roomid)
            ),
            "room_raw_id": roomid,
            "last_message_time": summary.last_message_time,
            "last_message_text": summary.last_message_text or "",
            "message_count": summary.message_count,
            "latest_sender_id": summary.latest_sender_id,
            "latest_sender_raw_id": summary.latest_sender_id,
            "latest_sender_display_name": resolve_person_display_name(
                summary.latest_sender_id, None
            )
            if summary.latest_sender_id
            else None,
            "review_status": None,
            "ai_status": None,
            "ai_summary": None,
            "_latest_message_id": summary.latest_message_id,
        }
        existing = conversations_by_id.get(roomid)
        if existing is None:
            conversations_by_id[roomid] = group_conversation
            continue

        # _build_conversation_list's group-wins policy also merges the two
        # colliding buckets' counts and selects the latest (msgtime, id).
        # Retain that exact behavior even on this compact path.
        combined_count = existing["message_count"] + group_conversation["message_count"]
        existing_key = (
            existing["last_message_time"] or 0,
            existing["_latest_message_id"],
        )
        group_key = (
            group_conversation["last_message_time"] or 0,
            group_conversation["_latest_message_id"],
        )
        if existing_key > group_key:
            existing.update(
                {
                    "conversation_type": "group",
                    "display_name": group_conversation["display_name"],
                    "raw_id": roomid,
                    "roomid": roomid,
                    "monitored_account_ids": [],
                    "monitored_account_raw_ids": [],
                    "monitored_account_display_names": [],
                    "contact_ids": [],
                    "contact_raw_ids": [],
                    "contact_display_names": [],
                    "room_display_name": group_conversation["room_display_name"],
                    "room_raw_id": roomid,
                    "message_count": combined_count,
                }
            )
        else:
            group_conversation["message_count"] = combined_count
            conversations_by_id[roomid] = group_conversation

    result = list(conversations_by_id.values())
    for conversation in result:
        conversation.pop("_latest_message_id", None)
    result.sort(
        key=lambda conversation: (
            conversation["last_message_time"] or 0,
            conversation["conversation_id"],
        ),
        reverse=True,
    )
    return result


def list_monitored_accounts(
    db: Session,
    tenant_id: str,
    *,
    include_conversation_count: bool = True,
) -> list[MonitoredAccountOut]:
    """
    Return all WeCom archive seats (monitored accounts) for this tenant —
    both the currently active seat and historical seats with archived
    records — sorted active-first, then by latest_message_time descending.

    See _collect_staff_ids() and app.routers.conversations' module
    docstring for how seats are identified in the absence of a formal
    seat-roster source.

    latest_message_time (and therefore active/history ranking) is computed
    from _batch_latest_own_participation_time() — each seat's own
    sender/recipient rows only, never the group-room-expanded set
    _fetch_messages_for_entity() returns. conversation_count still uses the
    expanded set: that is a session-viewing concern (how many threads to
    show under this seat), not a classification concern (RND-132 QA fix —
    see _latest_own_participation_time docstring for why these two must
    stay separate).

    RND-191: latest_message_time and conversation_count are computed for
    ALL seats in two batched calls (_batch_latest_own_participation_time,
    _batch_count_entity_conversations) instead of looping the singular
    per-seat _latest_own_participation_time/_count_entity_conversations —
    those two calls alone cost roughly 7 queries per seat (RND-158
    profiling), so a tenant with S seats issued ~7*S queries here. display_
    names is likewise scoped to just this tenant's staff_ids rather than
    loading every Contact row in the tenant.

    The API's default preserves the complete summary response.  Callers that
    do not render conversation counts (the initial review-console picker)
    can explicitly skip that full archive aggregation and receive ``null``
    for ``conversation_count`` instead.
    """
    staff_ids = _collect_staff_ids(db, tenant_id)
    if not staff_ids:
        return []

    display_names = _load_display_names_for_ids(db, tenant_id, staff_ids)
    avatars = internal_avatar_presentations(db, tenant_id, staff_ids)
    latest_times = _batch_latest_own_participation_time(db, tenant_id, staff_ids)
    conversation_counts = (
        _batch_count_entity_conversations(db, tenant_id, staff_ids, staff_ids)
        if include_conversation_count
        else {}
    )

    seats: list[dict] = []
    for sid in staff_ids:
        latest_message_time = latest_times.get(sid)
        if latest_message_time is None:
            # No direct participation at all for this identity — not a seat
            # worth surfacing (definition requires archived records where
            # the seat is literally the sender or a listed recipient).
            continue
        seats.append(
            {
                "staff_id": sid,
                "latest_message_time": latest_message_time,
                "conversation_count": (
                    conversation_counts.get(sid, 0) if include_conversation_count else None
                ),
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
                avatar_url=avatars[sid].url,
                avatar_status=avatars[sid].status,
                seat_status="active" if is_active else "history",
                is_active_archive_seat=is_active,
                latest_message_time=seat["latest_message_time"],
                conversation_count=seat["conversation_count"],
            )
        )
    return result


def list_contacts(db: Session, tenant_id: str) -> list[ContactOut]:
    """Return all contacts (non-staff participants) observed in the tenant archive.

    RND-191: participant_ids is computed once and passed into
    _collect_staff_ids so it doesn't redundantly repeat its own tenant-wide
    _collect_archive_participant_ids() scan; display_names is scoped to
    just the contacts being returned rather than every Contact row in the
    tenant (which can include staff and off-archive-graph contacts this
    response never surfaces).
    """
    participant_ids = _collect_archive_participant_ids(db, tenant_id)
    staff_ids = _collect_staff_ids(db, tenant_id, participant_ids)
    contact_ids = participant_ids - staff_ids
    display_names = _load_display_names_for_ids(db, tenant_id, contact_ids)
    avatars = internal_avatar_presentations(db, tenant_id, contact_ids)
    # An external identity's customer-level avatar supersedes the archive
    # registry's generic Contact cache without using a display name as a key.
    avatars.update(external_avatar_presentations(db, tenant_id, contact_ids))
    # No employee context exists in contact-centered selection, so the
    # identity helper intentionally uses a real nickname or opaque fallback,
    # never a randomly selected employee remark.
    display_names.update(external_contact_display_names(db, tenant_id, contact_ids))
    return [
        ContactOut(
            contact_id=cid,
            display_name=resolve_person_display_name(cid, display_names.get(cid)),
            raw_id=cid,
            avatar_url=avatars[cid].url,
            avatar_status=avatars[cid].status,
        )
        for cid in sorted(contact_ids)
    ]


def list_conversations(
    db: Session,
    tenant_id: str,
    entity_id: str,
    *,
    include_participant_metadata: bool = True,
) -> list[dict]:
    """Return conversations for a monitored account or contact entity_id.

    Sorted by last activity descending. Returns plain dicts (matching
    _build_conversation_list's return shape) — the route's
    response_model=list[ConversationOut] handles serialization, exactly as
    the original inline get_conversations() body did.
    """
    if not include_participant_metadata:
        return _list_compact_staff_conversations(db, tenant_id, entity_id)

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
    # A staff-centered archive view is the explicit employee context in which
    # that employee's own remark is the correct direct-conversation title.
    # A contact-centered view passes no follow user and therefore never picks
    # another employee's remark arbitrarily.
    follow_userid = entity_id if entity_id in staff_ids else None
    display_names.update(
        external_contact_display_names(
            db,
            tenant_id,
            participant_ids,
            follow_userid=follow_userid,
        )
    )
    room_display_names = load_group_chat_display_names(
        db, tenant_id, (message.roomid for message in messages)
    )
    return _build_conversation_list(
        messages,
        recipients_map,
        display_names,
        staff_ids,
        room_display_names,
    )
