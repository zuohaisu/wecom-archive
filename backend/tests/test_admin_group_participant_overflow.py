"""
Tests for RND-150 — fix group chat participant overflow in admin timeline.

Root cause: the review console's renderTimeline() (embedded JS in
_REVIEW_CONSOLE_HTML) rendered every message's recipient list inline as a
raw, comma-joined string — `rcptNames.join(', ')`. For a direct message this
is one name, but for a group message `recipients` is the full per-message
`tolist` fan-out (one row per room member), so the timeline printed a
massive raw-ID list under every group message. When display-name
resolution has no synced contact name it falls back to the raw WeCom
userid (see resolve_person_display_name), so this could also leak raw
internal identifiers into a normal admin view.

Fix: for messages with a roomid (group chat), render a compact muted
summary — "Group chat · N participants" — instead of the joined list.
Direct-message rendering (single "→ name" line) is unchanged.

These tests execute the actual embedded JS (extracted from
_REVIEW_CONSOLE_HTML by source pattern, same technique used in
test_admin_timestamp_formatting.py) under Node, with a minimal DOM/global
stub, so the assertions exercise real rendering behavior rather than only
pattern-matching the source text.

Run (from backend/):
    pytest tests/test_admin_group_participant_overflow.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from app.main import _REVIEW_CONSOLE_HTML

NODE = shutil.which("node")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_HTML, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_HTML"
    return match.group(0)


def _extract_render_timeline_bundle() -> str:
    """Pull out every JS symbol renderTimeline() transitively depends on."""
    # RND-157: renderTimeline()/renderMessageBody() now render their labels
    # via I18N.t(...) rather than hardcoded English. Prepend the i18n core
    # and pin locale to English so this file's literal-English assertions
    # keep working — the app's default locale is zh-CN, which would
    # otherwise make these functions render Chinese text.
    i18n_core_src = _extract(
        r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"
    )
    force_en = "I18N.setLocale('en');"
    esc_src = _extract(r"function esc\(s\)\{.*?\n\}", "esc()")
    fmt_time_src = _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()")
    pad_src = _extract(r"function pad\(n\)\{.*?\}", "pad()")
    media_labels_src = _extract(r"var MEDIA_LABELS=\{.*?\};", "MEDIA_LABELS")
    media_status_labels_src = _extract(
        r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"
    )
    # RND-173: renderMessageBody() now resolves unsupported/placeholder types
    # through MessageTypeRegistry instead of an inline generic string.
    message_type_registry_src = _extract(
        r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"
    )
    # RND-206: renderMessageBody()/renderTimeline() now also depend on the
    # MediaAccessCache/Viewer/rich-media/composite renderer block — pull the
    # whole contiguous block in so the bundle is self-contained (none of
    # these functions actually execute for text/image/unsupported messages,
    # but they must exist to be referenced).
    rnd206_block_src = _extract(
        r"var MediaAccessCache=\(function\(\)\{.*?\nfunction renderCompositeMessage\(m\)\{.*?\n\}",
        "RND-206 rich-media/composite block",
    )
    render_message_body_src = _extract(
        r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()"
    )
    safe_render_message_body_src = _extract(
        r"function safeRenderMessageBody\(m\)\{.*?\n\}", "safeRenderMessageBody()"
    )
    timeline_signature_src = _extract(
        r"function timelineSignature\(msgs\)\{.*?\n\}", "timelineSignature()"
    )
    timeline_row_html_src = _extract(
        r"function timelineRowHtml\(m\)\{.*?\n\}", "timelineRowHtml()"
    )
    render_timeline_src = _extract(
        r"function renderTimeline\(scrollToBottom\)\{.*?\n\}", "renderTimeline()"
    )
    return "\n".join(
        [
            i18n_core_src,
            force_en,
            esc_src,
            fmt_time_src,
            pad_src,
            media_labels_src,
            media_status_labels_src,
            message_type_registry_src,
            rnd206_block_src,
            render_message_body_src,
            safe_render_message_body_src,
            timeline_signature_src,
            timeline_row_html_src,
            render_timeline_src,
        ]
    )


def _run_render_timeline(messages: list[dict], mode: str = "staff") -> str:
    """Execute the real renderTimeline() under Node against `messages` and
    return the resulting innerHTML of #timeline-body."""
    assert NODE, "node executable not found"
    bundle = _extract_render_timeline_bundle()
    harness = f"""
{bundle}

var mode = {json.dumps(mode)};
var selEntityId = null;
var timelineMsgs = {json.dumps(messages)};
var timelineHasOlder = false;

var capturedHtml = null;
var timelineBodyEl = {{
  get innerHTML() {{ return capturedHtml; }},
  set innerHTML(v) {{ capturedHtml = v; }},
  scrollHeight: 0,
  scrollTop: 0,
  querySelectorAll: function() {{ return []; }},
}};
var document = {{
  getElementById: function(id) {{
    if (id === 'timeline-body') return timelineBodyEl;
    throw new Error('unexpected getElementById(' + id + ')');
  }}
}};

renderTimeline(false);
process.stdout.write(capturedHtml);
"""
    result = subprocess.run(
        [NODE, "-e", harness],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


# ---------------------------------------------------------------------------
# Group chat: long raw participant list must not render inline
# ---------------------------------------------------------------------------


def _group_message_with_many_participants(n: int) -> dict:
    # Simulate un-synced contacts: recipient_display_names falls back to the
    # raw userid (resolve_person_display_name's documented fallback), which
    # is the worst case for raw-ID leakage into the rendered view.
    raw_ids = [f"contact_raw_id_{i:04d}" for i in range(n)]
    return {
        "msgid": "msg-group-1",
        "sender": "staff_alice",
        "sender_display_name": "Alice",
        "sender_raw_id": "staff_alice",
        "recipients": raw_ids,
        "recipient_display_names": raw_ids,
        "recipient_raw_ids": raw_ids,
        "msgtime": 1751702400000,
        "msgtype": "text",
        "content_text": "let's ship the release today",
        "roomid": "room_after_sales_001",
        "decrypt_status": "success",
        "media_type": "text",
        "media_status": None,
        "unsupported_reason": None,
        "media_url": None,
    }


def test_group_message_does_not_render_raw_participant_ids_inline() -> None:
    msg = _group_message_with_many_participants(300)
    html = _run_render_timeline([msg])
    for raw_id in msg["recipients"]:
        assert raw_id not in html, f"raw participant id {raw_id!r} leaked into rendered timeline"


def test_group_message_shows_compact_participant_count() -> None:
    msg = _group_message_with_many_participants(300)
    html = _run_render_timeline([msg])
    assert "Group chat · 300 participants" in html


def test_group_message_singular_participant_count() -> None:
    msg = _group_message_with_many_participants(1)
    html = _run_render_timeline([msg])
    assert "Group chat · 1 participant" in html
    assert "1 participants" not in html


def test_group_message_still_renders_actual_text_content() -> None:
    msg = _group_message_with_many_participants(300)
    html = _run_render_timeline([msg])
    assert "let&#39;s ship the release today" in html or "let's ship the release today" in html


def test_group_message_still_renders_group_badge() -> None:
    msg = _group_message_with_many_participants(50)
    html = _run_render_timeline([msg])
    assert 'class="badge badge-group"' in html
    assert ">group<" in html


def test_group_message_still_renders_sender_and_timestamp() -> None:
    msg = _group_message_with_many_participants(50)
    html = _run_render_timeline([msg])
    assert "Alice" in html
    assert 'class="tl-time"' in html


def test_group_message_with_no_recipients_renders_no_participant_line() -> None:
    msg = _group_message_with_many_participants(0)
    html = _run_render_timeline([msg])
    assert "Group chat ·" not in html
    assert "tl-rcpt" not in html


# ---------------------------------------------------------------------------
# Direct messages: rendering unchanged
# ---------------------------------------------------------------------------


def _direct_message() -> dict:
    return {
        "msgid": "msg-direct-1",
        "sender": "staff_alice",
        "sender_display_name": "Alice",
        "sender_raw_id": "staff_alice",
        "recipients": ["contact_zhangsan"],
        "recipient_display_names": ["Zhang San"],
        "recipient_raw_ids": ["contact_zhangsan"],
        "msgtime": 1751702400000,
        "msgtype": "text",
        "content_text": "hello there",
        "roomid": None,
        "decrypt_status": "success",
        "media_type": "text",
        "media_status": None,
        "unsupported_reason": None,
        "media_url": None,
    }


def test_direct_message_still_shows_recipient_name_line() -> None:
    html = _run_render_timeline([_direct_message()])
    assert "→ Zhang San" in html


def test_direct_message_has_no_group_badge_or_participant_summary() -> None:
    html = _run_render_timeline([_direct_message()])
    assert "badge-group" not in html
    assert "Group chat ·" not in html


# ---------------------------------------------------------------------------
# Media / unsupported placeholders unaffected by this change
# ---------------------------------------------------------------------------


def test_unsupported_media_placeholder_unchanged() -> None:
    """RND-177 updated the generic fallback copy from "Unsupported message
    type" to "Unknown message type" — this test still only asserts that
    group-participant-overflow work (RND-150) doesn't affect which
    placeholder branch renders, not the exact fallback wording."""
    msg = _direct_message()
    msg["media_type"] = "unsupported"
    msg["content_text"] = None
    html = _run_render_timeline([msg])
    assert '<div class="media-placeholder">Unknown message type</div>' in html


def test_media_not_downloaded_placeholder_unchanged() -> None:
    msg = _direct_message()
    msg["media_type"] = "image"
    msg["media_status"] = "not_downloaded"
    msg["content_text"] = None
    html = _run_render_timeline([msg])
    assert "Image message" in html and "not downloaded" in html


# ---------------------------------------------------------------------------
# Structural guard: the group branch must never join the raw recipient list
# ---------------------------------------------------------------------------


def test_source_group_branch_never_joins_raw_recipients() -> None:
    """
    Regression guard: if someone reintroduces `rcptNames.join(...)` inside
    the `if(m.roomid){...}` branch, this must fail even before Node executes
    it, since that's exactly the overflow/leak bug RND-150 fixes.
    """
    # RND-204: the per-row markup (incl. the group recipient branch) now
    # lives in timelineRowHtml(), which renderTimeline() delegates to.
    timeline_row_html_src = _extract(
        r"function timelineRowHtml\(m\)\{.*?\n\}", "timelineRowHtml()"
    )
    match = re.search(r"if\(m\.roomid\)\{(.*?)\}else if", timeline_row_html_src, re.S)
    assert match is not None, "expected an if(m.roomid){...}else if(...) branch in timelineRowHtml()"
    group_branch_src = match.group(1)
    assert "join(" not in group_branch_src
