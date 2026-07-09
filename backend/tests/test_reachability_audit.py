"""
Tests for RND-178 — Message Reachability Audit.

Validates:
  - classify_message_reachability() — pure decision function, no DB.
  - build_message_reachability_report() — end-to-end audit against a real
    (sqlite-backed) database, reusing the actual conversation-membership
    functions from app.routers.conversations rather than a parallel
    reimplementation.
  - The admin diagnostic endpoint (/api/admin/reachability-audit) never
    exposes message content, raw payloads, or WeCom/media identifiers.
  - Tenant isolation: one tenant's audit never sees another tenant's data.
  - Existing conversation timeline pagination/continuity is not broken by
    this work (duplicate timestamps, unsupported-type placeholders).

Run (from backend/):
    pytest tests/test_reachability_audit.py -v

archive_messages/archive_message_recipients use PostgreSQL JSONB columns;
tests here build a hand-written sqlite schema (matching the production
column set exactly, minus postgres-only indexes) so the real ORM classes
and real query logic in app.routers.conversations can be exercised end to
end without requiring a live Postgres instance.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import ArchiveMessage, ArchiveMessageRecipient

# ---------------------------------------------------------------------------
# sqlite-backed test schema (see module docstring for why this isn't
# Base.metadata.create_all — that fails on JSONB/GIN/to_tsvector, all
# postgres-only features irrelevant to reachability logic).
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE tenants (
    id TEXT PRIMARY KEY, name TEXT, slug TEXT, is_active INTEGER,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE archive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid TEXT NOT NULL,
    seq INTEGER NOT NULL,
    publickey_ver INTEGER NOT NULL,
    raw_encrypted_payload TEXT,
    encrypt_random_key TEXT NOT NULL,
    encrypt_chat_msg TEXT NOT NULL,
    decrypt_status TEXT NOT NULL DEFAULT 'pending',
    decrypted_payload TEXT,
    content_text TEXT,
    msgtype TEXT,
    sender TEXT,
    roomid TEXT,
    msgtime INTEGER,
    tolist TEXT,
    sdkfileid TEXT,
    tenant_id TEXT,
    created_at TEXT
);
CREATE TABLE archive_message_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    receiver_userid TEXT NOT NULL,
    receiver_type TEXT,
    tenant_id TEXT,
    created_at TEXT
);
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wecom_userid TEXT NOT NULL,
    name TEXT,
    tenant_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE admin_users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    wecom_user_id TEXT NOT NULL,
    name TEXT,
    avatar_url TEXT,
    last_login_at TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sdkfileid TEXT NOT NULL,
    archive_message_id INTEGER NOT NULL,
    file_type TEXT,
    local_path TEXT,
    oss_key TEXT,
    file_size INTEGER,
    download_status TEXT NOT NULL DEFAULT 'pending',
    tenant_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(tenant_id, sdkfileid)
);
"""


def _make_session() -> Session:
    # StaticPool + check_same_thread=False: the TestClient dispatches
    # requests to a worker thread (via anyio.to_thread), but a bare
    # "sqlite:///:memory:" connection is thread-affined by default and the
    # in-memory DB only exists on that one connection — this keeps a single
    # shared connection alive and usable from any thread.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    return Session(engine)


@pytest.fixture()
def db():
    session = _make_session()
    yield session
    session.close()


_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"


def _insert_message(db: Session, **kwargs) -> ArchiveMessage:
    defaults = dict(
        seq=1,
        publickey_ver=1,
        encrypt_random_key="x",
        encrypt_chat_msg="y",
        decrypt_status="success",
        tenant_id=_TENANT_A,
    )
    defaults.update(kwargs)
    defaults.setdefault("msgid", f"msg-{defaults['seq']}-{id(defaults)}")
    msg = ArchiveMessage(**defaults)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _insert_recipient(db: Session, message_id: int, receiver_userid: str, tenant_id: str = _TENANT_A):
    r = ArchiveMessageRecipient(
        message_id=message_id,
        receiver_userid=receiver_userid,
        receiver_type="user",
        tenant_id=tenant_id,
    )
    db.add(r)
    db.commit()
    return r


# ---------------------------------------------------------------------------
# Part A — classify_message_reachability(): pure decision function, no DB
# ---------------------------------------------------------------------------


