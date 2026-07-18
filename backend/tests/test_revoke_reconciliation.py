"""
Tests for RND-201 — revoke event <-> original message reconciliation.

Scope: app.revoke_reconciliation — the single canonical place a WeCom
"revoke" event is associated with the original message it targets. These
tests exercise the module directly (not through the SDK-dependent decrypt
script), constructing ArchiveMessage rows the same way
scripts/decrypt_wecom_messages_once.py does: a msgtype="revoke" row whose
structured_content is exactly the shape
app.structured_message_parser.parse_structured_content("revoke", ...)
produces (see test_structured_message_parser.py's revoke tests for that
contract).

Reuses the shared sqlite-backed `db` fixture, `_TENANT_A`/`_TENANT_B`
constants, and `_insert_message` helper from test_reachability_audit.py —
the established cross-file fixture-import convention this test suite
already uses (see that module's docstring).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import ArchiveMessage, MediaFile, MessageRevocation
from app.revoke_reconciliation import (
    PENDING_TO_MISSING_THRESHOLD,
    display_status,
    reconcile_pending_revocations,
    reconcile_revoke_event,
)
from tests.test_reachability_audit import _SCHEMA_SQL, _TENANT_A, _TENANT_B, _insert_message, db  # noqa: F401 -- pytest fixture, must be imported to be discovered


def _revoke_structured_content(pre_msgid: "str | None") -> dict:
    """The exact structured_content shape
    app.structured_message_parser.parse_structured_content("revoke", ...)
    produces -- see test_structured_message_parser.py's revoke tests."""
    if not pre_msgid:
        return {"fields": None, "raw": {}, "parse_warnings": ["missing_pre_msgid"]}
    return {
        "fields": {"pre_msgid": pre_msgid},
        "raw": {"pre_msgid": pre_msgid},
        "parse_warnings": [],
    }


def _insert_revoke_event(
    db: Session,
    *,
    pre_msgid: "str | None",
    tenant_id: str = _TENANT_A,
    msgtime: int = 5000,
    **kwargs,
) -> ArchiveMessage:
    return _insert_message(
        db,
        msgtype="revoke",
        structured_content=_revoke_structured_content(pre_msgid),
        tenant_id=tenant_id,
        msgtime=msgtime,
        **kwargs,
    )


