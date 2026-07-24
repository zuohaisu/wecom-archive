"""
Tests for RND-149 — render admin timestamps in Beijing time.

Validates:
  - _fmt_msgtime() converts UTC epoch-ms to Beijing time (UTC+8, no DST).
  - Day-boundary conversion (UTC time that rolls to the next Beijing day).
  - None/invalid input falls back safely without raising.
  - /admin/messages list and detail pages render Beijing-time strings.
  - Message ordering (query .order_by) and pagination are unaffected by
    display-only formatting — RND-149 must not touch sort/cursor logic.
  - Consistent timezone labeling (QA follow-up): formatted values (both the
    Python _fmt_msgtime and the JS fmtTime used by the review console) carry
    no per-value "UTC"/"UTC+8"/"北京时间" suffix — the timezone is communicated
    exactly once via a header/label, not repeated on every row.

Run (from backend/):
    pytest tests/test_admin_timestamp_formatting.py -v
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.main import _fmt_msgtime
from tests._rnd216_web_shims import review_console_html, review_console_js_source

_REVIEW_CONSOLE_HTML = review_console_html()
_REVIEW_CONSOLE_JS = review_console_js_source()


# ---------------------------------------------------------------------------
# _fmt_msgtime — pure formatter tests (no DB, no network)
# ---------------------------------------------------------------------------


def _epoch_ms(y, m, d, hh, mm, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc).timestamp() * 1000)


def test_fmt_msgtime_converts_utc_to_beijing() -> None:
    """2026-07-05T10:00:00Z (UTC) must display as 2026-07-05 18:00:00 (UTC+8)."""
    ms = _epoch_ms(2026, 7, 5, 10, 0, 0)
    assert _fmt_msgtime(ms) == "2026-07-05 18:00:00"


def test_fmt_msgtime_day_boundary_rolls_forward() -> None:
    """2026-07-04T16:30:00Z must roll into the next Beijing calendar day."""
    ms = _epoch_ms(2026, 7, 4, 16, 30, 0)
    assert _fmt_msgtime(ms) == "2026-07-05 00:30:00"


def test_fmt_msgtime_none_returns_empty_string() -> None:
    assert _fmt_msgtime(None) == ""


def test_fmt_msgtime_invalid_input_falls_back_without_raising() -> None:
    """A value that can't be treated as epoch-ms must not raise; falls back to str()."""
    result = _fmt_msgtime("not-a-number")  # type: ignore[arg-type]
    assert result == "not-a-number"


def test_fmt_msgtime_has_no_stray_utc_label_in_value() -> None:
    """Formatter output itself carries no 'UTC' suffix (label lives in the surrounding UI)."""
    ms = _epoch_ms(2026, 7, 5, 10, 0, 0)
    assert "UTC" not in _fmt_msgtime(ms)
    assert "北京时间" not in _fmt_msgtime(ms)


# ---------------------------------------------------------------------------
# JS fmtTime (review console) — same no-per-value-suffix contract as Python
# ---------------------------------------------------------------------------


def _extract_js_fmt_time_source() -> str:
    match = re.search(r"function fmtTime\(ms\)\{.*?\n\}", _REVIEW_CONSOLE_JS, re.S)
    assert match is not None, "fmtTime() not found in _REVIEW_CONSOLE_JS"
    return match.group(0)


def test_js_fmt_time_return_value_has_no_timezone_suffix() -> None:
    """
    The JS formatter's actual output (its string literals) must not contain a
    timezone suffix. We check quoted string literals only — not the whole
    function source — so this doesn't false-positive on code comments or on
    legitimate identifiers like getUTCHours().
    """
    src = _extract_js_fmt_time_source()
    string_literals = re.findall(r"'([^']*)'", src)
    assert string_literals, "expected at least the date-part separators as string literals"
    for literal in string_literals:
        assert "UTC" not in literal, f"fmtTime() output literal {literal!r} must not contain 'UTC'"
        assert "北京时间" not in literal, f"fmtTime() output literal {literal!r} must not contain '北京时间'"


def test_js_fmt_time_source_has_no_utc_suffix_concatenation() -> None:
    """Guard against re-introducing a trailing +' UTC+8' (or similar) concatenation."""
    src = _extract_js_fmt_time_source()
    assert "+'UTC" not in src.replace(" ", "")
    assert "+' UTC" not in src


def test_review_console_has_exactly_one_timezone_label() -> None:
    """
    The review console must state the timezone exactly once (a page-level
    note), not repeat it on every row via fmtTime()'s return value.
    """
    assert _REVIEW_CONSOLE_HTML.count('class="tz-note"') == 1
    assert "Beijing time (UTC+8)" in _REVIEW_CONSOLE_HTML