def test_classify_reachable_direct() -> None:
    from app.reachability_audit import ReachabilityStatus, classify_message_reachability

    status, reason = classify_message_reachability(
        decrypt_status="success",
        has_sender=True,
        has_room=False,
        recipient_count=1,
        membership_found=True,
    )
    assert status is ReachabilityStatus.REACHABLE_DIRECT
    assert reason == "ok"


def test_classify_reachable_group() -> None:
    from app.reachability_audit import ReachabilityStatus, classify_message_reachability

    status, reason = classify_message_reachability(
        decrypt_status="success",
        has_sender=True,
        has_room=True,
        recipient_count=3,
        membership_found=True,
    )
    assert status is ReachabilityStatus.REACHABLE_GROUP
    assert reason == "ok"


def test_classify_missing_sender_takes_priority_over_everything_else() -> None:
    """Sender check runs first — even with room+recipients present, no sender means unreachable_missing_sender."""
    from app.reachability_audit import ReachabilityStatus, classify_message_reachability

    status, reason = classify_message_reachability(
        decrypt_status="success",
        has_sender=False,
        has_room=True,
        recipient_count=5,
        membership_found=True,
    )
    assert status is ReachabilityStatus.UNREACHABLE_MISSING_SENDER
    assert reason == "sender_null_or_unresolvable"


def test_classify_missing_recipient_for_direct_message() -> None:
    from app.reachability_audit import ReachabilityStatus, classify_message_reachability

    status, reason = classify_message_reachability(
        decrypt_status="success",
        has_sender=True,
        has_room=False,
        recipient_count=0,
        membership_found=False,
    )
    assert status is ReachabilityStatus.UNREACHABLE_MISSING_RECIPIENT
    assert reason == "direct_message_has_no_recipient_rows"


def test_classify_missing_room_for_group_shaped_fanout() -> None:
    from app.reachability_audit import ReachabilityStatus, classify_message_reachability

    status, reason = classify_message_reachability(
        decrypt_status="success",
        has_sender=True,
        has_room=False,
        recipient_count=2,
        membership_found=False,
    )
    assert status is ReachabilityStatus.UNREACHABLE_MISSING_ROOM
    assert reason == "roomid_missing_for_multi_recipient_fanout"


def test_classify_membership_mismatch_when_basic_data_present_but_lookup_fails() -> None:
    from app.reachability_audit import ReachabilityStatus, classify_message_reachability

    status, reason = classify_message_reachability(
        decrypt_status="success",
        has_sender=True,
        has_room=False,
        recipient_count=1,
        membership_found=False,
    )
    assert status is ReachabilityStatus.UNREACHABLE_MEMBERSHIP
    assert reason == "conversation_membership_lookup_did_not_return_message"


def test_classify_non_success_decrypt_status_is_unreachable_other() -> None:
    from app.reachability_audit import ReachabilityStatus, classify_message_reachability

    status, reason = classify_message_reachability(
        decrypt_status="pending",
        has_sender=True,
        has_room=False,
        recipient_count=1,
        membership_found=True,
    )
    assert status is ReachabilityStatus.UNREACHABLE_OTHER
    assert reason == "not_decrypt_success"


# ---------------------------------------------------------------------------
# Part B — build_message_reachability_report(): real sqlite DB, real
# conversation-membership functions reused from app.routers.conversations
# ---------------------------------------------------------------------------


def test_direct_text_reachability(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="text", sender="staff_yingzi", content_text="hi", msgtime=100)
    _insert_recipient(db, msg.id, "contact_zhangsan")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert report["scanned_count"] == 1
    assert report["reachable_count"] == 1
    sample = report["samples"][0]
    assert sample["reachability_status"] == "reachable_direct"
    assert sample["timeline_visible"] is True


def test_direct_voice_reachability(db) -> None:
    """Timeline visibility, not playback, is what this audit verifies for voice messages."""
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="voice", sender="contact_wangwu", sdkfileid="sdk123", msgtime=200)
    _insert_recipient(db, msg.id, "staff_yingzi")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    sample = report["samples"][0]
    assert sample["reachability_status"] == "reachable_direct"
    assert sample["message_type"] == "voice"


def test_group_text_reachability(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="text", sender="contact_a", roomid="room_001", content_text="hi group", msgtime=300)
    _insert_recipient(db, msg.id, "staff_b")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    sample = report["samples"][0]
    assert sample["reachability_status"] == "reachable_group"
    assert sample["conversation_type"] == "group"


