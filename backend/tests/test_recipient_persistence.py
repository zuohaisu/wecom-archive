"""
Tests for RND-179 — Recipient Persistence Recovery.

RND-178's Message Reachability Audit (app/reachability_audit.py) found that
a recipient-upsert failure inside scripts/decrypt_wecom_messages_once.py is
intentionally non-fatal to decrypt_status — but the script only ever
reprocesses archive_messages rows with decrypt_status in ("pending",
"failed"), so a message that hit exactly that gap (decrypt succeeded,
recipient rows never got written) was permanently classified
unreachable_missing_recipient with no retry path, even though the data
needed to recover it (the tolist column, already populated on the message
row) was sitting right there.

Scope:
  - _upsert_recipients() no longer inserts a duplicate row when tolist
    itself contains a repeated userid.
  - build_missing_recipient_repair_query() / repair_missing_recipients()
    (the new self-healing recovery scan) find and repair exactly the
    messages the audit calls out, and nothing else.
  - End-to-end: the Reachability Audit (RND-178, unmodified) reports the
    improvement before/after running the repair, on the same database —
    this is the audit verifying the fix, not a parallel check.

Reuses the sqlite-backed schema/fixtures from test_reachability_audit.py
(same technique test_download_wecom_image_media_once.py uses to reuse
test_staff_seats._msg) so this exercises the real ORM models and the real
audit rather than a hand-rolled substitute.

Run (from backend/):
    pytest tests/test_recipient_persistence.py -v
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import ArchiveMessageRecipient
from scripts.decrypt_wecom_messages_once import (
    _upsert_recipients,
    build_missing_recipient_repair_query,
    repair_missing_recipients,
)
from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    _insert_recipient,
    db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)


def _recipients_for(db: Session, message_id: int, tenant_id: str = _TENANT_A) -> list[str]:
    """Tenant-scoped, matching how the app itself always reads recipients
    (see _load_recipient_userids_map in app/reachability_audit.py) — a
    plain message_id filter would also surface stray cross-tenant rows
    that must never be attributed to this tenant's message."""
    return sorted(
        r.receiver_userid
        for r in db.query(ArchiveMessageRecipient)
        .filter(
            ArchiveMessageRecipient.message_id == message_id,
            ArchiveMessageRecipient.tenant_id == tenant_id,
        )
        .all()
    )


# ---------------------------------------------------------------------------
# Part A — _upsert_recipients(): duplicate-within-tolist fix
# ---------------------------------------------------------------------------


def test_upsert_recipients_dedupes_repeated_userid_within_tolist(db) -> None:
    """A tolist containing the same receiver twice must only persist one row."""
    msg = _insert_message(db, msgtype="text", sender="staff_a", msgtime=100)

    _upsert_recipients(db, msg.id, ["contact_a", "contact_a"], msg.tenant_id)
    db.commit()

    assert _recipients_for(db, msg.id) == ["contact_a"]


def test_upsert_recipients_dedupes_against_already_persisted_rows(db) -> None:
    """Calling _upsert_recipients again for a userid already on the message is a no-op."""
    msg = _insert_message(db, msgtype="text", sender="staff_a", msgtime=101)
    _insert_recipient(db, msg.id, "contact_a")

    _upsert_recipients(db, msg.id, ["contact_a", "contact_b"], msg.tenant_id)
    db.commit()

    assert _recipients_for(db, msg.id) == ["contact_a", "contact_b"]


def test_upsert_recipients_skips_falsy_entries(db) -> None:
    msg = _insert_message(db, msgtype="text", sender="staff_a", msgtime=102)

    _upsert_recipients(db, msg.id, ["contact_a", "", None], msg.tenant_id)
    db.commit()

    assert _recipients_for(db, msg.id) == ["contact_a"]


# ---------------------------------------------------------------------------
# Part B — build_missing_recipient_repair_query(): candidate selection
# ---------------------------------------------------------------------------


def test_repair_query_matches_success_message_with_tolist_and_no_recipients(db) -> None:
    msg = _insert_message(
        db, msgtype="text", sender="staff_a", tolist=["contact_a"], msgtime=200
    )

    candidates = build_missing_recipient_repair_query(db, _TENANT_A).all()

    assert [c.id for c in candidates] == [msg.id]


