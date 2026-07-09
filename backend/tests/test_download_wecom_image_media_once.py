"""
Tests for RND-147 — one-shot image media download script.

Scope: scripts/download_wecom_image_media_once.py. Exercises the
candidate query shape (image-only, tenant-scoped, idempotent), the stale
"downloaded" repair scan (RND-147 QA fix), the byte signature detection
gate, the media_files state machine (pending -> downloaded / pending ->
failed), .part-file safety (write-then-atomic-rename, cleanup on every
failure path via a single try/finally), the non-blocking concurrent-run
lock, the media-identity collision guard, --count-only performing zero
writes, and that a script-produced media_files row is servable through
the existing RND-144 timeline/route logic unmodified.

Run (from backend/):
    pytest tests/test_download_wecom_image_media_once.py -v
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as RealSession

from tests.test_staff_seats import _msg


# ---------------------------------------------------------------------------
# Image type detection (byte signature, not extension/msgtype trust)
# ---------------------------------------------------------------------------


def test_detect_jpeg_signature() -> None:
    from app.media_storage import detect_image_type_from_bytes

    assert detect_image_type_from_bytes(b"\xff\xd8\xff\xe0rest") == ".jpg"


def test_detect_png_signature() -> None:
    from app.media_storage import detect_image_type_from_bytes

    assert detect_image_type_from_bytes(b"\x89PNG\r\n\x1a\nrest") == ".png"


def test_detect_gif_signature() -> None:
    from app.media_storage import detect_image_type_from_bytes

    assert detect_image_type_from_bytes(b"GIF89a" + b"rest") == ".gif"
    assert detect_image_type_from_bytes(b"GIF87a" + b"rest") == ".gif"


def test_detect_webp_signature() -> None:
    from app.media_storage import detect_image_type_from_bytes

    data = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"rest"
    assert detect_image_type_from_bytes(data) == ".webp"


def test_detect_unknown_binary_rejected() -> None:
    from app.media_storage import detect_image_type_from_bytes

    assert detect_image_type_from_bytes(b"not-an-image-at-all") is None
    assert detect_image_type_from_bytes(b"BM fake bmp bytes") is None
    assert detect_image_type_from_bytes(b"") is None


# ---------------------------------------------------------------------------
# Candidate queries — image-only, tenant-scoped, retry gating
# ---------------------------------------------------------------------------


def _compiled_sql(query) -> str:
    return str(query.statement.compile(compile_kwargs={"literal_binds": True}))


def test_candidate_query_is_image_only_and_tenant_scoped() -> None:
    from scripts.download_wecom_image_media_once import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", retry=False))

    assert "archive_messages.tenant_id = 'tenant-a'" in sql
    assert "archive_messages.msgtype = 'image'" in sql
    assert "archive_messages.decrypt_status = 'success'" in sql
    assert "media_files" in sql  # outer join present


def test_candidate_query_default_excludes_failed_and_downloaded_rows() -> None:
    from scripts.download_wecom_image_media_once import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", retry=False))

    assert "'pending'" in sql
    assert "'failed'" not in sql
    assert "'downloaded'" not in sql


def test_candidate_query_retry_flag_includes_failed_rows_but_never_downloaded() -> None:
    from scripts.download_wecom_image_media_once import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", retry=True))

    assert "'pending'" in sql
    assert "'failed'" in sql
    assert "'downloaded'" not in sql


def test_downloaded_repair_query_is_image_only_tenant_scoped_and_downloaded_only() -> None:
    from scripts.download_wecom_image_media_once import build_downloaded_repair_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_downloaded_repair_query(session, "tenant-a"))

    assert "archive_messages.tenant_id = 'tenant-a'" in sql
    assert "archive_messages.msgtype = 'image'" in sql
    assert "archive_messages.decrypt_status = 'success'" in sql
    assert "media_files.download_status = 'downloaded'" in sql


# ---------------------------------------------------------------------------
# RND-151 — since_ms recency filter and newest_first ordering (query shape)
# ---------------------------------------------------------------------------


def test_candidate_query_default_omits_since_filter_and_orders_by_id_ascending() -> None:
    """Backward-compat: with no since_ms/newest_first args, the query shape
    (and therefore behavior) is unchanged from pre-RND-151."""
    from scripts.download_wecom_image_media_once import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", retry=False))

    assert "msgtime >=" not in sql
    assert "ORDER BY archive_messages.id" in sql
    assert "DESC" not in sql


def test_candidate_query_since_ms_adds_msgtime_lower_bound() -> None:
    from scripts.download_wecom_image_media_once import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(
            build_candidate_query(session, "tenant-a", retry=False, since_ms=1000)
        )

    assert "archive_messages.msgtime >= 1000" in sql


def test_candidate_query_newest_first_orders_by_msgtime_desc_then_id_desc() -> None:
    from scripts.download_wecom_image_media_once import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(
            build_candidate_query(session, "tenant-a", retry=False, newest_first=True)
        )

    assert "ORDER BY archive_messages.msgtime DESC, archive_messages.id DESC" in sql


def test_since_ms_cutoff_is_derived_from_current_utc_time() -> None:
    from scripts.download_wecom_image_media_once import _since_ms_cutoff

    now_ms = int(time.time() * 1000)
    cutoff = _since_ms_cutoff(72)
    expected = now_ms - 72 * 3600 * 1000
    # allow a small tolerance for wall-clock drift between the two time.time() calls
    assert abs(cutoff - expected) < 5000


# ---------------------------------------------------------------------------
# is_downloaded_media_file_stale — reuses the RND-144 servability predicate
# ---------------------------------------------------------------------------


def test_is_downloaded_media_file_stale_false_for_valid_servable_file(tmp_path, monkeypatch) -> None:
    from scripts.download_wecom_image_media_once import is_downloaded_media_file_stale

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"\xff\xd8\xff fake jpeg")

    media_file = SimpleNamespace(local_path=str(img))
    assert is_downloaded_media_file_stale(media_file) is False


def test_is_downloaded_media_file_stale_true_for_missing_file(tmp_path, monkeypatch) -> None:
    from scripts.download_wecom_image_media_once import is_downloaded_media_file_stale

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    media_file = SimpleNamespace(local_path=str(tmp_path / "nope.jpg"))
    assert is_downloaded_media_file_stale(media_file) is True


def test_is_downloaded_media_file_stale_true_for_disallowed_extension(tmp_path, monkeypatch) -> None:
    from scripts.download_wecom_image_media_once import is_downloaded_media_file_stale

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img = tmp_path / "photo.bmp"
    img.write_bytes(b"BM fake bmp bytes")
    media_file = SimpleNamespace(local_path=str(img))
    assert is_downloaded_media_file_stale(media_file) is True


def test_is_downloaded_media_file_stale_true_when_outside_storage_root(tmp_path, monkeypatch) -> None:
    from scripts.download_wecom_image_media_once import is_downloaded_media_file_stale

    root = tmp_path / "media_root"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xff fake jpeg")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(root))

    media_file = SimpleNamespace(local_path=str(outside))
    assert is_downloaded_media_file_stale(media_file) is True


# ---------------------------------------------------------------------------
# select_candidates — combines actionable + stale-downloaded repairs,
# budget-capped so the repair scan can never crowd out fresh candidates
# ---------------------------------------------------------------------------


def _query_mock(all_result=None, count_result=0, first_result=None):
    q = MagicMock()
    q.outerjoin.return_value = q
    q.join.return_value = q
    q.filter.return_value = q
    q.order_by.return_value = q
    q.limit.return_value = q
    q.all.return_value = list(all_result or [])
    q.count.return_value = count_result
    q.first.return_value = first_result
    return q


def test_select_candidates_combines_actionable_and_stale_repairs(monkeypatch, tmp_path) -> None:
    import scripts.download_wecom_image_media_once as script

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    good_path = tmp_path / "good.jpg"
    good_path.write_bytes(b"\xff\xd8\xff ok")

    good_media = SimpleNamespace(local_path=str(good_path))
    missing_media = SimpleNamespace(local_path=str(tmp_path / "missing.jpg"))

    actionable_msgs = [SimpleNamespace(id=1, sdkfileid="sdk-1")]
    downloaded_rows = [
        (SimpleNamespace(id=2, sdkfileid="sdk-2"), good_media),
        (SimpleNamespace(id=3, sdkfileid="sdk-3"), missing_media),
    ]

    actionable_q = _query_mock(all_result=actionable_msgs, count_result=1)
    downloaded_q = _query_mock(all_result=downloaded_rows)

    monkeypatch.setattr(
        script,
        "build_candidate_query",
        lambda _session, _tenant_id, _retry, since_ms=None, newest_first=False: actionable_q,
    )
    monkeypatch.setattr(
        script, "build_downloaded_repair_query", lambda _session, _tenant_id: downloaded_q
    )

    actionable, repairs, total = script.select_candidates(
        MagicMock(), "tenant-a", retry=False, limit=10
    )

    assert actionable == actionable_msgs
    assert total == 1
    assert len(repairs) == 1
    assert repairs[0][0].id == 3  # only the stale (missing-file) row


def test_select_candidates_skips_repair_scan_when_limit_exhausted_by_actionable(monkeypatch) -> None:
    import scripts.download_wecom_image_media_once as script

    actionable_msgs = [SimpleNamespace(id=i, sdkfileid=f"sdk-{i}") for i in range(3)]
    actionable_q = _query_mock(all_result=actionable_msgs, count_result=3)

    called = {"repair": False}

    def _repair_query(*_a, **_k):
        called["repair"] = True
        raise AssertionError("must not scan for repairs when limit already exhausted")

    monkeypatch.setattr(
        script,
        "build_candidate_query",
        lambda _session, _tenant_id, _retry, since_ms=None, newest_first=False: actionable_q,
    )
    monkeypatch.setattr(script, "build_downloaded_repair_query", _repair_query)

    actionable, repairs, total = script.select_candidates(
        MagicMock(), "tenant-a", retry=False, limit=3
    )
    assert len(actionable) == 3
    assert repairs == []
    assert called["repair"] is False


def test_select_candidates_caps_repairs_to_remaining_budget(monkeypatch, tmp_path) -> None:
    import scripts.download_wecom_image_media_once as script

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))

    actionable_msgs = [SimpleNamespace(id=1, sdkfileid="sdk-1")]  # uses 1 of limit=2
    stale_media = SimpleNamespace(local_path=str(tmp_path / "missing.jpg"))
    downloaded_rows = [
        (SimpleNamespace(id=2, sdkfileid="sdk-2"), stale_media),
        (SimpleNamespace(id=3, sdkfileid="sdk-3"), stale_media),
    ]

    actionable_q = _query_mock(all_result=actionable_msgs, count_result=1)
    downloaded_q = _query_mock(all_result=downloaded_rows)

    monkeypatch.setattr(
        script,
        "build_candidate_query",
        lambda _session, _tenant_id, _retry, since_ms=None, newest_first=False: actionable_q,
    )
    monkeypatch.setattr(
        script, "build_downloaded_repair_query", lambda _session, _tenant_id: downloaded_q
    )

    actionable, repairs, _total = script.select_candidates(
        MagicMock(), "tenant-a", retry=False, limit=2
    )
    assert len(actionable) == 1
    assert len(repairs) == 1  # only 1 of the 2 stale rows fits the remaining budget


# ---------------------------------------------------------------------------
# Stale-repair starvation regression (RND-147 QA fix)
#
# The bug: the repair scan previously applied the caller's remaining
# --limit budget as a SQL LIMIT on the "downloaded" query *before* running
# the RND-144 servability check in Python. If enough valid/servable
# "downloaded" rows sorted ahead of a genuinely stale one to fill that
# LIMIT, the stale row was never even fetched from the database — it would
# never be selected for repair, no matter how many times the script
# re-ran. A MagicMock-based query stub can't catch this (it ignores the
# argument passed to .limit()), so these tests use a real, file-backed
# SQLite database that actually enforces LIMIT, to prove the fix holds
# against genuine SQL semantics rather than mocked ones.
# ---------------------------------------------------------------------------


def _make_sqlite_engine(tmp_path):
    from sqlalchemy import text

    engine = create_engine(f"sqlite:///{tmp_path / 'repair_scan_test.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
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
                )
                """
            )
        )
        conn.execute(
            text(
                """
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
                )
                """
            )
        )
    return engine