def test_group_voice_reachability(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="voice", sender="contact_a", roomid="room_001", sdkfileid="sdk999", msgtime=400)
    _insert_recipient(db, msg.id, "staff_b")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    sample = report["samples"][0]
    assert sample["reachability_status"] == "reachable_group"
    assert sample["message_type"] == "voice"


def test_missing_recipient_direct_message(db) -> None:
    """Sender resolved, no roomid, but recipient rows never got written (recipient-persistence failure)."""
    from app.reachability_audit import build_message_reachability_report

    _insert_message(db, msgtype="text", sender="staff_yingzi", content_text="orphaned", msgtime=500)
    # deliberately no recipient row inserted

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    sample = report["samples"][0]
    assert sample["reachability_status"] == "unreachable_missing_recipient"
    assert sample["reason_code"] == "direct_message_has_no_recipient_rows"
    assert sample["timeline_visible"] is False
    # Must not be misclassified as a membership or frontend-rendering problem.
    assert sample["reachability_status"] != "unreachable_membership"


def test_missing_room_group_like_fanout(db) -> None:
    """No roomid, but the multi-recipient fan-out shape is a group broadcast, not a direct message."""
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="text", sender="staff_yingzi", roomid=None, content_text="broadcast", msgtime=600)
    _insert_recipient(db, msg.id, "contact_a")
    _insert_recipient(db, msg.id, "contact_b")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    sample = report["samples"][0]
    assert sample["reachability_status"] == "unreachable_missing_room"


def test_missing_sender(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="text", sender=None, roomid="room_002", content_text="ghost", msgtime=700)
    _insert_recipient(db, msg.id, "staff_yingzi")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    sample = report["samples"][0]
    assert sample["reachability_status"] == "unreachable_missing_sender"
    assert sample["has_sender"] is False


def test_membership_mismatch_when_conversation_id_encoding_collides(db) -> None:
    """
    sender/recipient rows are both present (basic data looks fine), but the
    triple-underscore direct conversation_id delimiter collides with a
    userid that itself contains "___" — a genuine conversation-membership
    aggregation bug the audit must surface as unreachable_membership rather
    than silently mislabel as reachable or as a recipient/room problem.
    """
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="text", sender="contact_a___b", content_text="edge case", msgtime=800)
    _insert_recipient(db, msg.id, "contact_c")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    sample = report["samples"][0]
    assert sample["reachability_status"] == "unreachable_membership"
    assert sample["reason_code"] == "conversation_membership_lookup_did_not_return_message"


def test_duplicate_timestamp_all_messages_counted(db) -> None:
    """Multiple messages sharing the exact same msgtime must all be counted, none dropped."""
    from app.reachability_audit import build_message_reachability_report

    for i in range(5):
        msg = _insert_message(db, msgtype="text", sender="staff_yingzi", content_text=f"m{i}", msgtime=900)
        _insert_recipient(db, msg.id, "contact_zhangsan")

    report = build_message_reachability_report(db, _TENANT_A)
    assert report["scanned_count"] == 5
    assert report["reachable_count"] == 5