def _as_utc(value: "datetime | None") -> "datetime | None":
    """sqlite (used in these tests) hands back a naive datetime for a
    DateTime(timezone=True) column even though the app wrote an
    aware one -- normalize before comparing so assertions are agnostic
    to that storage-layer quirk (matches app.revoke_reconciliation's own
    defensive normalization, see its module docstring)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _get_revocation(db: Session, revoke_message_id: int) -> MessageRevocation:
    row = (
        db.query(MessageRevocation)
        .filter(MessageRevocation.revoke_event_message_id == revoke_message_id)
        .one()
    )
    return row


# ---------------------------------------------------------------------------
# 3.1 — original message already exists
# ---------------------------------------------------------------------------


def test_revoke_after_original_links_immediately_and_preserves_content(db) -> None:
    original = _insert_message(
        db,
        msgtype="text",
        content_text="hello world",
        sender="staff_a",
        roomid="room1",
        msgtime=1000,
        seq=1,
        tenant_id=_TENANT_A,
    )
    revoke_event = _insert_revoke_event(db, pre_msgid=original.msgid, msgtime=2000, seq=2)

    reconcile_revoke_event(db, revoke_event)
    db.commit()
    db.refresh(original)

    assert original.is_revoked is True
    assert _as_utc(original.revoked_at) == datetime.fromtimestamp(2000 / 1000.0, tz=timezone.utc)
    # Original content is completely untouched.
    assert original.content_text == "hello world"
    assert original.sender == "staff_a"
    assert original.roomid == "room1"
    assert original.msgtime == 1000
    assert original.seq == 1
    assert original.msgtype == "text"

    revocation = _get_revocation(db, revoke_event.id)
    assert revocation.status == "linked"
    assert revocation.original_message_id == original.id
    assert revocation.target_msgid == original.msgid


def test_revoke_does_not_touch_structured_content_or_decrypted_payload(db) -> None:
    original = _insert_message(
        db,
        msgtype="location",
        structured_content={"fields": {"address": "somewhere"}, "raw": {"x": 1}, "parse_warnings": []},
        decrypted_payload={"msgtype": "location"},
        tenant_id=_TENANT_A,
        seq=1,
    )
    revoke_event = _insert_revoke_event(db, pre_msgid=original.msgid, seq=2)

    reconcile_revoke_event(db, revoke_event)
    db.commit()
    db.refresh(original)

    assert original.is_revoked is True
    assert original.structured_content == {"fields": {"address": "somewhere"}, "raw": {"x": 1}, "parse_warnings": []}
    assert original.decrypted_payload == {"msgtype": "location"}


def test_revoke_does_not_create_a_second_timeline_worthy_row_for_the_original() -> None:
    """No new ArchiveMessage row is ever created by reconciliation --
    only the existing original row's is_revoked/revoked_at are set. This
    is implicit in every other test here (no test ever asserts a new
    ArchiveMessage was inserted) but is called out explicitly since it's
    a hard ticket requirement."""


# ---------------------------------------------------------------------------
# 3.2 — revoke arrives before the original (same session AND cross-session)
# ---------------------------------------------------------------------------


def test_revoke_before_original_persists_pending_association(db) -> None:
    revoke_event = _insert_revoke_event(db, pre_msgid="not-archived-yet", seq=1)

    revocation = reconcile_revoke_event(db, revoke_event)
    db.commit()

    assert revocation.status == "pending"
    assert revocation.target_msgid == "not-archived-yet"
    assert revocation.original_message_id is None


def test_original_arriving_later_in_same_session_is_linked_by_repair_scan(db) -> None:
    revoke_event = _insert_revoke_event(db, pre_msgid="msg-arrives-later", seq=1)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    # The original arrives in a LATER decrypt sweep of the same worker
    # run (or a later run) -- simulated here as a second insert.
    original = _insert_message(
        db, msgtype="text", content_text="I was late", msgid="msg-arrives-later", seq=2, tenant_id=_TENANT_A
    )

    linked_count = reconcile_pending_revocations(db, _TENANT_A)
    db.commit()
    db.refresh(original)

    assert linked_count == 1
    assert original.is_revoked is True
    assert original.content_text == "I was late"
    revocation = _get_revocation(db, revoke_event.id)
    assert revocation.status == "linked"
    assert revocation.original_message_id == original.id


def test_cross_session_reconciliation_uses_persisted_state_not_memory(tmp_path) -> None:
    """Revoke and original processed in separate DB sessions (simulating
    separate worker executions / transactions) against the SAME
    database -- reconciliation must depend only on persisted state."""
    db_path = tmp_path / "cross_session.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))

    # Session/transaction #1: revoke event arrives and is processed alone.
    session1 = Session(engine)
    revoke_event = _insert_message(
        session1,
        msgtype="revoke",
        structured_content=_revoke_structured_content("cross-session-msgid"),
        tenant_id=_TENANT_A,
        seq=1,
    )
    reconcile_revoke_event(session1, revoke_event)
    session1.commit()
    session1.close()

    # Session/transaction #2 (a fresh worker run): the original arrives.
    session2 = Session(engine)
    original = _insert_message(
        session2,
        msgtype="text",
        content_text="arrived in run 2",
        msgid="cross-session-msgid",
        seq=2,
        tenant_id=_TENANT_A,
    )
    session2.commit()
    original_id = original.id
    session2.close()

    # Session/transaction #3 (yet another worker run): repair scan runs.
    session3 = Session(engine)
    linked_count = reconcile_pending_revocations(session3, _TENANT_A)
    session3.commit()

    refreshed_original = (
        session3.query(ArchiveMessage).filter(ArchiveMessage.id == original_id).one()
    )
    assert linked_count == 1
    assert refreshed_original.is_revoked is True
    assert refreshed_original.content_text == "arrived in run 2"
    session3.close()


# ---------------------------------------------------------------------------
# 3.3 — original message never becomes available
# ---------------------------------------------------------------------------


def test_original_never_arriving_stays_pending_forever_no_fabrication(db) -> None:
    revoke_event = _insert_revoke_event(db, pre_msgid="ghost-msgid", seq=1)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    # Repeated repair scans (simulating many future worker runs) never
    # fabricate a target -- the association simply stays pending forever.
    for _ in range(3):
        linked = reconcile_pending_revocations(db, _TENANT_A)
        assert linked == 0

    revocation = _get_revocation(db, revoke_event.id)
    assert revocation.status == "pending"
    assert revocation.original_message_id is None
    assert revocation.target_msgid == "ghost-msgid"


def test_display_status_ages_pending_into_original_missing(db) -> None:
    revoke_event = _insert_revoke_event(db, pre_msgid="ghost-msgid-2", seq=1)
    revocation = reconcile_revoke_event(db, revoke_event)
    db.commit()

    fresh_now = revocation.created_at.replace(tzinfo=timezone.utc) if revocation.created_at.tzinfo is None else revocation.created_at
    assert display_status(revocation, now=fresh_now) == "pending"

    aged_now = fresh_now + PENDING_TO_MISSING_THRESHOLD + timedelta(minutes=1)
    assert display_status(revocation, now=aged_now) == "original_missing"

    # The persisted status is untouched by display_status -- still eligible
    # for reconciliation regardless of how "old" it displays as.
    assert revocation.status == "pending"


# ---------------------------------------------------------------------------
# 3.4 — duplicate / equivalent revoke events
# ---------------------------------------------------------------------------


def test_processing_the_same_revoke_row_twice_is_idempotent(db) -> None:
    original = _insert_message(db, msgtype="text", content_text="hi", seq=1, tenant_id=_TENANT_A)
    revoke_event = _insert_revoke_event(db, pre_msgid=original.msgid, seq=2)

    reconcile_revoke_event(db, revoke_event)
    db.commit()
    reconcile_revoke_event(db, revoke_event)  # duplicate processing
    db.commit()

    rows = (
        db.query(MessageRevocation)
        .filter(MessageRevocation.revoke_event_message_id == revoke_event.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "linked"

    db.refresh(original)
    assert original.content_text == "hi"
    assert original.is_revoked is True


def test_duplicate_revoke_events_for_same_original_both_link_earliest_wins(db) -> None:
    original = _insert_message(db, msgtype="text", content_text="hi", seq=1, tenant_id=_TENANT_A)
    later_revoke = _insert_revoke_event(db, pre_msgid=original.msgid, msgtime=9000, seq=2)
    earlier_revoke = _insert_revoke_event(db, pre_msgid=original.msgid, msgtime=3000, seq=3)

    # Later revoke event processed first (arbitrary processing order).
    reconcile_revoke_event(db, later_revoke)
    db.commit()
    reconcile_revoke_event(db, earlier_revoke)
    db.commit()
    db.refresh(original)

    # Both are linked -- neither left dangling in "pending".
    later_revocation = _get_revocation(db, later_revoke.id)
    earlier_revocation = _get_revocation(db, earlier_revoke.id)
    assert later_revocation.status == "linked"
    assert earlier_revocation.status == "linked"
    assert later_revocation.original_message_id == original.id
    assert earlier_revocation.original_message_id == original.id

    # revoked_at reflects the EARLIEST revoke event's msgtime, not the
    # first one processed or the most recent one.
    assert _as_utc(original.revoked_at) == datetime.fromtimestamp(3000 / 1000.0, tz=timezone.utc)
    assert original.is_revoked is True


def test_a_later_arriving_earlier_revoke_event_moves_revoked_at_earlier_never_later(db) -> None:
    original = _insert_message(db, msgtype="text", content_text="hi", seq=1, tenant_id=_TENANT_A)
    first_processed = _insert_revoke_event(db, pre_msgid=original.msgid, msgtime=5000, seq=2)
    reconcile_revoke_event(db, first_processed)
    db.commit()
    db.refresh(original)
    assert _as_utc(original.revoked_at) == datetime.fromtimestamp(5000 / 1000.0, tz=timezone.utc)

    # A genuinely earlier revoke event surfaces later (e.g. arrived via a
    # slow/retried sync batch) -- revoked_at moves earlier to match.
    earlier_arriving = _insert_revoke_event(db, pre_msgid=original.msgid, msgtime=1000, seq=3)
    reconcile_revoke_event(db, earlier_arriving)
    db.commit()
    db.refresh(original)
    assert _as_utc(original.revoked_at) == datetime.fromtimestamp(1000 / 1000.0, tz=timezone.utc)

    # A THIRD, later-timed revoke event must never move it back later.
    later_arriving = _insert_revoke_event(db, pre_msgid=original.msgid, msgtime=8000, seq=4)
    reconcile_revoke_event(db, later_arriving)
    db.commit()
    db.refresh(original)
    assert _as_utc(original.revoked_at) == datetime.fromtimestamp(1000 / 1000.0, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Malformed revoke events
# ---------------------------------------------------------------------------


def test_malformed_revoke_missing_pre_msgid_is_preserved_not_dropped(db) -> None:
    revoke_event = _insert_revoke_event(db, pre_msgid=None, seq=1)

    revocation = reconcile_revoke_event(db, revoke_event)
    db.commit()

    assert revocation.status == "malformed"
    assert revocation.target_msgid is None
    assert revocation.original_message_id is None
    assert display_status(revocation) == "malformed"


def test_malformed_revoke_is_never_retried_by_the_repair_scan(db) -> None:
    revoke_event = _insert_revoke_event(db, pre_msgid=None, seq=1)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    linked = reconcile_pending_revocations(db, _TENANT_A)
    assert linked == 0
    revocation = _get_revocation(db, revoke_event.id)
    assert revocation.status == "malformed"


def test_reconcile_revoke_event_is_a_noop_for_non_revoke_rows(db) -> None:
    ordinary = _insert_message(db, msgtype="text", content_text="just text", seq=1, tenant_id=_TENANT_A)
    result = reconcile_revoke_event(db, ordinary)
    assert result is None
    assert db.query(MessageRevocation).count() == 0


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_revoke_never_crosses_tenant_boundary_on_colliding_msgid(db) -> None:
    shared_msgid = "collides-across-tenants"
    original_a = _insert_message(
        db, msgid=shared_msgid, msgtype="text", content_text="tenant a content", tenant_id=_TENANT_A, seq=1
    )
    original_b = _insert_message(
        db, msgid=shared_msgid, msgtype="text", content_text="tenant b content", tenant_id=_TENANT_B, seq=1
    )

    revoke_event = _insert_revoke_event(db, pre_msgid=shared_msgid, tenant_id=_TENANT_A, seq=2)
    reconcile_revoke_event(db, revoke_event)
    db.commit()
    db.refresh(original_a)
    db.refresh(original_b)

    assert original_a.is_revoked is True
    assert original_b.is_revoked is False
    assert original_b.content_text == "tenant b content"

    revocation = _get_revocation(db, revoke_event.id)
    assert revocation.original_message_id == original_a.id


def test_pending_repair_scan_respects_tenant_scope(db) -> None:
    revoke_a = _insert_revoke_event(db, pre_msgid="shared-target", tenant_id=_TENANT_A, seq=1)
    revoke_b = _insert_revoke_event(db, pre_msgid="shared-target", tenant_id=_TENANT_B, seq=2)
    reconcile_revoke_event(db, revoke_a)
    reconcile_revoke_event(db, revoke_b)
    db.commit()

    # Only tenant A's original arrives.
    original_a = _insert_message(
        db, msgid="shared-target", msgtype="text", content_text="a", tenant_id=_TENANT_A, seq=3
    )
    db.commit()

    linked = reconcile_pending_revocations(db, _TENANT_A)
    assert linked == 1
    db.refresh(original_a)
    assert original_a.is_revoked is True

    revocation_b = _get_revocation(db, revoke_b.id)
    assert revocation_b.status == "pending"


# ---------------------------------------------------------------------------
# Media preservation
# ---------------------------------------------------------------------------


def test_revoke_never_touches_media_files_row(db) -> None:
    original = _insert_message(db, msgtype="image", sdkfileid="sdk-123", seq=1, tenant_id=_TENANT_A)
    media = MediaFile(
        sdkfileid="sdk-123",
        archive_message_id=original.id,
        tenant_id=_TENANT_A,
        storage_backend="qiniu_kodo",
        storage_ref="tenant-a/sdk-123.jpg",
        file_size=12345,
        download_status="downloaded",
        mime_type="image/jpeg",
        checksum_sha256="deadbeef",
    )
    db.add(media)
    db.commit()
    db.refresh(media)

    revoke_event = _insert_revoke_event(db, pre_msgid=original.msgid, seq=2)
    reconcile_revoke_event(db, revoke_event)
    db.commit()
    db.refresh(original)
    db.refresh(media)

    assert original.is_revoked is True
    # Every media field is byte-for-byte unchanged.
    assert media.archive_message_id == original.id
    assert media.storage_backend == "qiniu_kodo"
    assert media.storage_ref == "tenant-a/sdk-123.jpg"
    assert media.file_size == 12345
    assert media.download_status == "downloaded"
    assert media.mime_type == "image/jpeg"
    assert media.checksum_sha256 == "deadbeef"