def _insert_downloaded_message(session, msg_id: int, tenant_id: str, sdkfileid: str, local_path: str) -> None:
    from app.db.models import ArchiveMessage, MediaFile

    session.add(
        ArchiveMessage(
            id=msg_id,
            msgid=f"m-{msg_id}",
            seq=msg_id,
            publickey_ver=1,
            encrypt_random_key="k",
            encrypt_chat_msg="c",
            decrypt_status="success",
            msgtype="image",
            sdkfileid=sdkfileid,
            tenant_id=tenant_id,
        )
    )
    session.add(
        MediaFile(
            sdkfileid=sdkfileid,
            archive_message_id=msg_id,
            tenant_id=tenant_id,
            file_type="image",
            local_path=local_path,
            download_status="downloaded",
        )
    )
    session.commit()


def _insert_pending_message(
    session, msg_id: int, tenant_id: str, sdkfileid: str, msgtime: int
) -> None:
    """Insert an image ArchiveMessage with no media_files row at all —
    i.e. a fresh actionable candidate per build_candidate_query — at a
    given msgtime (epoch-ms), for RND-151 recency/ordering tests."""
    from app.db.models import ArchiveMessage

    session.add(
        ArchiveMessage(
            id=msg_id,
            msgid=f"m-{msg_id}",
            seq=msg_id,
            publickey_ver=1,
            encrypt_random_key="k",
            encrypt_chat_msg="c",
            decrypt_status="success",
            msgtype="image",
            sdkfileid=sdkfileid,
            tenant_id=tenant_id,
            msgtime=msgtime,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# RND-151 regression: recent images must not be starved by old expired ones
#
# The bug: the downloader selected fresh candidates oldest-first with no
# recency filter, so on a tenant with many old, already platform-expired
# image messages, --limit was fully consumed by those old rows before ever
# reaching recently ingested ones — new images silently never got
# downloaded. --since-hours + --newest-first fix this at the candidate
# selection layer (not via any manual SQL update).
# ---------------------------------------------------------------------------


def test_select_candidates_since_hours_and_newest_first_prioritizes_recent(
    tmp_path, monkeypatch
) -> None:
    from scripts.download_wecom_image_media_once import _since_ms_cutoff, select_candidates

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    now_ms = int(time.time() * 1000)
    retention_ms = 72 * 3600 * 1000

    # 37 old messages, well outside the 72h retention window, lower ids so
    # they would be selected first under the old oldest-first-by-id default.
    for i in range(1, 38):
        _insert_pending_message(
            session, i, "tenant-a", f"sdk-old-{i}", now_ms - retention_ms - (i * 1000)
        )
    # 23 recent messages, inside the window, higher ids and higher msgtime.
    for i in range(38, 61):
        _insert_pending_message(
            session, i, "tenant-a", f"sdk-recent-{i}", now_ms - (i * 1000)
        )

    since_ms = _since_ms_cutoff(72)
    actionable, _repairs, _total = select_candidates(
        session, "tenant-a", retry=False, limit=10, since_ms=since_ms, newest_first=True
    )

    assert len(actionable) == 10
    # every selected candidate must come from the recent batch (id >= 38),
    # never one of the 37 old expired candidates.
    assert all(msg.id >= 38 for msg in actionable)
    # newest-first: strictly descending msgtime across the selected batch.
    msgtimes = [msg.msgtime for msg in actionable]
    assert msgtimes == sorted(msgtimes, reverse=True)


def test_select_candidates_since_hours_excludes_old_candidates_entirely(tmp_path, monkeypatch) -> None:
    from scripts.download_wecom_image_media_once import _since_ms_cutoff, select_candidates

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    now_ms = int(time.time() * 1000)
    retention_ms = 72 * 3600 * 1000

    _insert_pending_message(session, 1, "tenant-a", "sdk-old-1", now_ms - retention_ms - 60_000)
    _insert_pending_message(session, 2, "tenant-a", "sdk-recent-1", now_ms - 60_000)

    since_ms = _since_ms_cutoff(72)
    actionable, _repairs, _total = select_candidates(
        session, "tenant-a", retry=False, limit=10, since_ms=since_ms
    )

    assert [msg.id for msg in actionable] == [2]


def test_select_candidates_without_since_hours_default_is_oldest_first_by_id(
    tmp_path, monkeypatch
) -> None:
    """Default behavior (no --since-hours/--newest-first) stays unchanged:
    oldest-first by ascending id, no recency exclusion."""
    from scripts.download_wecom_image_media_once import select_candidates

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    now_ms = int(time.time() * 1000)
    _insert_pending_message(session, 1, "tenant-a", "sdk-1", now_ms - 999_000_000)
    _insert_pending_message(session, 2, "tenant-a", "sdk-2", now_ms)

    actionable, _repairs, _total = select_candidates(session, "tenant-a", retry=False, limit=10)

    assert [msg.id for msg in actionable] == [1, 2]


def _insert_failed_message(
    session, msg_id: int, tenant_id: str, sdkfileid: str, msgtime: int
) -> None:
    """Insert an image ArchiveMessage with an existing media_files row whose
    download_status is 'failed' — only actionable with --retry."""
    from app.db.models import ArchiveMessage, MediaFile

    session.add(
        ArchiveMessage(
            id=msg_id,
            msgid=f"m-{msg_id}",
            seq=msg_id,
            publickey_ver=1,
            encrypt_random_key="k",
            encrypt_chat_msg="c",
            decrypt_status="success",
            msgtype="image",
            sdkfileid=sdkfileid,
            tenant_id=tenant_id,
            msgtime=msgtime,
        )
    )
    session.add(
        MediaFile(sdkfileid=sdkfileid, archive_message_id=msg_id, download_status="failed")
    )
    session.commit()


def test_select_candidates_old_failed_rows_do_not_block_recent_no_row_candidates(
    tmp_path, monkeypatch
) -> None:
    """Requirement: existing failed old media rows must not consume the
    --limit budget ahead of recent messages that have no media row at all,
    once --since-hours excludes the old ones from the window."""
    from scripts.download_wecom_image_media_once import _since_ms_cutoff, select_candidates

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    now_ms = int(time.time() * 1000)
    retention_ms = 72 * 3600 * 1000

    # Old messages with an existing "failed" media row — only actionable
    # with --retry, and low ids so they'd sort first under oldest-first.
    for i in range(1, 6):
        _insert_failed_message(
            session, i, "tenant-a", f"sdk-old-failed-{i}", now_ms - retention_ms - (i * 1000)
        )
    # Recent messages with no media row at all.
    for i in range(6, 11):
        _insert_pending_message(session, i, "tenant-a", f"sdk-recent-{i}", now_ms - (i * 1000))

    since_ms = _since_ms_cutoff(72)
    actionable, _repairs, _total = select_candidates(
        session, "tenant-a", retry=True, limit=5, since_ms=since_ms, newest_first=True
    )

    assert len(actionable) == 5
    assert all(msg.id >= 6 for msg in actionable)


def test_select_candidates_stale_row_not_starved_by_preceding_valid_rows(tmp_path, monkeypatch) -> None:
    """Required RND-147 regression test: 5 valid/servable "downloaded"
    rows are inserted first (lower ids, so they sort first per
    build_downloaded_repair_query's ORDER BY ArchiveMessage.id), then one
    stale "downloaded" row (missing file) with a higher id. With a real
    SQL LIMIT applied before Python filtering (the old, buggy behavior),
    limit=1 would return only the first valid row and find zero stale
    rows. With the fix, the stale row must still be found and selected."""
    from scripts.download_wecom_image_media_once import select_candidates

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    for i in range(1, 6):
        good_path = tmp_path / f"good-{i}.jpg"
        good_path.write_bytes(b"\xff\xd8\xff ok")
        _insert_downloaded_message(session, i, "tenant-a", f"sdk-{i}", str(good_path))

    _insert_downloaded_message(session, 6, "tenant-a", "sdk-6", str(tmp_path / "missing.jpg"))

    actionable, repairs, total = select_candidates(session, "tenant-a", retry=False, limit=1)

    assert actionable == []
    assert total == 0  # no fresh pending/no-row candidates exist
    assert len(repairs) == 1
    assert repairs[0][0].id == 6  # the stale row, not one of the 5 valid ones


def test_select_candidates_valid_downloaded_rows_never_consume_repair_quota(tmp_path, monkeypatch) -> None:
    """Companion assertion: even with a larger limit, valid/servable
    "downloaded" rows must never appear in the repair list at all — only
    genuinely stale rows count against the quota."""
    from scripts.download_wecom_image_media_once import select_candidates

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    for i in range(1, 4):
        good_path = tmp_path / f"good-{i}.jpg"
        good_path.write_bytes(b"\xff\xd8\xff ok")
        _insert_downloaded_message(session, i, "tenant-a", f"sdk-{i}", str(good_path))
    _insert_downloaded_message(session, 4, "tenant-a", "sdk-4", str(tmp_path / "missing.jpg"))

    _actionable, repairs, _total = select_candidates(session, "tenant-a", retry=False, limit=10)

    assert [msg.id for msg, _mf in repairs] == [4]


def test_scan_for_stale_downloaded_paginates_across_batches_without_starving_stale_row(
    tmp_path, monkeypatch
) -> None:
    """Directly exercises _scan_for_stale_downloaded with a small internal
    batch_size (independent of the caller's --limit) to prove the
    ascending-id cursor correctly walks past a full batch of valid rows to
    find a stale row in a later batch — not just a single-LIMIT query."""
    from scripts.download_wecom_image_media_once import _scan_for_stale_downloaded

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    for i in range(1, 5):  # 4 valid rows — more than one batch of size 2
        good_path = tmp_path / f"good-{i}.jpg"
        good_path.write_bytes(b"\xff\xd8\xff ok")
        _insert_downloaded_message(session, i, "tenant-a", f"sdk-{i}", str(good_path))
    _insert_downloaded_message(session, 5, "tenant-a", "sdk-5", str(tmp_path / "missing.jpg"))

    stale = _scan_for_stale_downloaded(session, "tenant-a", needed=1, batch_size=2)

    assert len(stale) == 1
    assert stale[0][0].id == 5


# ---------------------------------------------------------------------------
# get_or_reset_media_file — state transitions + identity/collision guard
# ---------------------------------------------------------------------------


def test_get_or_reset_media_file_creates_pending_row_when_absent() -> None:
    from scripts.download_wecom_image_media_once import get_or_reset_media_file

    session = MagicMock()
    q = MagicMock()
    q.filter.return_value = q
    q.first.return_value = None
    session.query.return_value = q

    get_or_reset_media_file(session, "tenant-a", "sdk-1", 100)

    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.tenant_id == "tenant-a"
    assert added.sdkfileid == "sdk-1"
    assert added.archive_message_id == 100
    assert added.download_status == "pending"
    session.commit.assert_called_once()


def test_get_or_reset_media_file_resets_existing_failed_row_to_pending() -> None:
    from app.db.models import MediaFile
    from scripts.download_wecom_image_media_once import get_or_reset_media_file

    existing = MediaFile(
        sdkfileid="sdk-1",
        archive_message_id=100,
        download_status="failed",
        local_path="/old/path.jpg",
        file_size=123,
        oss_key=None,
    )

    session = MagicMock()
    q = MagicMock()
    q.filter.return_value = q
    q.first.return_value = existing
    session.query.return_value = q

    row = get_or_reset_media_file(session, "tenant-a", "sdk-1", 100)

    assert row is existing
    assert row.download_status == "pending"
    assert row.local_path is None
    assert row.file_size is None
    session.add.assert_not_called()


def test_get_or_reset_media_file_resets_stale_downloaded_row_belonging_to_same_message() -> None:
    from app.db.models import MediaFile
    from scripts.download_wecom_image_media_once import get_or_reset_media_file

    existing = MediaFile(
        sdkfileid="sdk-1",
        archive_message_id=100,
        download_status="downloaded",
        local_path="/old/stale.jpg",
        file_size=999,
    )

    session = MagicMock()
    q = MagicMock()
    q.filter.return_value = q
    q.first.return_value = existing
    session.query.return_value = q

    row = get_or_reset_media_file(session, "tenant-a", "sdk-1", 100)

    assert row is existing
    assert row.download_status == "pending"
    assert row.local_path is None


def test_get_or_reset_media_file_refuses_to_reuse_row_from_a_different_message() -> None:
    """RND-147 QA fix: an existing row whose archive_message_id does not
    match must never be reset/reused — that would silently reassign
    another message's media row (a genuine identifier collision within the
    same tenant)."""
    from app.db.models import MediaFile
    from scripts.download_wecom_image_media_once import get_or_reset_media_file

    existing = MediaFile(
        sdkfileid="sdk-shared",
        archive_message_id=999,
        download_status="downloaded",
        local_path="/somewhere/other.jpg",
        file_size=42,
    )

    session = MagicMock()
    q = MagicMock()
    q.filter.return_value = q
    q.first.return_value = existing
    session.query.return_value = q

    result = get_or_reset_media_file(session, "tenant-a", "sdk-shared", 42)  # different archive_message_id

    assert result is None
    session.add.assert_not_called()
    session.commit.assert_not_called()
    # The conflicting row must be completely untouched.
    assert existing.archive_message_id == 999
    assert existing.download_status == "downloaded"
    assert existing.local_path == "/somewhere/other.jpg"
    assert existing.file_size == 42


# ---------------------------------------------------------------------------
# download_one — chunk assembly, byte-sniffing gate, .part safety
# ---------------------------------------------------------------------------


def test_download_one_success_writes_and_atomically_renames(tmp_path, monkeypatch) -> None:
    import scripts.download_wecom_image_media_once as script

    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body-bytes"
    monkeypatch.setattr(
        script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([jpeg_bytes])
    )

    outcome, detail = script.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 42, "sdk-1", timeout=5
    )

    assert outcome == "downloaded"
    final_path = Path(detail)
    assert final_path.suffix == ".jpg"
    assert final_path.read_bytes() == jpeg_bytes
    assert not (final_path.parent / "42.part").exists()


def test_download_one_multi_chunk_assembly(tmp_path, monkeypatch) -> None:
    import scripts.download_wecom_image_media_once as script

    part_a = b"\x89PNG\r\n\x1a\n"
    part_b = b"rest-of-png-data"
    monkeypatch.setattr(
        script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([part_a, part_b])
    )

    outcome, detail = script.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 5, "sdk-png", timeout=5
    )

    assert outcome == "downloaded"
    assert Path(detail).read_bytes() == part_a + part_b
    assert Path(detail).suffix == ".png"