def test_tenant_isolation_excludes_other_tenant_messages(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg_a = _insert_message(db, msgtype="text", sender="staff_a", tenant_id=_TENANT_A, content_text="a", msgtime=1000)
    _insert_recipient(db, msg_a.id, "contact_a", tenant_id=_TENANT_A)

    msg_b = _insert_message(db, msgtype="text", sender="staff_b", tenant_id=_TENANT_B, content_text="b", msgtime=1000)
    _insert_recipient(db, msg_b.id, "contact_b", tenant_id=_TENANT_B)

    report_a = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert report_a["scanned_count"] == 1
    assert report_a["samples"][0]["message_db_id"] == msg_a.id

    report_b = build_message_reachability_report(db, _TENANT_B, include_samples=True)
    assert report_b["scanned_count"] == 1
    assert report_b["samples"][0]["message_db_id"] == msg_b.id


def test_counts_by_status_stays_stable_across_mixed_scenarios(db) -> None:
    """Sanity check that per-status counts sum correctly across a mixed batch."""
    from app.reachability_audit import build_message_reachability_report

    reachable = _insert_message(db, msgtype="text", sender="staff_x", msgtime=1100)
    _insert_recipient(db, reachable.id, "contact_x")

    missing_recipient = _insert_message(db, msgtype="text", sender="staff_y", msgtime=1101)

    missing_sender = _insert_message(db, msgtype="text", sender=None, roomid="room_z", msgtime=1102)
    _insert_recipient(db, missing_sender.id, "staff_y")

    report = build_message_reachability_report(db, _TENANT_A)
    assert report["scanned_count"] == 3
    assert report["reachable_count"] == 1
    assert report["unreachable_count"] == 2
    assert report["counts_by_status"]["reachable_direct"] == 1
    assert report["counts_by_status"]["unreachable_missing_recipient"] == 1
    assert report["counts_by_status"]["unreachable_missing_sender"] == 1
    assert sum(report["counts_by_status"].values()) == 3


# ---------------------------------------------------------------------------
# Part C — Sensitive field protection
# ---------------------------------------------------------------------------

_FORBIDDEN_SUBSTRINGS = (
    "content_text",
    "raw_encrypted_payload",
    "decrypted_payload",
    "encrypt_random_key",
    "encrypt_chat_msg",
    "sdkfileid",
    "msgid",
    "seq",
    "local_path",
    "oss_key",
)


def test_report_never_includes_banned_fields(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(
        db,
        msgtype="text",
        sender="staff_yingzi",
        content_text="this is secret customer content",
        sdkfileid="wecom-sdk-file-id-should-never-leak",
        msgid="wecom-msgid-should-never-leak",
        msgtime=1200,
    )
    _insert_recipient(db, msg.id, "contact_zhangsan")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)

    import json

    serialized = json.dumps(report)
    assert "secret customer content" not in serialized
    assert "wecom-sdk-file-id-should-never-leak" not in serialized
    assert "wecom-msgid-should-never-leak" not in serialized

    sample = report["samples"][0]
    for forbidden_key in _FORBIDDEN_SUBSTRINGS:
        assert forbidden_key not in sample


def test_sample_count_is_capped_by_sample_limit(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    for i in range(10):
        msg = _insert_message(db, msgtype="text", sender="staff_x", msgtime=1300 + i)
        _insert_recipient(db, msg.id, "contact_x")

    report = build_message_reachability_report(
        db, _TENANT_A, include_samples=True, sample_limit=3
    )
    assert report["scanned_count"] == 10
    assert len(report["samples"]) == 3


def test_samples_absent_when_include_samples_false(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="text", sender="staff_x", msgtime=1400)
    _insert_recipient(db, msg.id, "contact_x")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=False)
    assert "samples" not in report


# ---------------------------------------------------------------------------
# Part D — Admin diagnostic endpoint: auth + tenant scoping + no sensitive fields
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_reachability_audit_endpoint_requires_auth(client) -> None:
    from app.db.session import get_db
    from app.main import app

    def _mock_db_no_session():
        mock = MagicMock()
        mock.query.return_value.filter.return_value.first.return_value = None
        yield mock

    app.dependency_overrides[get_db] = _mock_db_no_session
    try:
        resp = client.get("/api/admin/reachability-audit")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_reachability_audit_endpoint_returns_tenant_scoped_report(client, db) -> None:
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    msg = _insert_message(db, msgtype="text", sender="staff_yingzi", content_text="hi", msgtime=1500)
    _insert_recipient(db, msg.id, "contact_zhangsan")

    mock_user = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: (mock_user, _TENANT_A)
    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        resp = client.get("/api/admin/reachability-audit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scanned_count"] == 1
        assert data["reachable_count"] == 1
        assert "samples" not in data or data["samples"] is None
        assert "content_text" not in resp.text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Part E — Timeline continuity / duplicate timestamps via the real endpoint
# (regression guard: this work must not disturb existing pagination)
# ---------------------------------------------------------------------------


def test_conversation_timeline_endpoint_handles_duplicate_timestamps_and_mixed_types(client, db) -> None:
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    room = "room_continuity"
    msgs = []
    for i, msgtype in enumerate(["text", "voice", "location", "text"]):
        msg = _insert_message(
            db,
            msgtype=msgtype,
            sender="contact_a",
            roomid=room,
            content_text="hello" if msgtype == "text" else None,
            sdkfileid="sdk1" if msgtype == "voice" else None,
            msgtime=5000,  # identical msgtime for all — must not drop any
            seq=i + 1,
        )
        _insert_recipient(db, msg.id, "staff_b")
        msgs.append(msg)

    mock_user = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: (mock_user, _TENANT_A)
    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        resp = client.get(f"/api/conversations/{room}/messages?limit=20")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 4

        by_type = {m["msgtype"]: m for m in data["messages"]}
        assert by_type["location"]["unsupported_reason"] == "unsupported_msgtype"
        assert by_type["location"]["media_type"] == "unsupported"
        assert by_type["voice"]["media_type"] == "voice"

        # Force pagination with limit=2 and confirm cursor walks older history
        # without dropping/duplicating any of the same-msgtime rows.
        resp_page1 = client.get(f"/api/conversations/{room}/messages?limit=2")
        page1 = resp_page1.json()
        assert len(page1["messages"]) == 2
        assert page1["pagination"]["has_older"] is True
        cursor = page1["pagination"]["next_before"]

        resp_page2 = client.get(
            f"/api/conversations/{room}/messages?limit=2&before={cursor}"
        )
        page2 = resp_page2.json()
        assert len(page2["messages"]) == 2
        assert page2["pagination"]["has_older"] is False

        all_msgids = {m["msgid"] for m in page1["messages"]} | {
            m["msgid"] for m in page2["messages"]
        }
        assert all_msgids == {m.msgid for m in msgs}
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Part F — QA follow-up fix 1: full-audit completeness semantics
#
# A single call only ever classifies `limit` rows starting at `offset` — the
# report must say so explicitly (matching_total / scanned_count / has_more)
# instead of reading like the whole archive was checked.
# ---------------------------------------------------------------------------


def test_matching_total_exceeds_scanned_count_when_more_rows_than_limit(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    for i in range(7):
        msg = _insert_message(db, msgtype="text", sender="staff_x", msgtime=2000 + i)
        _insert_recipient(db, msg.id, "contact_x")

    report = build_message_reachability_report(db, _TENANT_A, limit=3, offset=0)
    assert report["scanned_count"] == 3
    assert report["matching_total"] == 7
    assert report["limit"] == 3
    assert report["offset"] == 0
    assert report["has_more"] is True


def test_has_more_false_on_final_page(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    for i in range(7):
        msg = _insert_message(db, msgtype="text", sender="staff_x", msgtime=2100 + i)
        _insert_recipient(db, msg.id, "contact_x")

    report = build_message_reachability_report(db, _TENANT_A, limit=3, offset=6)
    assert report["scanned_count"] == 1
    assert report["matching_total"] == 7
    assert report["offset"] == 6
    assert report["has_more"] is False


def test_offset_walks_through_pages_without_gaps_or_duplicates(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    ids = []
    for i in range(10):
        msg = _insert_message(db, msgtype="text", sender="staff_x", msgtime=2200 + i)
        _insert_recipient(db, msg.id, "contact_x")
        ids.append(msg.id)

    seen = []
    offset = 0
    while True:
        report = build_message_reachability_report(
            db, _TENANT_A, limit=4, offset=offset, include_samples=True
        )
        seen.extend(s["message_db_id"] for s in report["samples"])
        offset += report["scanned_count"]
        if not report["has_more"]:
            break

    assert sorted(seen) == sorted(ids)


def test_report_never_claims_full_coverage_when_only_a_page_was_scanned(db) -> None:
    """matching_total must reflect all matching messages, not just the scanned page."""
    from app.reachability_audit import build_message_reachability_report

    for i in range(5):
        msg = _insert_message(db, msgtype="text", sender="staff_x", msgtime=2300 + i)
        _insert_recipient(db, msg.id, "contact_x")

    report = build_message_reachability_report(db, _TENANT_A, limit=2, offset=0)
    assert report["scanned_count"] != report["matching_total"]
    assert report["matching_total"] == 5
    assert "max_scan_limit" in report


def test_matching_total_respects_filters_not_just_tenant(db) -> None:
    """matching_total must count only rows matching the same filters as the scan, e.g. message_type."""
    from app.reachability_audit import build_message_reachability_report

    for i in range(3):
        msg = _insert_message(db, msgtype="text", sender="staff_x", msgtime=2400 + i)
        _insert_recipient(db, msg.id, "contact_x")
    for i in range(2):
        msg = _insert_message(db, msgtype="voice", sender="staff_x", sdkfileid="sdk", msgtime=2500 + i)
        _insert_recipient(db, msg.id, "contact_x")

    report = build_message_reachability_report(
        db, _TENANT_A, message_type="voice", limit=1, offset=0
    )
    assert report["matching_total"] == 2
    assert report["scanned_count"] == 1
    assert report["has_more"] is True


# ---------------------------------------------------------------------------
# Part G — QA follow-up fix 2: exact direct conversation_id matching
# ---------------------------------------------------------------------------


def test_conversation_id_filter_includes_exact_direct_pair_both_directions(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg1 = _insert_message(db, msgtype="text", sender="staff_a", msgtime=3000)
    _insert_recipient(db, msg1.id, "contact_a")
    msg2 = _insert_message(db, msgtype="text", sender="contact_a", msgtime=3001)
    _insert_recipient(db, msg2.id, "staff_a")

    report = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__staff_a___contact_a", include_samples=True
    )
    ids = {s["message_db_id"] for s in report["samples"]}
    assert ids == {msg1.id, msg2.id}
    assert report["scanned_count"] == 2


def test_conversation_id_filter_excludes_same_sender_different_recipient(db) -> None:
    """staff_a -> contact_b must not appear when auditing direct__staff_a___contact_a."""
    from app.reachability_audit import build_message_reachability_report

    wanted = _insert_message(db, msgtype="text", sender="staff_a", msgtime=3100)
    _insert_recipient(db, wanted.id, "contact_a")

    unrelated = _insert_message(db, msgtype="text", sender="staff_a", msgtime=3101)
    _insert_recipient(db, unrelated.id, "contact_b")

    report = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__staff_a___contact_a", include_samples=True
    )
    ids = {s["message_db_id"] for s in report["samples"]}
    assert ids == {wanted.id}
    assert unrelated.id not in ids


def test_conversation_id_filter_excludes_same_recipient_different_sender(db) -> None:
    """staff_b -> contact_a must not appear when auditing direct__staff_a___contact_a."""
    from app.reachability_audit import build_message_reachability_report

    wanted = _insert_message(db, msgtype="text", sender="staff_a", msgtime=3200)
    _insert_recipient(db, wanted.id, "contact_a")

    unrelated = _insert_message(db, msgtype="text", sender="staff_b", msgtime=3201)
    _insert_recipient(db, unrelated.id, "contact_a")

    report = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__staff_a___contact_a", include_samples=True
    )
    ids = {s["message_db_id"] for s in report["samples"]}
    assert ids == {wanted.id}
    assert unrelated.id not in ids


def test_conversation_id_filter_excludes_group_messages(db) -> None:
    """A group message involving both participants must not leak into a direct conversation audit."""
    from app.reachability_audit import build_message_reachability_report

    direct_msg = _insert_message(db, msgtype="text", sender="staff_a", msgtime=3300)
    _insert_recipient(db, direct_msg.id, "contact_a")

    group_msg = _insert_message(
        db, msgtype="text", sender="staff_a", roomid="room_xyz", msgtime=3301
    )
    _insert_recipient(db, group_msg.id, "contact_a")

    report = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__staff_a___contact_a", include_samples=True
    )
    ids = {s["message_db_id"] for s in report["samples"]}
    assert ids == {direct_msg.id}
    assert group_msg.id not in ids


def test_conversation_id_filter_includes_direct_voice_and_text(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    text_msg = _insert_message(db, msgtype="text", sender="staff_a", msgtime=3400)
    _insert_recipient(db, text_msg.id, "contact_a")
    voice_msg = _insert_message(
        db, msgtype="voice", sender="contact_a", sdkfileid="sdk1", msgtime=3401
    )
    _insert_recipient(db, voice_msg.id, "staff_a")

    report = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__staff_a___contact_a", include_samples=True
    )
    ids = {s["message_db_id"] for s in report["samples"]}
    assert ids == {text_msg.id, voice_msg.id}
    statuses = {s["reachability_status"] for s in report["samples"]}
    assert statuses == {"reachable_direct"}


def test_conversation_id_filter_still_surfaces_missing_recipient_for_named_sender(db) -> None:
    """A message from staff_a with zero recipient rows is exactly the bug this audit
    exists to catch — it must not be filtered out of its own pair's candidate scan."""
    from app.reachability_audit import build_message_reachability_report

    orphaned = _insert_message(db, msgtype="text", sender="staff_a", msgtime=3500)
    # no recipient row inserted

    report = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__staff_a___contact_a", include_samples=True
    )
    ids = {s["message_db_id"] for s in report["samples"]}
    assert ids == {orphaned.id}
    assert report["samples"][0]["reachability_status"] == "unreachable_missing_recipient"