def test_repair_query_excludes_message_that_already_has_recipients(db) -> None:
    msg = _insert_message(
        db, msgtype="text", sender="staff_a", tolist=["contact_a"], msgtime=201
    )
    _insert_recipient(db, msg.id, "contact_a")

    candidates = build_missing_recipient_repair_query(db, _TENANT_A).all()

    assert candidates == []


def test_repair_skips_message_with_no_tolist(db) -> None:
    """
    build_missing_recipient_repair_query() is a coarse candidate query (see
    its docstring: SQLAlchemy binds Python None as JSON `null`, not SQL
    NULL, so it cannot reliably exclude an empty tolist at the SQL layer).
    repair_missing_recipients() makes the precise per-row check instead —
    this message must not be repaired and must not gain any recipient rows.
    """
    msg = _insert_message(db, msgtype="text", sender="staff_a", tolist=None, msgtime=202)

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 0
    assert _recipients_for(db, msg.id) == []


def test_repair_query_excludes_non_success_decrypt_status(db) -> None:
    _insert_message(
        db,
        msgtype="text",
        sender="staff_a",
        tolist=["contact_a"],
        decrypt_status="failed",
        msgtime=203,
    )

    candidates = build_missing_recipient_repair_query(db, _TENANT_A).all()

    assert candidates == []


def test_repair_query_respects_tenant_scope(db) -> None:
    _insert_message(
        db, msgtype="text", sender="staff_b", tolist=["contact_b"],
        tenant_id=_TENANT_B, msgtime=204,
    )

    candidates = build_missing_recipient_repair_query(db, _TENANT_A).all()

    assert candidates == []
    assert build_missing_recipient_repair_query(db, _TENANT_B).all() != []


# ---------------------------------------------------------------------------
# Part C — repair_missing_recipients(): end-to-end recovery
# ---------------------------------------------------------------------------


def test_repair_persists_recipients_from_stored_tolist(db) -> None:
    msg = _insert_message(
        db, msgtype="text", sender="staff_a", tolist=["contact_a"], msgtime=300
    )

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 1
    assert _recipients_for(db, msg.id) == ["contact_a"]


def test_repair_is_idempotent_across_repeated_calls(db) -> None:
    msg = _insert_message(
        db, msgtype="text", sender="staff_a", tolist=["contact_a"], msgtime=301
    )

    first = repair_missing_recipients(db, _TENANT_A)
    db.commit()
    second = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert first == 1
    assert second == 0
    assert _recipients_for(db, msg.id) == ["contact_a"]


def test_repair_applies_uniformly_to_voice_messages(db) -> None:
    """No message-type-specific handling — voice messages recover exactly
    like text messages (RND-179 scope: reachability, not media playback)."""
    msg = _insert_message(
        db, msgtype="voice", sender="contact_wangwu", sdkfileid="sdk1",
        tolist=["staff_yingzi"], msgtime=302,
    )

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 1
    assert _recipients_for(db, msg.id) == ["staff_yingzi"]


def test_repair_leaves_group_message_reachable_and_adds_recipients(db) -> None:
    """Group messages are already reachable without recipient rows — the
    repair scan still backfills them for data completeness, uniformly."""
    msg = _insert_message(
        db, msgtype="text", sender="staff_a", roomid="room_1",
        tolist=["staff_b", "contact_a"], msgtime=303,
    )

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 1
    assert _recipients_for(db, msg.id) == ["contact_a", "staff_b"]


def test_repair_does_not_touch_other_tenants_messages(db) -> None:
    other = _insert_message(
        db, msgtype="text", sender="staff_b", tolist=["contact_b"],
        tenant_id=_TENANT_B, msgtime=304,
    )

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 0
    assert _recipients_for(db, other.id) == []