def test_download_one_unsupported_type_rejected_and_part_cleaned_up(tmp_path, monkeypatch) -> None:
    import scripts.download_wecom_image_media_once as script

    garbage = b"totally-not-an-image-signature"
    monkeypatch.setattr(
        script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([garbage])
    )

    outcome, detail = script.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 7, "sdk-2", timeout=5
    )

    assert outcome == "failed"
    assert detail == "unsupported_type"
    directory = tmp_path / "tenants" / "tenant-a" / "images"
    assert not (directory / "7.part").exists()
    assert not any(directory.glob("7.*"))


def test_download_one_sdk_error_leaves_no_part_file(tmp_path, monkeypatch) -> None:
    import scripts.download_wecom_image_media_once as script

    def _raise(*_a, **_k):
        raise script.wecom_sdk.SdkMediaError("boom")

    monkeypatch.setattr(script.wecom_sdk, "iter_media_chunks", _raise)

    outcome, detail = script.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 9, "sdk-3", timeout=5
    )

    assert outcome == "failed"
    assert detail == "sdk_error"
    directory = tmp_path / "tenants" / "tenant-a" / "images"
    assert not (directory / "9.part").exists()


def test_download_one_empty_payload_is_failure(tmp_path, monkeypatch) -> None:
    import scripts.download_wecom_image_media_once as script

    monkeypatch.setattr(script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([]))

    outcome, detail = script.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 11, "sdk-4", timeout=5
    )
    assert outcome == "failed"
    assert detail == "empty_payload"


