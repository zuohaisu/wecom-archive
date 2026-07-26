"""
Tests for RND-153 — auto-refresh and refresh status in the admin conversation UI.

Scope: `/admin/conversations` (the review console, embedded JS in
_REVIEW_CONSOLE_HTML) only. Frontend polling against existing APIs — no
websocket/SSE, no backend route changes, no auth changes, no schema changes.

Covers:
  - Static presence of the refresh status label, countdown, and manual
    refresh button in the rendered HTML.
  - Absence of any websocket/SSE usage (guard against scope creep).
  - Real execution (under Node) of mergeMessagesByMsgid() — msgid-based
    dedupe + msgtime ordering — and isNearBottom() — the scroll guard that
    decides whether auto-refresh may jump the viewport.
  - renderConvList() still re-applies the .active class for the selected
    conversation after a full re-render (needed so a periodic refresh does
    not visually lose the current selection).
  - RND-149 (Beijing time) and RND-150 (group participant overflow) contracts
    are undisturbed by this change.

Run (from backend/):
    pytest tests/test_admin_auto_refresh.py -v
"""

from __future__ import annotations

import json
import re
import shutil

import pytest

from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_html, review_console_js_source

_REVIEW_CONSOLE_HTML = review_console_html()
_REVIEW_CONSOLE_JS = review_console_js_source()

NODE = shutil.which("node")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_JS, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_JS"
    return match.group(0)


# ---------------------------------------------------------------------------
# Static HTML/JS presence checks (no Node required)
# ---------------------------------------------------------------------------


def test_html_has_refresh_status_element() -> None:
    assert 'id="refresh-status"' in _REVIEW_CONSOLE_HTML


def test_html_has_manual_refresh_button() -> None:
    assert 'id="btn-refresh"' in _REVIEW_CONSOLE_HTML
    assert "refreshNow('manual')" in _REVIEW_CONSOLE_HTML
    assert ">刷新<" in _REVIEW_CONSOLE_HTML


def test_html_has_new_message_indicator() -> None:
    assert 'id="new-msg-indicator"' in _REVIEW_CONSOLE_HTML
    assert "有新消息" in _REVIEW_CONSOLE_HTML
    assert "scrollTimelineToBottom()" in _REVIEW_CONSOLE_HTML


def test_js_has_countdown_and_last_refresh_labels() -> None:
    assert "最近更新" in _REVIEW_CONSOLE_JS
    assert "下次刷新" in _REVIEW_CONSOLE_JS
    assert "秒后" in _REVIEW_CONSOLE_JS


def test_js_has_polling_interval_and_countdown_logic() -> None:
    assert "REFRESH_INTERVAL_SEC=30" in _REVIEW_CONSOLE_JS
    assert "setInterval(tickRefreshCountdown,1000)" in _REVIEW_CONSOLE_JS
    assert "function scheduleNextRefresh()" in _REVIEW_CONSOLE_JS
    assert "function updateRefreshStatus()" in _REVIEW_CONSOLE_JS
    assert "function refreshNow(reason)" in _REVIEW_CONSOLE_JS


def test_js_has_near_bottom_guard() -> None:
    assert "function isNearBottom()" in _REVIEW_CONSOLE_JS


def test_js_has_msgid_based_merge_dedupe() -> None:
    assert "function mergeMessagesByMsgid(existing,incoming)" in _REVIEW_CONSOLE_JS
    assert "m.msgid" in _extract(
        r"function mergeMessagesByMsgid\(existing,incoming\)\{.*?\n\}", "mergeMessagesByMsgid()"
    )


def test_js_pauses_polling_when_tab_hidden_and_resumes_when_visible() -> None:
    assert "visibilitychange" in _REVIEW_CONSOLE_JS
    assert "document.hidden" in _REVIEW_CONSOLE_JS
    assert "refreshNow('visibility')" in _REVIEW_CONSOLE_JS


def test_no_websocket_or_sse_usage() -> None:
    assert "WebSocket" not in _REVIEW_CONSOLE_JS
    assert "EventSource" not in _REVIEW_CONSOLE_JS
    assert "text/event-stream" not in _REVIEW_CONSOLE_JS


def test_refresh_does_not_touch_load_older_pagination_state() -> None:
    """
    refreshTimelineIfSelected() must never assign timelineHasOlder or
    timelineNextBefore — those belong exclusively to loadTimeline() /
    fetchTimelinePage() / loadOlderMessages(), so "Load older" pagination
    cursor semantics stay untouched by auto-refresh.
    """
    src = _extract(
        r"function refreshTimelineIfSelected\(\)\{.*?\n\}", "refreshTimelineIfSelected()"
    )
    assert "timelineHasOlder=" not in src
    assert "timelineNextBefore=" not in src