def test_repair_inserts_tenant_a_row_when_cross_tenant_row_has_same_receiver_userid(db) -> None:
    """
    QA regression (RND-179): the candidate query was fixed to be
    tenant-scoped, but _upsert_recipients()'s own "already there" dedup
    check was still a bare message_id match. When the cross-tenant stray
    row happens to name the *same* receiver_userid as the real tenant-A
    recipient ("contact_a" in both), that bug made _upsert_recipients()
    believe "contact_a" was already persisted for this message and skip
    inserting the real tenant-A row entirely — repair silently did nothing
    (0 rows inserted) even though the candidate query correctly selected
    the message. This is the exact failure case QA reported; it must now
    repair correctly and must never touch the tenant-B row.
    """
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(
        db, msgtype="text", sender="staff_a", tolist=["contact_a"], msgtime=310
    )
    # Malformed tenant-B row: same message_id, SAME receiver_userid, wrong tenant.
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_B)

    before = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert before["samples"][0]["reachability_status"] == "unreachable_missing_recipient"

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 1
    assert _recipients_for(db, msg.id, tenant_id=_TENANT_A) == ["contact_a"]
    # Tenant-B's row must be untouched — still exactly the one malformed row.
    assert _recipients_for(db, msg.id, tenant_id=_TENANT_B) == ["contact_a"]
    all_rows = (
        db.query(ArchiveMessageRecipient)
        .filter(ArchiveMessageRecipient.message_id == msg.id)
        .all()
    )
    assert len(all_rows) == 2  # one per tenant, no duplicate, no row lost

    after = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert after["samples"][0]["reachability_status"] == "reachable_direct"
    assert after["counts_by_status"]["unreachable_missing_recipient"] == 0


def test_repair_ignores_cross_tenant_recipient_row_when_detecting_candidates(db) -> None:
    """
    QA fix (RND-179): "already has recipients" must be tenant-scoped, not a
    bare message_id match. A malformed/corrupt row that names the right
    message_id but the wrong tenant_id must not block repair of the real
    tenant-A message — this mirrors how the RND-178 audit's own
    _load_recipient_userids_map already treats such a row as not belonging
    to the message for reachability purposes (see
    test_reachability_audit.test_cross_tenant_recipient_row_is_ignored).
    """
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(
        db, msgtype="text", sender="staff_a", tolist=["contact_a"], msgtime=305
    )
    # Only "existing" recipient row for this message belongs to tenant B.
    _insert_recipient(db, msg.id, "cross_tenant_stray", tenant_id=_TENANT_B)

    before = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert before["samples"][0]["reachability_status"] == "unreachable_missing_recipient"

    candidates = build_missing_recipient_repair_query(db, _TENANT_A).all()
    assert [c.id for c in candidates] == [msg.id]

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 1
    assert _recipients_for(db, msg.id) == ["contact_a"]

    after = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert after["samples"][0]["reachability_status"] == "reachable_direct"
    assert after["counts_by_status"]["unreachable_missing_recipient"] == 0


# ---------------------------------------------------------------------------
# Part D — Reachability Audit before/after: the repair scan actually moves
# the RND-178 audit's numbers, without any change to the audit itself.
# ---------------------------------------------------------------------------


def test_audit_reports_recovery_for_direct_text_message(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(
        db, msgtype="text", sender="staff_yingzi", tolist=["contact_zhangsan"],
        content_text="hi", msgtime=400,
    )

    before = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert before["counts_by_status"]["unreachable_missing_recipient"] == 1
    assert before["counts_by_status"]["reachable_direct"] == 0

    repair_missing_recipients(db, _TENANT_A)
    db.commit()

    after = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert after["counts_by_status"]["unreachable_missing_recipient"] == 0
    assert after["counts_by_status"]["reachable_direct"] == 1
    assert after["samples"][0]["message_db_id"] == msg.id
    assert after["samples"][0]["timeline_visible"] is True


def test_audit_reports_recovery_for_direct_voice_message(db) -> None:
    """Voice messages become reachable through the same generic recovery
    path — they still render via the existing unsupported-voice placeholder,
    this only concerns whether the timeline can find the message at all."""
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(
        db, msgtype="voice", sender="contact_wangwu", sdkfileid="sdk1",
        tolist=["staff_yingzi"], msgtime=401,
    )

    before = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert before["samples"][0]["reachability_status"] == "unreachable_missing_recipient"

    repair_missing_recipients(db, _TENANT_A)
    db.commit()

    after = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert after["samples"][0]["message_db_id"] == msg.id
    assert after["samples"][0]["reachability_status"] == "reachable_direct"


def test_audit_membership_recovers_once_recipients_are_backfilled(db) -> None:
    """
    A message with a stray extra recipient (beyond the direct pair) that
    still names the opposite party correctly stays candidate-eligible; this
    test exercises the direct membership lookup (_fetch_conversation_messages)
    actually finding the message post-repair, not just the recipient_count
    check clearing.
    """
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(
        db, msgtype="text", sender="staff_yingzi", tolist=["contact_zhangsan"],
        content_text="hi", msgtime=402,
    )

    before = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__contact_zhangsan___staff_yingzi",
        include_samples=True,
    )
    assert before["samples"][0]["reachability_status"] == "unreachable_missing_recipient"

    repair_missing_recipients(db, _TENANT_A)
    db.commit()

    after = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__contact_zhangsan___staff_yingzi",
        include_samples=True,
    )
    assert after["samples"][0]["message_db_id"] == msg.id
    assert after["samples"][0]["reachability_status"] == "reachable_direct"