def test_download_one_write_failure_leaves_no_part_file(tmp_path, monkeypatch) -> None:
    """RND-147 QA blocker regression: a write_bytes() failure (e.g. disk
    full, permission error) must still clean up the .part temp file —
    previously only the unsupported-type and rename-failure paths did."""
    import scripts.download_wecom_image_media_once as script

    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    monkeypatch.setattr(
        script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([jpeg_bytes])
    )

    def _raise_write(_self, _data):
        raise OSError("simulated disk write failure")

    monkeypatch.setattr(Path, "write_bytes", _raise_write)

    outcome, detail = script.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 13, "sdk-5", timeout=5
    )

    assert outcome == "failed"
    assert detail == "write_error"
    directory = tmp_path / "tenants" / "tenant-a" / "images"
    assert not (directory / "13.part").exists()
    assert not any(directory.glob("13.*"))


def test_download_one_rename_failure_leaves_no_part_file(tmp_path, monkeypatch) -> None:
    import scripts.download_wecom_image_media_once as script
    from app import media_storage

    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    monkeypatch.setattr(
        script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([jpeg_bytes])
    )
    monkeypatch.setattr(
        media_storage.os, "replace", MagicMock(side_effect=OSError("simulated rename failure"))
    )

    outcome, detail = script.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 15, "sdk-6", timeout=5
    )

    assert outcome == "failed"
    assert detail == "rename_error"
    directory = tmp_path / "tenants" / "tenant-a" / "images"
    assert not (directory / "15.part").exists()
    assert not any(directory.glob("15.*"))