# ---------------------------------------------------------------------------
# HTML rendering — Beijing time appears on admin pages; ordering untouched
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app

    app.dependency_overrides.clear()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _make_mock_msg(msgid: str, msgtime: int, tenant_id: str = "tenant-a") -> MagicMock:
    msg = MagicMock()
    msg.msgid = msgid
    msg.id = 1
    msg.seq = 1
    msg.msgtype = "text"
    msg.sender = "staff_alice"
    msg.roomid = None
    msg.msgtime = msgtime
    msg.content_text = "hello"
    msg.decrypt_status = "success"
    msg.tenant_id = tenant_id
    return msg


def _db_with_session_and_messages(tenant_id: str, messages):
    """get_db override: valid session for tenant_id + a captured ArchiveMessage query."""
    messages = list(messages)
    order_by_calls = []

    def _override():
        mock = MagicMock()

        session_mock = MagicMock()
        session_mock.tenant_id = tenant_id

        session_q = MagicMock()
        session_q.filter.return_value = session_q
        session_q.first.return_value = session_mock

        msg_q = MagicMock()
        msg_q.filter.return_value = msg_q

        def _order_by(*args, **kwargs):
            order_by_calls.append(args)
            return msg_q

        msg_q.order_by.side_effect = _order_by
        msg_q.limit.return_value = msg_q
        msg_q.all.return_value = messages
        msg_q.first.return_value = messages[0] if messages else None

        rcpt_q = MagicMock()
        rcpt_q.filter.return_value = rcpt_q
        rcpt_q.all.return_value = []

        def _query(model):
            from app.db.models import AdminSession, ArchiveMessageRecipient

            if model is AdminSession:
                return session_q
            if model is ArchiveMessageRecipient:
                return rcpt_q
            return msg_q

        mock.query.side_effect = _query
        yield mock

    _override.order_by_calls = order_by_calls
    return _override


def test_admin_messages_list_shows_beijing_time(client) -> None:
    from app.db.session import get_db
    from app.main import app

    ms = _epoch_ms(2026, 7, 5, 10, 0, 0)
    msg = _make_mock_msg("msg-001", ms)
    override = _db_with_session_and_messages("tenant-a", [msg])
    app.dependency_overrides[get_db] = override
    client.cookies.set("session_id", "valid-tenant-a-session")

    resp = client.get("/admin/messages")
    assert resp.status_code == 200
    body = resp.content
    assert b"2026-07-05 18:00:00" in body
    # Timezone communicated exactly once, via the column header — not repeated per row.
    assert body.count(b"time (UTC+8)") == 1
    assert b"2026-07-05 18:00:00 UTC" not in body
    assert b"2026-07-05 18:00:00UTC+8" not in body


def test_admin_message_detail_shows_beijing_time(client) -> None:
    from app.db.session import get_db
    from app.main import app

    ms = _epoch_ms(2026, 7, 4, 16, 30, 0)
    msg = _make_mock_msg("msg-002", ms)
    override = _db_with_session_and_messages("tenant-a", [msg])
    app.dependency_overrides[get_db] = override
    client.cookies.set("session_id", "valid-tenant-a-session")

    resp = client.get("/admin/messages/msg-002")
    assert resp.status_code == 200
    body = resp.content
    assert b"2026-07-05 00:30:00" in body
    assert body.count(b"msgtime (UTC+8)") == 1
    assert b"2026-07-05 00:30:00 UTC" not in body


def test_admin_messages_ordering_unaffected_by_display_formatting(client) -> None:
    """
    RND-149 is display-only: the DB query must still order by ArchiveMessage.msgtime
    desc, and the raw (unconverted) msgtime is what's used for that ordering.
    """
    from app.db.models import ArchiveMessage
    from app.db.session import get_db
    from app.main import app

    msgs = [
        _make_mock_msg("msg-newer", _epoch_ms(2026, 7, 5, 10, 0, 0)),
        _make_mock_msg("msg-older", _epoch_ms(2026, 7, 4, 10, 0, 0)),
    ]
    override = _db_with_session_and_messages("tenant-a", msgs)
    app.dependency_overrides[get_db] = override
    client.cookies.set("session_id", "valid-tenant-a-session")

    resp = client.get("/admin/messages")
    assert resp.status_code == 200

    assert len(override.order_by_calls) == 1
    (order_expr,) = override.order_by_calls[0]
    assert str(order_expr) == str(ArchiveMessage.msgtime.desc())

    newer_pos = resp.content.index(b"msg-newer")
    older_pos = resp.content.index(b"msg-older")
    assert newer_pos < older_pos, "row order must still follow raw msgtime desc, unchanged by display formatting"