# ---------------------------------------------------------------------------
# Part E — QA fix: malformed/blank tolist must not over-report success
# ---------------------------------------------------------------------------


def test_repair_returns_zero_for_tolist_of_only_blank_entries(db) -> None:
    """
    tolist=[""] is a candidate at the SQL layer (see
    build_missing_recipient_repair_query docstring) but has no real
    recipient data — _upsert_recipients() inserts nothing for a falsy
    entry, so this must not be counted as repaired.
    """
    msg = _insert_message(db, msgtype="text", sender="staff_a", tolist=[""], msgtime=306)

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 0
    assert _recipients_for(db, msg.id) == []


def test_repair_of_blank_tolist_leaves_audit_classification_unreachable(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="text", sender="staff_a", tolist=[""], msgtime=307)

    repair_missing_recipients(db, _TENANT_A)
    db.commit()

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    sample = next(s for s in report["samples"] if s["message_db_id"] == msg.id)
    assert sample["reachability_status"] == "unreachable_missing_recipient"
    assert sample["recipient_count"] == 0


def test_repair_of_mixed_blank_and_valid_tolist_inserts_only_the_valid_entry(db) -> None:
    msg = _insert_message(
        db, msgtype="text", sender="staff_a", tolist=["", "contact_a"], msgtime=308
    )

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 1
    assert _recipients_for(db, msg.id) == ["contact_a"]


def test_repair_of_tolist_with_duplicate_valid_recipients_dedupes_and_counts_as_one(db) -> None:
    """Duplicate valid recipients in tolist must still count as exactly one
    repaired message with exactly one persisted recipient row (not one row
    per duplicate, and not skipped as if it were invalid)."""
    msg = _insert_message(
        db, msgtype="text", sender="staff_a", tolist=["contact_a", "contact_a"], msgtime=309
    )

    repaired = repair_missing_recipients(db, _TENANT_A)
    db.commit()

    assert repaired == 1
    assert _recipients_for(db, msg.id) == ["contact_a"]


def test_audit_counts_by_status_stay_internally_consistent_after_repair(db) -> None:
    """reachable_count + unreachable_count must still equal scanned_count,
    and the sum of counts_by_status must still equal scanned_count, both
    before and after the repair runs — the fix must never let messages
    disappear from the audit's tally."""
    from app.reachability_audit import build_message_reachability_report

    _insert_message(
        db, msgtype="text", sender="staff_a", tolist=["contact_a"], msgtime=500
    )
    _insert_message(db, msgtype="text", sender=None, msgtime=501)  # missing sender, untouched by repair

    before = build_message_reachability_report(db, _TENANT_A)
    assert before["reachable_count"] + before["unreachable_count"] == before["scanned_count"]
    assert sum(before["counts_by_status"].values()) == before["scanned_count"]

    repair_missing_recipients(db, _TENANT_A)
    db.commit()

    after = build_message_reachability_report(db, _TENANT_A)
    assert after["scanned_count"] == before["scanned_count"]
    assert after["reachable_count"] + after["unreachable_count"] == after["scanned_count"]
    assert sum(after["counts_by_status"].values()) == after["scanned_count"]