# ---------------------------------------------------------------------------
# Part H — QA follow-up fix 3: recipient loading must defend tenant scope
# ---------------------------------------------------------------------------


def test_cross_tenant_recipient_row_is_ignored(db) -> None:
    """
    A recipient row that (through malformed/corrupt data) carries the wrong
    tenant_id must never be trusted by tenant A's audit, even though it
    correctly references a tenant-A message_id.
    """
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="text", sender="staff_a", tenant_id=_TENANT_A, msgtime=4000)
    # Recipient row references the right message_id but the wrong tenant_id —
    # simulates corrupt/malformed data, not a normal code path.
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_B)

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert report["scanned_count"] == 1
    sample = report["samples"][0]
    # The mismatched row must not count as a valid recipient for tenant A.
    assert sample["recipient_count"] == 0
    assert sample["reachability_status"] == "unreachable_missing_recipient"


def test_cross_tenant_recipient_row_does_not_enable_false_membership(db) -> None:
    """
    Even a message with roomid (group) present must not treat a cross-tenant
    recipient row as legitimate — reachability for the group case does not
    depend on recipients, but the recipient_count itself must not include
    another tenant's row (Fix 3 requires this defensively regardless of the
    classification path it feeds into).
    """
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(
        db, msgtype="text", sender="staff_a", roomid="room_1", tenant_id=_TENANT_A, msgtime=4100
    )
    _insert_recipient(db, msg.id, "cross_tenant_contact", tenant_id=_TENANT_B)

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    sample = report["samples"][0]
    assert sample["recipient_count"] == 0
    # Group membership only depends on roomid, so this is still reachable —
    # but must not credit the cross-tenant row as a real recipient.
    assert sample["reachability_status"] == "reachable_group"


