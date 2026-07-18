"""Revoke event <-> original message reconciliation (RND-201).

The single, canonical place that associates a WeCom "revoke" (撤回) event
-- itself an archive_messages row, msgtype="revoke" -- with the original
message it targets (archive_messages.msgid == revoke.pre_msgid,
tenant-scoped; see app.structured_message_parser's revoke branch for
where pre_msgid is extracted, and its module docstring for the confirmed
WeCom payload shape). Called from exactly one place in the ingest
pipeline -- scripts/decrypt_wecom_messages_once.py, right after each
row's structured_content is set -- so there is only one matching
implementation in the codebase (no duplicated matching rules across the
parser, sync worker, API serializer, route handler, or frontend).

Two entry points cover both arrival orders, and both are driven purely by
persisted database state -- never in-memory processing order -- so they
work correctly regardless of how many worker runs or decrypt sweeps
separate a revoke event from the message it targets:

  reconcile_revoke_event(session, revoke_message)
      Called once, immediately after a msgtype="revoke" row finishes
      decrypting. Upserts a MessageRevocation row and links it to the
      original message immediately if that row already exists in this
      tenant ("original message already exists" case).

  reconcile_pending_revocations(session, tenant_id=None)
      A repair scan, safe to call on every worker invocation (mirrors
      app.media_download's candidate-query pattern and this same
      script's repair_missing_recipients()): finds every still-"pending"
      MessageRevocation row and re-checks whether its target message now
      exists, linking it if so. This is what makes "revoke arrives before
      the original" resolve correctly later, in this sweep or any future
      one ("revoke arrives before the original" / cross-batch cases).

Neither function ever reads or writes content_text, structured_content,
decrypted_payload, raw_encrypted_payload, msgtype, sender, roomid,
msgtime, tolist, sdkfileid, or any media_files row on the original
message -- the only columns ever written on the original are is_revoked
and revoked_at (never cleared once set; see _apply_revocation_to_original).

Idempotency: both functions are safe to call repeatedly for the same
event. reconcile_revoke_event() SELECTs the existing MessageRevocation
row for (tenant_id, revoke_event_message_id) before deciding whether to
insert, backed by the uq_message_revocations_revoke_event_message_id
UniqueConstraint (migration 0011, RND-201 round 3 / B4 QA fix -- a PLAIN
unique constraint on revoke_event_message_id alone, replacing the
composite (tenant_id, revoke_event_message_id) constraint migration
0009 originally shipped, since a composite constraint alone still let a
malformed/buggy write claim the same revoke event under a second,
different tenant_id) as a database-level backstop -- the same
SELECT-then-write convention used throughout this codebase (see
scripts/sync_wecom_archive_once.py, app.media_download.
get_or_reset_media_file). Linking an already-linked revocation, or
re-running the repair scan when nothing new has arrived, is a no-op.
This module's own code did not need to change for the 0011 tightening
-- it already always derives revoke_message.tenant_id from the real
ArchiveMessage row being processed, so it never generates a
cross-tenant value for this column in the first place; 0011 only closes
that door for writers outside this module (or a future bug within it).

Duplicate/equivalent revoke events (ticket 3.4): if two distinct revoke
events (different revoke_event_message_id) ever target the same original
message, both get their own MessageRevocation row and both end up
"linked" -- neither is left dangling -- but only one revoked_at value
ever lands on the original, and it is always the earliest of the two
revoke events' own msgtime (see _apply_revocation_to_original). This
also means the original's is_revoked flag and revoked_at can never be
"un-set" or moved backwards to a less-revoked state by later processing.

Concurrency (RND-201 round 2 QA fix): production runs a single worker at
a time behind a flock, but this module is also called directly by the
backfill script and could plausibly run from more than one process (an
operator running a manual backfill while the timer-driven worker is also
mid-sweep). Two guarantees hold under that kind of overlap:

  - Creating the MessageRevocation row is conflict-safe. The INSERT runs
    inside a SAVEPOINT (Session.begin_nested()); if a concurrent process
    already committed the same (tenant_id, revoke_event_message_id) row
    first, the UniqueConstraint violation raises IntegrityError, which is
    caught and handled by rolling back only that SAVEPOINT (not the
    whole session/transaction) and re-reading the row the other process
    created. A worker must never end a sweep with PendingRollbackError --
    letting an IntegrityError propagate to the session's outer
    transaction is exactly what causes that.
  - Updating the original's is_revoked/revoked_at, and flipping a
    revocation from "pending" to "linked", are both done as a single
    conditional UPDATE ... WHERE statement evaluated by the database,
    not a Python-side read-compare-write. This is what actually makes
    "earliest revoke_event_msgtime wins" race-free: two concurrent
    processes racing to link the same original can each safely issue
    their own UPDATE, and the database's row lock (not application code)
    decides which one's WHERE clause still matches by the time it runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, MessageRevocation

# The complete, authoritative set of values MessageRevocation.status is
# ever persisted as (RND-201 round 4 QA fix). Mirrors migration 0010's
# ck_message_revocations_status_valid CHECK constraint and every literal
# status string this module assigns above -- the single source of truth
# other code must import rather than redefining independently (see
# scripts/check_message_revocations_integrity.py, which does exactly
# that instead of hardcoding a second, driftable copy of this list).
#
# "original_missing" / "missing" is NOT in this set -- it is a
# request-time DISPLAY label display_status() below derives from an aged
# "pending" row; it is never written to the status column itself.
PERSISTED_STATUSES = frozenset({"pending", "linked", "malformed"})


def reconcile_revoke_event(
    session: Session, revoke_message: ArchiveMessage
) -> Optional[MessageRevocation]:
    """Process one just-decrypted msgtype="revoke" ArchiveMessage row.

    Must be called after revoke_message.structured_content has been set
    by app.structured_message_parser.parse_structured_content() for this
    row. Returns the MessageRevocation row (new or pre-existing), or None
    if this row is not actually a revoke event -- a no-op guard so
    callers may pass every freshly-decrypted row through uniformly
    without a msgtype check of their own.
    """
    if revoke_message.msgtype != "revoke":
        return None

    existing = _select_existing_revocation(
        session, revoke_message.tenant_id, revoke_message.id
    )
    if existing is not None:
        # Idempotent replay (e.g. a backfill re-scanning already-decrypted
        # revoke rows). A still-pending row gets one more link attempt in
        # case the original has arrived since; linked/malformed rows are
        # terminal and are left completely untouched.
        if existing.status == "pending":
            _try_link(session, existing)
        return existing

    target_msgid = _extract_target_msgid(revoke_message)
    revocation = MessageRevocation(
        tenant_id=revoke_message.tenant_id,
        revoke_event_message_id=revoke_message.id,
        revoke_event_msgid=revoke_message.msgid,
        revoke_event_msgtime=revoke_message.msgtime,
        target_msgid=target_msgid,
        status="pending" if target_msgid else "malformed",
    )
    try:
        with session.begin_nested():
            session.add(revocation)
            session.flush()
    except IntegrityError:
        # A concurrent process (another worker run, or a manual backfill
        # overlapping with the live decrypt pipeline) already committed
        # this exact (tenant_id, revoke_event_message_id) row between our
        # existence check above and this insert. Only the SAVEPOINT for
        # this one insert is rolled back -- the session and its outer
        # transaction remain fully usable for the rest of this sweep, so
        # this can never surface as PendingRollbackError to the caller.
        if revocation in session:
            session.expunge(revocation)
        existing = _select_existing_revocation(
            session, revoke_message.tenant_id, revoke_message.id
        )
        if existing is None:
            # Vanishingly unlikely (the row would have to be deleted
            # between the failed insert and this re-read) -- degrade to
            # "nothing to report" rather than raising, matching this
            # module's never-raise contract.
            return None
        if existing.status == "pending":
            _try_link(session, existing)
        return existing

    if target_msgid:
        _try_link(session, revocation)

    return revocation


def _select_existing_revocation(
    session: Session, tenant_id: Optional[str], revoke_event_message_id: int
) -> Optional[MessageRevocation]:
    return (
        session.query(MessageRevocation)
        .filter(
            MessageRevocation.tenant_id == tenant_id,
            MessageRevocation.revoke_event_message_id == revoke_event_message_id,
        )
        .first()
    )


def reconcile_pending_revocations(
    session: Session, tenant_id: Optional[str] = None
) -> int:
    """Repair scan: re-check every still-"pending" MessageRevocation row
    against current archive_messages state, linking any whose target has
    since arrived (in this sweep or an earlier one). Safe and cheap to
    call on every worker invocation -- a row already "linked" or
    "malformed" never matches this query again, so the candidate set is
    always just the small backlog of genuinely unresolved revokes.

    Returns the number of revocations newly linked by this call.
    """
    query = session.query(MessageRevocation).filter(
        MessageRevocation.status == "pending"
    )
    if tenant_id is not None:
        query = query.filter(MessageRevocation.tenant_id == tenant_id)

    linked = 0
    for revocation in query.order_by(MessageRevocation.id).all():
        if _try_link(session, revocation):
            linked += 1
    return linked


def _extract_target_msgid(revoke_message: ArchiveMessage) -> Optional[str]:
    content = revoke_message.structured_content
    if not isinstance(content, dict):
        return None
    fields = content.get("fields")
    if not isinstance(fields, dict):
        return None
    pre_msgid = fields.get("pre_msgid")
    return pre_msgid if isinstance(pre_msgid, str) and pre_msgid else None


def _try_link(session: Session, revocation: MessageRevocation) -> bool:
    """Attempt to link a pending revocation to its original message.

    Returns True iff THIS call performed the link. Tenant isolation is
    enforced here, not merely inherited from the caller: the lookup
    always filters by revocation.tenant_id explicitly, so a message with
    a colliding msgid in a different tenant can never be matched -- see
    module docstring / RND-201 tenant isolation requirement.

    Concurrency: both writes below (the original's is_revoked/revoked_at,
    and this revocation's own pending -> linked transition) are single
    conditional UPDATE statements whose WHERE clause is re-evaluated by
    the database at write time, not a Python-side read-modify-write --
    see module docstring's Concurrency section. A concurrent caller
    linking the same revocation, or racing to set revoked_at on the same
    original from a different (duplicate) revoke event, can never lose
    an update or leave revoked_at in an inconsistent state.
    """
    if revocation.status != "pending" or not revocation.target_msgid:
        return False

    original = (
        session.query(ArchiveMessage)
        .filter(
            ArchiveMessage.tenant_id == revocation.tenant_id,
            ArchiveMessage.msgid == revocation.target_msgid,
        )
        .first()
    )
    if original is None:
        return False

    _apply_revocation_to_original(session, original.id, revocation.revoke_event_msgtime)

    # Atomically claim this revocation for linking, guarded by status=
    # "pending" in the WHERE clause -- if a concurrent process (e.g. two
    # overlapping repair-scan runs) already flipped it to "linked" between
    # our check above and this statement, rowcount is 0 and we correctly
    # report False rather than double-counting or clobbering the other
    # writer's result.
    # synchronize_session=False: this is a fire-and-forget conditional
    # write -- no caller reads revocation's Python attributes before a
    # fresh reload, so there is no need for SQLAlchemy to try to keep the
    # in-memory ORM object in sync (its default "evaluate" strategy would
    # otherwise re-run the WHERE clause in Python against already-loaded
    # objects, which is both unnecessary work and, for a datetime column,
    # prone to the same naive/aware comparison hazard this function is
    # designed to avoid at the database level).
    result = session.execute(
        update(MessageRevocation)
        .where(
            MessageRevocation.id == revocation.id,
            MessageRevocation.status == "pending",
        )
        .values(original_message_id=original.id, status="linked")
        .execution_options(synchronize_session=False)
    )
    session.flush()
    if result.rowcount:
        revocation.original_message_id = original.id
        revocation.status = "linked"
        return True
    return False


def _apply_revocation_to_original(
    session: Session, original_id: int, revoke_event_msgtime: Optional[int]
) -> None:
    """The ONLY mutation this module ever performs on an original
    message -- is_revoked/revoked_at, nothing else. content_text,
    structured_content, decrypted_payload, raw_encrypted_payload,
    msgtype, sender, roomid, msgtime, tolist, sdkfileid, and every
    media_files row belonging to this message are never read or written
    here, directly or indirectly.

    revoked_at is derived from the revoke event's own msgtime (when
    WeCom says the revoke happened), not wall-clock processing time, so
    it stays correct across delayed/out-of-order/replayed sync runs.

    Earliest-wins tie-break for duplicate/equivalent revoke events
    (ticket 3.4), made race-free (RND-201 round 2 QA fix): this is a
    single conditional UPDATE ... WHERE statement, not a Python read of
    original.revoked_at followed by a separate write. The WHERE clause
    (revoked_at IS NULL OR revoked_at > candidate) is evaluated by the
    database against the current row at write time, so two processes
    concurrently linking two different revoke events to the same
    original can never produce a lost update or an out-of-order result --
    whichever transaction's candidate is genuinely earliest always wins,
    regardless of commit order.
    """
    # synchronize_session=False throughout -- see _try_link's comment on
    # the identical rationale (no in-memory ArchiveMessage attribute is
    # read after these writes within this module; callers that need a
    # fresh value re-query/refresh explicitly).
    candidate = _msgtime_to_datetime(revoke_event_msgtime)
    if candidate is None:
        session.execute(
            update(ArchiveMessage)
            .where(ArchiveMessage.id == original_id)
            .values(is_revoked=True)
            .execution_options(synchronize_session=False)
        )
        return
    session.execute(
        update(ArchiveMessage)
        .where(
            ArchiveMessage.id == original_id,
            or_(
                ArchiveMessage.revoked_at.is_(None),
                ArchiveMessage.revoked_at > candidate,
            ),
        )
        .values(is_revoked=True, revoked_at=candidate)
        .execution_options(synchronize_session=False)
    )
    # No further statement is needed when the WHERE above doesn't match:
    # that only happens when revoked_at already holds an earlier-or-equal
    # value, which (by this same invariant, maintained on every write
    # path in this function) can only be true if is_revoked was already
    # set True by that earlier write.


def _msgtime_to_datetime(msgtime_ms: Optional[int]) -> Optional[datetime]:
    if msgtime_ms is None:
        return None
    try:
        return datetime.fromtimestamp(msgtime_ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Display-only status derivation, shared by the timeline API
# (app.routers.conversations) and the backfill script
# (scripts/backfill_revoke_associations_once.py) -- kept here, not
# duplicated in either caller, so there is exactly one definition of "how
# old is too old to still call this pending" (ticket requirement: no
# duplicated matching/status rules across call sites).
# ---------------------------------------------------------------------------

# How long a revoke event may sit in status="pending" (its target message
# has not been archived yet) before callers should report it as
# "original_missing" instead of "pending" -- a purely presentational
# distinction. The persisted MessageRevocation.status stays "pending"
# regardless -- reconcile_pending_revocations() keeps retrying it forever,
# so a message that finally arrives after this window still gets linked
# correctly on the next decrypt sweep.
PENDING_TO_MISSING_THRESHOLD = timedelta(hours=24)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def display_status(revocation: MessageRevocation, now: Optional[datetime] = None) -> str:
    """Derive the caller-facing status string for a MessageRevocation row:
    "linked"/"malformed" are returned verbatim; "pending" additionally
    ages into "original_missing" once PENDING_TO_MISSING_THRESHOLD has
    elapsed since the revoke event was first recorded. `now` is
    injectable for tests; defaults to the real current time."""
    if revocation.status != "pending":
        return revocation.status
    now = now if now is not None else datetime.now(timezone.utc)
    age = now - _ensure_aware(revocation.created_at)
    if age > PENDING_TO_MISSING_THRESHOLD:
        return "original_missing"
    return "pending"