# ---------------------------------------------------------------------------
# Node-executed behavior: mergeMessagesByMsgid()
# ---------------------------------------------------------------------------


def _run_merge(existing: list[dict], incoming: list[dict]) -> list[dict]:
    assert NODE, "node executable not found"
    src = _extract(
        r"function mergeMessagesByMsgid\(existing,incoming\)\{.*?\n\}", "mergeMessagesByMsgid()"
    )
    harness = f"""
{src}
process.stdout.write(JSON.stringify(mergeMessagesByMsgid({json.dumps(existing)}, {json.dumps(incoming)})));
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def test_merge_dedupes_by_msgid() -> None:
    existing = [{"msgid": "m1", "msgtime": 100}, {"msgid": "m2", "msgtime": 200}]
    incoming = [{"msgid": "m2", "msgtime": 200}]
    merged = _run_merge(existing, incoming)
    assert [m["msgid"] for m in merged] == ["m1", "m2"]


def test_merge_appends_new_messages_and_sorts_by_msgtime() -> None:
    existing = [{"msgid": "m1", "msgtime": 100}]
    incoming = [{"msgid": "m1", "msgtime": 100}, {"msgid": "m3", "msgtime": 300}, {"msgid": "m2", "msgtime": 200}]
    merged = _run_merge(existing, incoming)
    assert [m["msgid"] for m in merged] == ["m1", "m2", "m3"]


def test_merge_keeps_older_loaded_messages_not_present_in_latest_page() -> None:
    """A refresh only re-fetches the latest page; anything loaded earlier via
    'Load older' must survive the merge even though it isn't in `incoming`."""
    existing = [{"msgid": "old-1", "msgtime": 10}, {"msgid": "old-2", "msgtime": 20}]
    incoming = [{"msgid": "new-1", "msgtime": 500}]
    merged = _run_merge(existing, incoming)
    assert {m["msgid"] for m in merged} == {"old-1", "old-2", "new-1"}


def test_merge_incoming_replaces_existing_entry_with_same_msgid() -> None:
    existing = [{"msgid": "m1", "msgtime": 100, "content_text": "old"}]
    incoming = [{"msgid": "m1", "msgtime": 100, "content_text": "new"}]
    merged = _run_merge(existing, incoming)
    assert len(merged) == 1
    assert merged[0]["content_text"] == "new"


def test_merge_produces_no_duplicate_msgids_after_repeated_refresh() -> None:
    state: list[dict] = []
    for page in (
        [{"msgid": "a", "msgtime": 1}, {"msgid": "b", "msgtime": 2}],
        [{"msgid": "b", "msgtime": 2}, {"msgid": "c", "msgtime": 3}],
        [{"msgid": "c", "msgtime": 3}, {"msgid": "d", "msgtime": 4}],
    ):
        state = _run_merge(state, page)
    ids = [m["msgid"] for m in state]
    assert ids == ["a", "b", "c", "d"]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Node-executed behavior: isNearBottom()
# ---------------------------------------------------------------------------


def _run_is_near_bottom(scroll_height: int, scroll_top: int, client_height: int) -> bool:
    assert NODE, "node executable not found"
    src = _extract(r"function isNearBottom\(\)\{.*?\n\}", "isNearBottom()")
    harness = f"""
{src}
var document = {{
  getElementById: function(id) {{
    if (id === 'timeline-body') return {{
      scrollHeight: {scroll_height},
      scrollTop: {scroll_top},
      clientHeight: {client_height}
    }};
    throw new Error('unexpected getElementById(' + id + ')');
  }}
}};
process.stdout.write(JSON.stringify(isNearBottom()));
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def test_is_near_bottom_true_when_scrolled_to_bottom() -> None:
    assert _run_is_near_bottom(scroll_height=1000, scroll_top=920, client_height=80) is True


def test_is_near_bottom_false_when_scrolled_up() -> None:
    assert _run_is_near_bottom(scroll_height=1000, scroll_top=200, client_height=80) is False


# ---------------------------------------------------------------------------
# Node-executed behavior: renderConvList() preserves the active selection
# ---------------------------------------------------------------------------


def _run_render_conv_list_active(convs: list[dict], sel_conv_id: str) -> list[str]:
    assert NODE, "node executable not found"
    # RND-157: renderConvList() now renders its direct/group badge and message
    # count via I18N.t(...) instead of hardcoded English. Bring in the i18n
    # core so I18N is defined (this test only checks .active class toggling,
    # not label text, so the locale choice itself doesn't matter here).
    i18n_core_src = _extract(
        r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"
    )
    esc_src = _extract(r"function esc\(s\)\{.*?\n\}", "esc()")
    fmt_time_src = _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()")
    pad_src = _extract(r"function pad\(n\)\{.*?\}", "pad()")
    # Archive Console v2 (design import): renderConvList() now filters
    # through applyConvTypeFilter() (client-side 全部/群聊/单聊 tabs) before
    # rendering.
    apply_conv_type_filter_src = _extract(
        r"function applyConvTypeFilter\(convs\)\{.*?\n\}", "applyConvTypeFilter()"
    )
    render_conv_list_src = _extract(r"function renderConvList\(convs\)\{.*?\n\}", "renderConvList()")
    # RND-204: renderConvList() now records a content signature (used to skip
    # unchanged background refreshes), so pull convListSignature() in too.
    conv_list_signature_src = _extract(
        r"function convListSignature\(convs\)\{.*?\n\}", "convListSignature()"
    )
    harness = f"""
{i18n_core_src}
{esc_src}
{fmt_time_src}
{pad_src}
{conv_list_signature_src}
{apply_conv_type_filter_src}
{render_conv_list_src}