def test_cross_tenant_recipient_row_never_appears_in_tenant_a_counts_or_samples(db) -> None:
    from app.reachability_audit import build_message_reachability_report

    msg_a = _insert_message(db, msgtype="text", sender="staff_a", tenant_id=_TENANT_A, msgtime=4200)
    _insert_recipient(db, msg_a.id, "contact_a", tenant_id=_TENANT_A)

    # A completely separate tenant-B message + its own (correctly tenant-B) recipient.
    msg_b = _insert_message(db, msgtype="text", sender="staff_b", tenant_id=_TENANT_B, msgtime=4200)
    _insert_recipient(db, msg_b.id, "contact_b", tenant_id=_TENANT_B)

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert report["scanned_count"] == 1
    sample = report["samples"][0]
    assert sample["message_db_id"] == msg_a.id
    assert sample["reachability_status"] == "reachable_direct"
    assert sample["recipient_count"] == 1

    serialized_ids = {s["message_db_id"] for s in report["samples"]}
    assert msg_b.id not in serialized_ids


# ---------------------------------------------------------------------------
# Part I — QA follow-up: malformed direct-recipient graphs must never crash
# the audit (self-recipient rows, duplicate self-recipient rows, and any
# other membership-derivation failure we haven't enumerated).
# ---------------------------------------------------------------------------


