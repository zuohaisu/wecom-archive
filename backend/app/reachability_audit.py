"""
Message Reachability Audit (RND-178).

An archived message (archive_messages row with decrypt_status="success") is
only useful if the review console can actually show it in some conversation
timeline. Between decrypt success and the timeline API there are several
places a message can silently fall through:

  - archive_message_recipients rows never get written (recipient
    persistence failure) — direct conversations depend on these rows.
  - roomid is missing even though the message fans out to multiple
    recipients (a group-shaped message with no room anchor).
  - sender is null/unresolvable, so the message cannot be attributed to
    any conversation participant.
  - all of the above look fine, but the real conversation-membership
    lookup still doesn't return the message (a genuine aggregation bug).

This module classifies every successfully-archived message into exactly one
of these outcomes instead of leaving "message is in the DB but nobody knows
why it doesn't show up" unexplained.

Design constraint: the final "is this message actually reachable" check
must replay the *same* membership function the timeline API uses
(_fetch_conversation_messages in app.routers.conversations), not a parallel
reimplementation — see build_message_reachability_report(). This keeps the
audit from ever diverging from what a console user actually sees.

Fault tolerance constraint: an audit must never abort because the archived
data it is scanning is malformed — that is exactly the class of problem it
exists to surface. _fetch_conversation_messages raises HTTPException on a
malformed conversation_id (e.g. a self-recipient row deriving the
single-party id "direct__staff_a" instead of a proper "a___b" pair); this
module both narrows the candidate set to keep such rows out in the first
place (see _apply_conversation_candidate_filter) and, defensively, catches
any exception from that lookup so one malformed message is classified
unreachable_membership rather than crashing the rest of the scan (see
membership_found_for in build_message_reachability_report()).

Safety constraint: nothing in this module's output may include message
content, raw payloads, WeCom msgid/seq/sdkfileid, media keys, or external
contact identifiers beyond the internal database id. See
MessageReachabilitySample for the full allow-list of exposed fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, false, or_
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient


class ReachabilityStatus(str, Enum):
    REACHABLE_DIRECT = "reachable_direct"
    REACHABLE_GROUP = "reachable_group"
    UNREACHABLE_MISSING_RECIPIENT = "unreachable_missing_recipient"
    UNREACHABLE_MISSING_ROOM = "unreachable_missing_room"
    UNREACHABLE_MISSING_SENDER = "unreachable_missing_sender"
    UNREACHABLE_MEMBERSHIP = "unreachable_membership"
    UNREACHABLE_OTHER = "unreachable_other"


REACHABLE_STATUSES = frozenset(
    {ReachabilityStatus.REACHABLE_DIRECT, ReachabilityStatus.REACHABLE_GROUP}
)

# A message with no roomid but a multi-recipient fan-out looks like a WeCom
# group broadcast that lost its room anchor, not a 1:1 direct message — see
# _infer_conversation_shape.
_GROUP_LIKE_RECIPIENT_THRESHOLD = 2

# Internal safety caps — never let a single audit call scan or sample an
# unbounded number of rows regardless of what the caller requests.
MAX_SCAN_LIMIT = 2000
DEFAULT_SCAN_LIMIT = 500
MAX_SAMPLE_LIMIT = 200
DEFAULT_SAMPLE_LIMIT = 20


@dataclass(frozen=True)
class MessageReachabilitySample:
    """
    One sanitized, per-message audit row.

    Every field here is on the explicit allow-list from the RND-178 spec.
    Never add message content, raw payloads, msgid/seq/sdkfileid, media
    keys, or external-contact identifiers to this dataclass.
    """

    message_db_id: int
    reachability_status: str
    reason_code: str
    message_type: Optional[str]
    conversation_type: Optional[str]
    has_sender: bool
    has_room: bool
    recipient_count: int
    timeline_visible: bool
    msg_time: Optional[int] = None
    created_at: Optional[str] = None


def _infer_conversation_shape(has_room: bool, recipient_count: int) -> str:
    """
    Infer what kind of conversation a message would belong to, from data
    alone. This schema has no separate "room" table or stored authoring
    intent — roomid is the only signal that a message was sent into a
    group. A missing roomid combined with a multi-recipient fan-out
    (>= _GROUP_LIKE_RECIPIENT_THRESHOLD) mirrors that group-broadcast shape
    without a room id to anchor it, which is exactly the scenario
    unreachable_missing_room exists to catch.
    """
    if has_room:
        return "group"
    if recipient_count >= _GROUP_LIKE_RECIPIENT_THRESHOLD:
        return "group_like_missing_room"
    return "direct"


def classify_message_reachability(
    *,
    decrypt_status: str,
    has_sender: bool,
    has_room: bool,
    recipient_count: int,
    membership_found: bool,
) -> Tuple[ReachabilityStatus, str]:
    """
    Pure decision function — no DB access, no side effects.

    membership_found must already reflect a real replay of the timeline
    API's membership check (see build_message_reachability_report); this
    function only decides which bucket the combination of facts belongs to.

    Check order mirrors the RND-178 spec's step list:
      1. message must be a successful archive (caller's scan already
         filters on this; defended here too).
      2. sender must be resolvable.
      3. room/group shape must be consistent with roomid presence.
      4. recipient rows must exist for a direct-shaped message.
      5. the real conversation-membership lookup must actually return it.
    """
    if decrypt_status != "success":
        return ReachabilityStatus.UNREACHABLE_OTHER, "not_decrypt_success"

    if not has_sender:
        return ReachabilityStatus.UNREACHABLE_MISSING_SENDER, "sender_null_or_unresolvable"

    shape = _infer_conversation_shape(has_room, recipient_count)

    if shape == "group_like_missing_room":
        return (
            ReachabilityStatus.UNREACHABLE_MISSING_ROOM,
            "roomid_missing_for_multi_recipient_fanout",
        )

    if shape == "direct" and recipient_count == 0:
        return (
            ReachabilityStatus.UNREACHABLE_MISSING_RECIPIENT,
            "direct_message_has_no_recipient_rows",
        )

    if not membership_found:
        return (
            ReachabilityStatus.UNREACHABLE_MEMBERSHIP,
            "conversation_membership_lookup_did_not_return_message",
        )

    if shape == "group":
        return ReachabilityStatus.REACHABLE_GROUP, "ok"
    return ReachabilityStatus.REACHABLE_DIRECT, "ok"


def _empty_status_counts() -> Dict[str, int]:
    return {status.value: 0 for status in ReachabilityStatus}


def build_message_reachability_report(
    db: Session,
    tenant_id: str,
    *,
    conversation_id: Optional[str] = None,
    message_type: Optional[str] = None,
    msgtime_from: Optional[int] = None,
    msgtime_to: Optional[int] = None,
    limit: int = DEFAULT_SCAN_LIMIT,
    offset: int = 0,
    include_samples: bool = False,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict:
    """
    Audit every successfully-archived message in `tenant_id` (optionally
    narrowed by conversation_id / message_type / msgtime range) and report
    how many are reachable vs. unreachable, and why.

    tenant_id must come from the caller's authenticated session — this
    function never accepts tenant scope from anywhere else, matching every
    other tenant-scoped query in this codebase.

    conversation_id narrows the scan to messages belonging to that *exact*
    direct pair or room — see _apply_conversation_candidate_filter for the
    precise matching rules (it intentionally still matches on raw
    sender/recipient/roomid signals rather than the real membership
    function's output, so a message that has already fallen out of that
    conversation can still be surfaced as unreachable instead of silently
    excluded).

    This call scans at most `limit` rows starting at `offset` — it is a
    page, not a guarantee that the whole tenant archive was checked.
    matching_total/has_more in the returned report make that explicit; a
    caller that wants full-archive coverage must page through with
    offset += scanned_count until has_more is false.

    Returns aggregate counts always; per-message samples only when
    include_samples=True, capped at sample_limit (itself capped at
    MAX_SAMPLE_LIMIT). No message content, raw payload, or WeCom/media
    identifiers are ever included — see MessageReachabilitySample.
    """
    limit = max(1, min(limit, MAX_SCAN_LIMIT))
    offset = max(0, offset)
    sample_limit = max(0, min(sample_limit, MAX_SAMPLE_LIMIT))

    # TODO(RND-179/RND-180): _collect_staff_ids / _derive_conversation_membership /
    # _fetch_conversation_messages / _is_staff are private helpers of
    # app.routers.conversations, imported here so the audit replays the exact
    # same membership logic instead of a parallel reimplementation (see
    # module docstring). Extracting them into a shared, non-router service
    # module (e.g. app/conversation_membership.py) would remove this
    # cross-module private import, but conversations.py is a large,
    # heavily-tested router — that extraction is deliberately deferred to a
    # follow-up rather than folded into this focused fix patch.
    #
    # Imported lazily (not at module scope) to avoid a circular import: the
    # router module in turn does not import this module, but keeping this
    # runtime-local documents the direction of dependency deliberately.
    from app.routers.conversations import (
        _collect_staff_ids,
        _derive_conversation_membership,
        _fetch_conversation_messages,
        _is_staff,
    )

    query = db.query(ArchiveMessage).filter(
        ArchiveMessage.tenant_id == tenant_id,
        ArchiveMessage.decrypt_status == "success",
    )
    if message_type:
        query = query.filter(ArchiveMessage.msgtype == message_type)
    if msgtime_from is not None:
        query = query.filter(ArchiveMessage.msgtime >= msgtime_from)
    if msgtime_to is not None:
        query = query.filter(ArchiveMessage.msgtime <= msgtime_to)

    if conversation_id:
        query = _apply_conversation_candidate_filter(query, conversation_id, tenant_id)

    # matching_total is computed on the filtered-but-unpaginated query, so it
    # always reflects every message matching tenant/filter criteria — not
    # just the page this call happens to scan. Computed before order_by is
    # applied (order_by is irrelevant to COUNT and some backends choke on
    # ordering an aggregate query).
    matching_total = query.count()

    messages = (
        query.order_by(ArchiveMessage.id).offset(offset).limit(limit).all()
    )
    scanned_count = len(messages)
    has_more = (offset + scanned_count) < matching_total

    message_ids = [m.id for m in messages]
    recipients_map = _load_recipient_userids_map(db, message_ids, tenant_id)

    staff_ids = _collect_staff_ids(db, tenant_id)

    def is_staff(uid: str) -> bool:
        if staff_ids:
            return uid in staff_ids
        return _is_staff(uid)

    counts_by_status = _empty_status_counts()
    counts_by_message_type: Dict[str, int] = {}
    counts_by_conversation_type: Dict[str, int] = {}
    samples: List[MessageReachabilitySample] = []

    # Cache real membership lookups per conversation_id within this run —
    # several messages typically share the same room/pair, and each lookup
    # replays a real DB query (_fetch_conversation_messages).
    # Cached value is (message_ids_found, errored) — errored is True when
    # the lookup itself raised (e.g. a malformed derived conversation_id
    # such as "direct__staff_a" with no "___" separator, which
    # _fetch_conversation_messages rejects with HTTPException). The audit
    # must never abort because archived data is malformed — that is
    # exactly what it exists to detect — so any such failure is caught
    # here and turned into an unreachable classification instead of
    # propagating, and the rest of the scan continues.
    membership_cache: Dict[str, Tuple[set, bool]] = {}

    def membership_found_for(conv_id: str) -> Tuple[bool, bool]:
        if conv_id not in membership_cache:
            try:
                found = _fetch_conversation_messages(db, conv_id, tenant_id)
                membership_cache[conv_id] = ({m.id for m in found}, False)
            except Exception:
                membership_cache[conv_id] = (set(), True)
        found_ids, errored = membership_cache[conv_id]
        return message.id in found_ids, errored

    for message in messages:
        recipients = recipients_map.get(message.id, [])
        has_sender = bool(message.sender)
        has_room = bool(message.roomid)
        recipient_count = len(recipients)
        shape = _infer_conversation_shape(has_room, recipient_count)

        needs_membership_check = has_sender and (
            (shape == "direct" and recipient_count > 0) or shape == "group"
        )
        membership_found = False
        membership_errored = False
        conv_type_hint: Optional[str] = None
        if needs_membership_check:
            conv_id, conv_type, _staff_set, _contact_set = _derive_conversation_membership(
                message.sender, message.roomid, recipients, is_staff
            )
            conv_type_hint = conv_type
            membership_found, membership_errored = membership_found_for(conv_id)
        elif shape in ("group", "group_like_missing_room"):
            conv_type_hint = "group"
        elif shape == "direct":
            conv_type_hint = "direct"

        status, reason_code = classify_message_reachability(
            decrypt_status=message.decrypt_status,
            has_sender=has_sender,
            has_room=has_room,
            recipient_count=recipient_count,
            membership_found=membership_found,
        )
        if membership_errored and status == ReachabilityStatus.UNREACHABLE_MEMBERSHIP:
            # Distinguish "membership lookup ran and returned nothing" from
            # "membership lookup itself failed on malformed data" without
            # adding a branch to the pure classifier — same status, more
            # specific (still safe/static) reason code.
            reason_code = "conversation_membership_lookup_errored"

        counts_by_status[status.value] += 1
        mtype_key = message.msgtype or "unknown"
        counts_by_message_type[mtype_key] = counts_by_message_type.get(mtype_key, 0) + 1
        ctype_key = conv_type_hint or "unknown"
        counts_by_conversation_type[ctype_key] = (
            counts_by_conversation_type.get(ctype_key, 0) + 1
        )

        if include_samples and len(samples) < sample_limit:
            samples.append(
                MessageReachabilitySample(
                    message_db_id=message.id,
                    reachability_status=status.value,
                    reason_code=reason_code,
                    message_type=message.msgtype,
                    conversation_type=conv_type_hint,
                    has_sender=has_sender,
                    has_room=has_room,
                    recipient_count=recipient_count,
                    timeline_visible=status in REACHABLE_STATUSES,
                    msg_time=message.msgtime,
                    created_at=(
                        message.created_at.isoformat() if message.created_at else None
                    ),
                )
            )

    reachable_count = sum(
        counts_by_status[s.value] for s in REACHABLE_STATUSES
    )

    report = {
        # scanned_count: rows actually classified by this call (the page).
        # matching_total: every message matching tenant/filter criteria,
        # regardless of pagination — this is what makes it explicit that a
        # single call may not have covered the whole archive.
        "scanned_count": scanned_count,
        "matching_total": matching_total,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "max_scan_limit": MAX_SCAN_LIMIT,
        "reachable_count": reachable_count,
        "unreachable_count": scanned_count - reachable_count,
        "counts_by_status": counts_by_status,
        "counts_by_message_type": counts_by_message_type,
        "counts_by_conversation_type": counts_by_conversation_type,
    }
    if include_samples:
        report["samples"] = [s.__dict__ for s in samples]
    return report


def _load_recipient_userids_map(
    db: Session, message_ids: List[int], tenant_id: str
) -> Dict[int, List[str]]:
    """
    Return {message_id: [receiver_userid, ...]} for the given message ids.

    Defensively scoped by tenant_id on the recipient row itself (not just
    on the parent message) — message_id.in_(message_ids) alone trusts that
    every archive_message_recipients row referencing one of these ids
    genuinely belongs to this tenant. That should always hold by
    construction, but a malformed/cross-tenant row must never be able to
    make its way into this tenant's reachability counts, so the filter is
    applied explicitly rather than relied upon implicitly.
    """
    if not message_ids:
        return {}
    result: Dict[int, List[str]] = {}
    for row in (
        db.query(ArchiveMessageRecipient)
        .filter(
            ArchiveMessageRecipient.message_id.in_(message_ids),
            ArchiveMessageRecipient.tenant_id == tenant_id,
        )
        .all()
    ):
        result.setdefault(row.message_id, []).append(row.receiver_userid)
    return result


def _apply_conversation_candidate_filter(query, conversation_id: str, tenant_id: str):
    """
    Narrow an ArchiveMessage query to rows that belong to the *exact*
    conversation_id, using raw per-message signals (sender / recipient /
    roomid) rather than the real membership function — a message that has
    already fallen out of real membership must still be selectable here,
    otherwise the audit could never explain why it disappeared.

    Direct pairs ("direct__<uid_a>___<uid_b>") require an exact match:
      - roomid must be empty (group messages are never part of a direct
        conversation's candidate set, regardless of sender/recipient).
      - sender must be one of the two named participants.
      - no recipient row on the message may name a third party outside
        {uid_a, uid_b} — this is what keeps "staff_a -> contact_b" out of
        the "direct__staff_a___contact_a" scan, and keeps
        "staff_b -> contact_a" out of it too, without requiring a
        recipient row to exist at all (a message with zero recipient rows
        for this sender is exactly the unreachable_missing_recipient case
        this audit exists to catch, so it must not be filtered out here).
      - if a recipient row does exist, at least one of them must name the
        *opposite* participant relative to the sender — a self-recipient
        row (e.g. sender=staff_a, recipient=staff_a) names no third party
        but is still not a valid direct-pair relationship, and previously
        slipped through into conversation-membership derivation where it
        produces a single-party conversation id ("direct__staff_a") that
        _fetch_conversation_messages rejects with an HTTPException — an
        audit must never abort on malformed data like that, so it is kept
        out of the candidate set here as the first line of defense (see
        also the fault-tolerant membership lookup below).
    A malformed direct id (not exactly two named parties) matches nothing,
    rather than loosening to a sender-only match.
    """
    if conversation_id.startswith("direct__"):
        rest = conversation_id[len("direct__"):]
        parts = rest.split("___", 1)
        uids = [p for p in parts if p]
        if len(uids) != 2:
            return query.filter(false())
        uid_a, uid_b = uids

        stray_recipient_exists = (
            query.session.query(ArchiveMessageRecipient.id)
            .filter(
                ArchiveMessageRecipient.message_id == ArchiveMessage.id,
                ArchiveMessageRecipient.tenant_id == tenant_id,
                ArchiveMessageRecipient.receiver_userid.notin_([uid_a, uid_b]),
            )
            .exists()
        )

        any_recipient_exists = (
            query.session.query(ArchiveMessageRecipient.id)
            .filter(
                ArchiveMessageRecipient.message_id == ArchiveMessage.id,
                ArchiveMessageRecipient.tenant_id == tenant_id,
            )
            .exists()
        )

        # The recipient row (if any) must name the *opposite* participant
        # relative to this row's sender — a self-recipient row (sender ==
        # uid_a, recipient == uid_a) or a recipient naming the same side as
        # the sender is not a valid direct-pair relationship and must not
        # be treated as one, even though it names no third party. Messages
        # with zero recipient rows are unaffected (that is the
        # unreachable_missing_recipient case this audit exists to catch).
        has_opposite_recipient = (
            query.session.query(ArchiveMessageRecipient.id)
            .filter(
                ArchiveMessageRecipient.message_id == ArchiveMessage.id,
                ArchiveMessageRecipient.tenant_id == tenant_id,
                or_(
                    and_(
                        ArchiveMessage.sender == uid_a,
                        ArchiveMessageRecipient.receiver_userid == uid_b,
                    ),
                    and_(
                        ArchiveMessage.sender == uid_b,
                        ArchiveMessageRecipient.receiver_userid == uid_a,
                    ),
                ),
            )
            .exists()
        )

        return query.filter(
            or_(ArchiveMessage.roomid.is_(None), ArchiveMessage.roomid == ""),
            ArchiveMessage.sender.in_([uid_a, uid_b]),
            ~stray_recipient_exists,
            or_(~any_recipient_exists, has_opposite_recipient),
        )
    return query.filter(ArchiveMessage.roomid == conversation_id)