var mode = 'staff';
var selConvId = {json.dumps(sel_conv_id)};
var convTypeFilter = 'all';
var cardEls = {{}};

var bodyEl = {{
  get innerHTML() {{ return this._html; }},
  set innerHTML(v) {{
    this._html = v;
    // Recreate one fake card element per conv id found in the generated html,
    // mirroring how the real DOM would parse data-id attributes.
    var re = /data-id="([^"]*)"/g;
    var m;
    cardEls = {{}};
    while ((m = re.exec(v)) !== null) {{
      cardEls[m[1]] = {{ id: m[1], active: false }};
    }}
  }},
  querySelectorAll: function(sel) {{
    return Object.keys(cardEls).map(function(k) {{ return cardEls[k]; }}).map(function(card) {{
      card.dataset = {{ id: card.id }};
      card.classList = {{
        remove: function() {{ card.active = false; }},
        toggle: function(cls, on) {{ if (cls === 'active') card.active = on; }},
        add: function() {{ card.active = true; }}
      }};
      return card;
    }});
  }}
}};
var document = {{
  getElementById: function(id) {{
    if (id === 'conv-body') return bodyEl;
    throw new Error('unexpected getElementById(' + id + ')');
  }}
}};

renderConvList({json.dumps(convs)});
var activeAfterRender = Object.keys(cardEls).filter(function(k) {{ return cardEls[k].active; }});
process.stdout.write(JSON.stringify(activeAfterRender));
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def _conv(conv_id: str) -> dict:
    return {
        "conversation_id": conv_id,
        "conversation_type": "direct",
        "display_name": "Zhang San",
        "raw_id": "contact_zhangsan",
        "last_message_text": "hi",
        "last_message_time": 1751702400000,
        "message_count": 3,
        "monitored_account_ids": ["staff_alice"],
        "monitored_account_display_names": ["Alice"],
    }


def test_render_conv_list_reapplies_active_class_for_selected_conversation() -> None:
    convs = [_conv("direct__a___b"), _conv("direct__c___d")]
    convs[1]["conversation_id"] = "direct__c___d"
    active = _run_render_conv_list_active(convs, "direct__c___d")
    assert active == ["direct__c___d"]


def test_render_conv_list_no_active_class_when_selection_not_present() -> None:
    convs = [_conv("direct__a___b")]
    active = _run_render_conv_list_active(convs, "direct__does-not-exist")
    assert active == []


# ---------------------------------------------------------------------------
# Prior contracts (RND-149 / RND-150) must remain intact
# ---------------------------------------------------------------------------


def test_review_console_still_has_exactly_one_timezone_label() -> None:
    assert _REVIEW_CONSOLE_HTML.count('class="tz-note"') == 1
    assert "Beijing time (UTC+8)" in _REVIEW_CONSOLE_JS


def test_render_timeline_group_branch_still_never_joins_raw_recipients() -> None:
    # RND-204: the per-row group recipient branch now lives in
    # timelineRowHtml(), which renderTimeline() delegates to.
    timeline_row_html_src = _extract(
        r"function timelineRowHtml\(m\)\{.*?\n\}", "timelineRowHtml()"
    )
    match = re.search(r"if\(m\.roomid\)\{(.*?)\}else if", timeline_row_html_src, re.S)
    assert match is not None
    assert "join(" not in match.group(1)