def test_self_recipient_excluded_from_direct_conversation_candidate_set(db) -> None:
    """sender=staff_a, recipient=[staff_a] names no opposite participant and
    must not be treated as part of direct__staff_a___contact_a."""
    from app.reachability_audit import build_message_reachability_report

    malformed = _insert_message(db, msgtype="text", sender="staff_a", msgtime=5000)
    _insert_recipient(db, malformed.id, "staff_a")

    report = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__staff_a___contact_a", include_samples=True
    )
    assert report["scanned_count"] == 0
    ids = {s["message_db_id"] for s in report["samples"]}
    assert malformed.id not in ids


def test_self_recipient_message_does_not_crash_full_tenant_audit(db) -> None:
    """
    Without a conversation_id filter there is no candidate-set narrowing —
    this message reaches conversation-membership derivation directly, which
    (before this fix) produced the single-party id "direct__staff_a" and
    _fetch_conversation_messages raised HTTPException, aborting the whole
    audit. It must now be classified unreachable instead, and the rest of
    the batch must still be classified normally.
    """
    from app.reachability_audit import build_message_reachability_report

    malformed = _insert_message(db, msgtype="text", sender="staff_a", msgtime=5100)
    _insert_recipient(db, malformed.id, "staff_a")
    other = _insert_message(db, msgtype="text", sender="staff_x", msgtime=5101)
    _insert_recipient(db, other.id, "contact_x")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)

    assert report["scanned_count"] == 2
    by_id = {s["message_db_id"]: s for s in report["samples"]}
    assert by_id[malformed.id]["reachability_status"] in (
        "unreachable_membership",
        "unreachable_other",
    )
    assert by_id[other.id]["reachability_status"] == "reachable_direct"