# ---------------------------------------------------------------------------
# Concurrent-run lock
# ---------------------------------------------------------------------------


def test_acquire_lock_returns_fd_when_available(tmp_path) -> None:
    import scripts.download_wecom_image_media_once as script

    lock_path = str(tmp_path / "sub" / "media.lock")
    fd = script._acquire_lock(lock_path)
    assert fd is not None
    script._release_lock(fd)


def test_acquire_lock_returns_none_when_already_held(tmp_path) -> None:
    import scripts.download_wecom_image_media_once as script

    lock_path = str(tmp_path / "media.lock")
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = script._acquire_lock(lock_path)
        assert result is None
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_main_exits_cleanly_without_db_access_when_lock_held(tmp_path, monkeypatch, capsys) -> None:
    import scripts.download_wecom_image_media_once as script

    lock_path = str(tmp_path / "media.lock")
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", lock_path)
    monkeypatch.setattr(
        script,
        "create_engine",
        MagicMock(side_effect=AssertionError("must not touch DB when lock is held")),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--count-only"])

    try:
        with pytest.raises(SystemExit) as exc:
            script.main()
        assert exc.value.code == 0
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    captured = capsys.readouterr()
    assert "already holds the run lock" in captured.out
    assert lock_path not in captured.out


def test_main_releases_lock_after_completion(tmp_path, monkeypatch) -> None:
    """After one run completes, the lock must be released so a subsequent
    invocation can acquire it (no permanent lockout from a single run)."""
    import scripts.download_wecom_image_media_once as script

    lock_path = str(tmp_path / "media.lock")
    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", lock_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")

    tenant_row = SimpleNamespace(tenant_id="tenant-a")

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.ArchiveMessage:
            return _query_mock(count_result=0)
        raise AssertionError(f"unexpected model queried: {model}")

    session = MagicMock()
    session.query.side_effect = _query

    @contextmanager
    def _fake_session(_engine):
        yield session

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(
        script, "select_candidates", lambda *_a, **_k: ([], [], 0)
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--count-only"])

    with pytest.raises(SystemExit):
        script.main()

    # Lock must be free now — acquiring it directly must succeed.
    fd = script._acquire_lock(lock_path)
    assert fd is not None
    script._release_lock(fd)


# ---------------------------------------------------------------------------
# main() end-to-end: --count-only, pending->downloaded, pending->failed,
# identity-conflict safety
# ---------------------------------------------------------------------------


def _use_scratch_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", str(tmp_path / "media-download.lock"))


def test_count_only_performs_no_writes_and_never_touches_sdk(tmp_path, monkeypatch, capsys) -> None:
    import scripts.download_wecom_image_media_once as script

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    candidate_msg = SimpleNamespace(id=1, sdkfileid="sdk-should-not-be-printed")

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.ArchiveMessage:
            return _query_mock(count_result=5)
        raise AssertionError(f"unexpected model queried in count-only mode: {model}")

    session = MagicMock()
    session.query.side_effect = _query
    session.add.side_effect = AssertionError("must not write in count-only mode")
    session.commit.side_effect = AssertionError("must not commit in count-only mode")

    @contextmanager
    def _fake_session(_engine):
        yield session

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(
        script, "select_candidates", lambda *_a, **_k: ([candidate_msg], [], 3)
    )
    monkeypatch.setattr(
        script.wecom_sdk,
        "load_sdk",
        MagicMock(side_effect=AssertionError("must not load SDK in count-only mode")),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--count-only"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    captured = capsys.readouterr()
    assert "candidate_total: 3" in captured.out
    assert "candidate_ordering: oldest_first" in captured.out
    assert "candidates_with_existing_media_row: 5" in captured.out
    assert "sdk-should-not-be-printed" not in captured.out


@pytest.mark.parametrize("bad_value", ["0", "-5"])
def test_main_rejects_non_positive_since_hours(bad_value, tmp_path, monkeypatch, capsys) -> None:
    import scripts.download_wecom_image_media_once as script

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setattr(
        script,
        "create_engine",
        MagicMock(side_effect=AssertionError("must not touch DB on validation failure")),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--count-only", "--since-hours", bad_value])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 1

    captured = capsys.readouterr()
    assert "--since-hours must be a positive number" in captured.out


def test_count_only_with_since_hours_and_newest_first_reports_window_stats_and_no_writes(
    tmp_path, monkeypatch, capsys
) -> None:
    """RND-151: --count-only combined with --since-hours/--newest-first must
    still perform zero DB writes, zero file writes, and zero SDK calls, and
    must report the new aggregate window stats without leaking identifiers."""
    import scripts.download_wecom_image_media_once as script

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    candidate_msg = SimpleNamespace(id=1, sdkfileid="sdk-should-not-be-printed")

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.ArchiveMessage:
            return _query_mock(count_result=7)
        raise AssertionError(f"unexpected model queried in count-only mode: {model}")

    session = MagicMock()
    session.query.side_effect = _query
    session.add.side_effect = AssertionError("must not write in count-only mode")
    session.commit.side_effect = AssertionError("must not commit in count-only mode")

    @contextmanager
    def _fake_session(_engine):
        yield session

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(
        script, "select_candidates", lambda *_a, **_k: ([candidate_msg], [], 10)
    )
    monkeypatch.setattr(
        script.wecom_sdk,
        "load_sdk",
        MagicMock(side_effect=AssertionError("must not load SDK in count-only mode")),
    )
    monkeypatch.setattr(
        sys, "argv", ["prog", "--count-only", "--since-hours", "72", "--newest-first"]
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    captured = capsys.readouterr()
    assert "candidate_ordering: newest_first" in captured.out
    assert "since_hours: 72.0" in captured.out
    assert "candidates_in_window: 7" in captured.out
    assert "candidates_excluded_by_window: 3" in captured.out  # 10 total - 7 in window
    assert "candidates_with_existing_media_row: 7" in captured.out
    assert "sdk-should-not-be-printed" not in captured.out


def _run_main_with_one_candidate(monkeypatch, tmp_path, jpeg_or_garbage_chunks, extra_args=None):
    import scripts.download_wecom_image_media_once as script
    from app.db.models import MediaFile

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("WECOM_ARCHIVE_SECRET", "secret")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    candidate_msg = SimpleNamespace(id=1, sdkfileid="sdk-secret-1")
    media_file_row = MediaFile(sdkfileid="sdk-secret-1", archive_message_id=1, download_status="pending")

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.MediaFile:
            return _query_mock(first_result=media_file_row)
        raise AssertionError(f"unexpected model queried: {model}")

    session = MagicMock()
    session.query.side_effect = _query

    @contextmanager
    def _fake_session(_engine):
        yield session

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(
        script, "select_candidates", lambda *_a, **_k: ([candidate_msg], [], 1)
    )
    monkeypatch.setattr(script.wecom_sdk, "load_sdk", lambda _path: MagicMock())
    monkeypatch.setattr(script.wecom_sdk, "configure_sdk", lambda _lib: None)
    monkeypatch.setattr(script.wecom_sdk, "configure_sdk_media_data", lambda _lib: None)
    monkeypatch.setattr(script.wecom_sdk, "new_sdk", lambda _lib: 123)
    monkeypatch.setattr(script.wecom_sdk, "init_sdk", lambda _lib, _h, _c, _s: 0)
    monkeypatch.setattr(script.wecom_sdk, "destroy_sdk", lambda _lib, _h: None)
    monkeypatch.setattr(
        script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter(jpeg_or_garbage_chunks)
    )

    monkeypatch.setattr(sys, "argv", ["prog"] + (extra_args or []))
    return script, media_file_row, session


def test_main_pending_to_downloaded_transition(tmp_path, monkeypatch, capsys) -> None:
    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    script, media_file_row, _session = _run_main_with_one_candidate(monkeypatch, tmp_path, [jpeg_bytes])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.download_status == "downloaded"
    assert media_file_row.file_type == "image"
    assert media_file_row.oss_key is None
    assert media_file_row.local_path is not None
    assert Path(media_file_row.local_path).read_bytes() == jpeg_bytes
    assert media_file_row.file_size == len(jpeg_bytes)

    captured = capsys.readouterr()
    assert "sdk-secret-1" not in captured.out
    assert media_file_row.local_path not in captured.out


def test_main_pending_to_failed_transition_on_unsupported_type(tmp_path, monkeypatch, capsys) -> None:
    garbage = b"not-a-real-image"
    script, media_file_row, _session = _run_main_with_one_candidate(monkeypatch, tmp_path, [garbage])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.download_status == "failed"
    assert media_file_row.local_path is None

    captured = capsys.readouterr()
    assert "sdk-secret-1" not in captured.out


def test_main_db_commit_failure_after_download_removes_orphaned_file(tmp_path, monkeypatch) -> None:
    """RND-147 QA fix: if the DB commit fails right after a successful
    download+atomic-rename, the now-orphaned final file (not a .part file
    anymore) must be removed rather than left on disk with no
    corresponding media_files record."""
    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    script, media_file_row, session = _run_main_with_one_candidate(monkeypatch, tmp_path, [jpeg_bytes])

    commit_calls = {"count": 0}

    def _commit_side_effect():
        commit_calls["count"] += 1
        if commit_calls["count"] == 2:  # 1st = get_or_reset_media_file's reset commit
            raise RuntimeError("simulated DB commit failure")
        return None

    session.commit.side_effect = _commit_side_effect

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    directory = tmp_path / "tenants" / "tenant-a" / "images"
    assert not any(directory.glob("1.*"))


def test_main_media_identity_conflict_fails_safely_without_overwrite_or_download(
    tmp_path, monkeypatch, capsys
) -> None:
    import scripts.download_wecom_image_media_once as script
    from app.db.models import MediaFile

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("WECOM_ARCHIVE_SECRET", "secret")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    candidate_msg = SimpleNamespace(id=1, sdkfileid="sdk-shared")
    conflicting_row = MediaFile(
        sdkfileid="sdk-shared",
        archive_message_id=999,  # belongs to a different message
        download_status="downloaded",
        local_path="/somewhere/other.jpg",
    )

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.MediaFile:
            return _query_mock(first_result=conflicting_row)
        raise AssertionError(f"unexpected model queried: {model}")

    session = MagicMock()
    session.query.side_effect = _query

    @contextmanager
    def _fake_session(_engine):
        yield session

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(script, "select_candidates", lambda *_a, **_k: ([candidate_msg], [], 1))
    monkeypatch.setattr(script.wecom_sdk, "load_sdk", lambda _p: MagicMock())
    monkeypatch.setattr(script.wecom_sdk, "configure_sdk", lambda _l: None)
    monkeypatch.setattr(script.wecom_sdk, "configure_sdk_media_data", lambda _l: None)
    monkeypatch.setattr(script.wecom_sdk, "new_sdk", lambda _l: 123)
    monkeypatch.setattr(script.wecom_sdk, "init_sdk", lambda _l, _h, _c, _s: 0)
    monkeypatch.setattr(script.wecom_sdk, "destroy_sdk", lambda _l, _h: None)
    monkeypatch.setattr(
        script.wecom_sdk,
        "iter_media_chunks",
        MagicMock(side_effect=AssertionError("must not attempt download on identity conflict")),
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    # The conflicting row must be left completely untouched — no overwrite.
    assert conflicting_row.archive_message_id == 999
    assert conflicting_row.download_status == "downloaded"
    assert conflicting_row.local_path == "/somewhere/other.jpg"

    captured = capsys.readouterr()
    assert "sdk-shared" not in captured.out
    assert "media_identity_conflict" in captured.out


def test_main_failed_row_not_retried_without_flag(tmp_path, monkeypatch) -> None:
    """The candidate query itself excludes 'failed' rows unless --retry is
    passed (covered at the SQL level above); this asserts main() threads
    the CLI flag into select_candidates correctly by default (no --retry
    given)."""
    import scripts.download_wecom_image_media_once as script

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    seen_retry_flags = []

    def _spy_select_candidates(_session, _tenant_id, retry, _limit, since_ms=None, newest_first=False):
        seen_retry_flags.append(retry)
        return [], [], 0

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.ArchiveMessage:
            return _query_mock(count_result=0)
        raise AssertionError(f"unexpected model queried: {model}")

    session = MagicMock()
    session.query.side_effect = _query

    @contextmanager
    def _fake_session(_engine):
        yield session

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(script, "select_candidates", _spy_select_candidates)
    monkeypatch.setattr(sys, "argv", ["prog", "--count-only"])

    with pytest.raises(SystemExit):
        script.main()

    assert seen_retry_flags == [False]


def test_main_retry_flag_threads_through_to_select_candidates(tmp_path, monkeypatch) -> None:
    import scripts.download_wecom_image_media_once as script

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    seen_retry_flags = []

    def _spy_select_candidates(_session, _tenant_id, retry, _limit, since_ms=None, newest_first=False):
        seen_retry_flags.append(retry)
        return [], [], 0

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.ArchiveMessage:
            return _query_mock(count_result=0)
        raise AssertionError(f"unexpected model queried: {model}")

    session = MagicMock()
    session.query.side_effect = _query

    @contextmanager
    def _fake_session(_engine):
        yield session

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(script, "select_candidates", _spy_select_candidates)
    monkeypatch.setattr(sys, "argv", ["prog", "--count-only", "--retry"])

    with pytest.raises(SystemExit):
        script.main()

    assert seen_retry_flags == [True]


# ---------------------------------------------------------------------------
# Script output stays servable through existing RND-144 route/timeline logic
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_script_produced_media_file_is_servable_via_rnd144_timeline(
    client, monkeypatch, tmp_path
) -> None:
    """Downloads a real image via download_one(), then feeds the resulting
    media_files-shaped row through the exact same timeline serializer
    RND-144 ships — proving this script's output needs no changes to that
    route to become visible in the admin UI."""
    import scripts.download_wecom_image_media_once as script
    from app.main import app
    from app.auth import get_current_user
    from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact, MediaFile
    from app.db.session import get_db

    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))

    jpeg_bytes = b"\xff\xd8\xff" + b"real-jpeg-body"
    monkeypatch.setattr(
        script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([jpeg_bytes])
    )
    outcome, final_path = script.download_one(
        MagicMock(), MagicMock(), media_root, "tenant-a", 1, "sdk-1", timeout=5
    )
    assert outcome == "downloaded"

    all_msgs = [_msg(1, "staff_a", roomid="room1", msgtime=1000, msgtype="image", sdkfileid="sdk-1")]
    all_msgs[0].msgid = "m-1"
    media_files = [
        SimpleNamespace(
            archive_message_id=1,
            download_status="downloaded",
            local_path=final_path,
            file_type="image",
        )
    ]

    def _override_db():
        mock = MagicMock()

        msg_q = MagicMock()
        msg_q.filter.return_value = msg_q
        msg_q.all.return_value = list(all_msgs)

        empty_q = MagicMock()
        empty_q.filter.return_value = empty_q
        empty_q.all.return_value = []

        media_q = MagicMock()
        media_q.filter.return_value = media_q
        media_q.all.return_value = list(media_files)

        def _query(model):
            if model is ArchiveMessageRecipient:
                return empty_q
            if model is Contact:
                return empty_q
            if model is MediaFile:
                return media_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), "tenant-a")
    app.dependency_overrides[get_db] = _override_db
    try:
        resp = client.get("/api/conversations/room1/messages")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["media_status"] == "available"
    assert msg["media_url"] == "/api/conversations/room1/messages/m-1/media"
    assert "sdk-1" not in resp.text
    assert final_path not in resp.text