def test_duplicate_self_recipient_rows_do_not_crash(db) -> None:
    """
    Two identical (sender-duplicating) recipient rows must never raise —
    whatever bucket they land in (a 2-recipient fan-out with no roomid is
    itself flagged unreachable_missing_room before membership derivation
    even runs), the point is the scan completes and nothing is reachable.
    """
    from app.reachability_audit import build_message_reachability_report

    malformed = _insert_message(db, msgtype="text", sender="staff_a", msgtime=5200)
    _insert_recipient(db, malformed.id, "staff_a")
    _insert_recipient(db, malformed.id, "staff_a")

    # Full-tenant scan: must not raise.
    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)
    assert report["scanned_count"] == 1
    sample = report["samples"][0]
    assert sample["reachability_status"].startswith("unreachable_")
    assert sample["timeline_visible"] is False

    # Conversation-scoped scan: must exclude it from the candidate set.
    scoped_report = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__staff_a___contact_a", include_samples=True
    )
    assert scoped_report["scanned_count"] == 0


def test_mixed_recipients_never_included_as_valid_direct_conversation(db) -> None:
    """recipient rows [contact_a, contact_b] for direct__staff_a___contact_a must be excluded."""
    from app.reachability_audit import build_message_reachability_report

    msg = _insert_message(db, msgtype="text", sender="staff_a", msgtime=5300)
    _insert_recipient(db, msg.id, "contact_a")
    _insert_recipient(db, msg.id, "contact_b")

    report = build_message_reachability_report(
        db, _TENANT_A, conversation_id="direct__staff_a___contact_a", include_samples=True
    )
    assert report["scanned_count"] == 0
    ids = {s["message_db_id"] for s in report["samples"]}
    assert msg.id not in ids


def test_invalid_membership_derivation_is_caught_and_audit_continues(monkeypatch, db) -> None:
    """
    Force the real membership lookup to raise (simulating any malformed-data
    path beyond the self-recipient one) and verify the audit still completes
    and classifies the rest of the batch instead of letting the exception
    escape.
    """
    from app.reachability_audit import build_message_reachability_report
    import app.routers.conversations as conversations_module

    def _boom(db, conv_id, tenant_id):
        raise RuntimeError("simulated malformed conversation id")

    monkeypatch.setattr(conversations_module, "_fetch_conversation_messages", _boom)

    broken = _insert_message(db, msgtype="text", sender="staff_a", msgtime=5400)
    _insert_recipient(db, broken.id, "contact_a")
    also_needs_check = _insert_message(
        db, msgtype="text", sender="contact_z", roomid="room_ok", msgtime=5401
    )
    _insert_recipient(db, also_needs_check.id, "staff_z")

    report = build_message_reachability_report(db, _TENANT_A, include_samples=True)

    assert report["scanned_count"] == 2
    by_id = {s["message_db_id"]: s for s in report["samples"]}
    assert by_id[broken.id]["reachability_status"] == "unreachable_membership"
    assert by_id[broken.id]["reason_code"] == "conversation_membership_lookup_errored"
    # The forced failure applies to every membership lookup in this run, so
    # both messages land on unreachable_membership — the point is that
    # neither call raised and the scan completed for both.
    assert by_id[also_needs_check.id]["reachability_status"] == "unreachable_membership"
    assert by_id[also_needs_check.id]["reason_code"] == "conversation_membership_lookup_errored"
