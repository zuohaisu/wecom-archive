"""
Tests for RND-152 — automatically load older chat history on scroll-to-top.

Scope: `/admin/conversations` (the review console, embedded JS in
_REVIEW_CONSOLE_HTML) only. Replaces the manual "Load older" button with an
IntersectionObserver-driven auto-load, reusing the existing cursor-based
pagination API. No backend, schema, auth, or pagination-contract changes.

These tests execute the real embedded JS under Node (same technique as
test_admin_auto_refresh.py and test_admin_group_participant_overflow.py),
with a minimal DOM/fetch/IntersectionObserver stub, so assertions exercise
real behavior rather than only pattern-matching source text.

Covers:
  - observer initialization (root/sentinel wiring, disconnect on stop)
  - the loadingOlder guard (no request while already loading / no more
    history / a retry-able error is pending)
  - duplicate-request prevention when the observer fires more than once
  - scroll-position preservation math (preserveScrollPosition)
  - end-of-history state once pagination reports has_older=false
  - retry state on fetch failure, and that Retry reuses the same pipeline
  - no duplicate/lost messages across an older-page prepend
  - coexistence with auto-refresh (refreshTimelineIfSelected must not touch
    the timeline while an older-history load is in flight)
  - QA blocker 1: a pending old-history error/retry state survives a full
    renderTimeline() rebuild (e.g. from a RND-153 refresh) instead of being
    silently wiped while still blocking loadOlderAutomatically()
  - QA blocker 2: an in-flight older-history request for conversation A is
    ignored if the user switches to conversation B before it resolves —
    no stale messages/cursor/hasMore/error/scroll applied to B, and the
    loading guard is not left stuck

Run (from backend/):
    pytest tests/test_admin_auto_load_older.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from app.main import _REVIEW_CONSOLE_HTML

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_HTML, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_HTML"
    return match.group(0)


# ---------------------------------------------------------------------------
# Static presence / structural checks (no Node required beyond skip guard)
# ---------------------------------------------------------------------------


def test_manual_load_older_button_is_gone() -> None:
    assert "load-older-btn" not in _REVIEW_CONSOLE_HTML
    assert "function loadOlderMessages(" not in _REVIEW_CONSOLE_HTML


def test_sentinel_and_history_status_elements_present() -> None:
    assert 'id="timeline-top-sentinel"' in _REVIEW_CONSOLE_HTML
    assert 'id="timeline-history-status"' in _REVIEW_CONSOLE_HTML


def test_new_helper_functions_exist() -> None:
    for fn in (
        "function isNearTop()",
        "function preserveScrollPosition(body,beforeHeight)",
        "function startHistoryObserver()",
        "function stopHistoryObserver()",
        "function loadOlderAutomatically()",
        "function retryLoadOlder()",
        "function showLoadingOlder()",
        "function showEndOfHistory()",
        "function showHistoryRetry()",
        "function historyRetryHtml()",
        "function fetchOlderMessages(convId,before)",
    ):
        assert fn in _REVIEW_CONSOLE_HTML, f"missing {fn}"


def test_uses_intersection_observer() -> None:
    assert "new IntersectionObserver(" in _REVIEW_CONSOLE_HTML


# ---------------------------------------------------------------------------
# Bundle assembly for Node execution
# ---------------------------------------------------------------------------


def _bundle() -> str:
    # RND-157: renderMessageBody/renderTimeline/history status helpers now go
    # through I18N.t(...) instead of hardcoded English strings. Pull in the
    # i18n core (Locale Registry + helpers) and pin the locale to English so
    # this file's pre-existing literal-English assertions keep meaning what
    # they said before i18n existed — the default locale is zh-CN, so without
    # this the same functions would render Chinese text instead.
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('en');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\}", "pad()"),
        _extract(r"function handleUnauth\(r\)\{.*?\n\}", "handleUnauth()"),
        _extract(r"var MEDIA_LABELS=\{.*?\};", "MEDIA_LABELS"),
        _extract(r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"),
        # RND-206: fetchOlderMessages/fetchTimelinePage/loadTimeline/
        # refreshTimelineIfSelected all read this generation-token guard now.
        _extract(r"var timelineRequestGen=0;", "timelineRequestGen"),
        # RND-173: renderMessageBody() now resolves unsupported/placeholder
        # types through MessageTypeRegistry instead of an inline generic string.
        _extract(
            r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"
        ),
        # RND-206: renderMessageBody()/renderTimeline() now also depend on
        # the MediaAccessCache/Viewer/rich-media/composite renderer block —
        # pull the whole contiguous block in so the bundle is self-contained
        # (none of these functions actually execute for text/image/
        # unsupported messages, but they must exist to be referenced).
        _extract(
            r"var MediaAccessCache=\(function\(\)\{.*?\nfunction renderCompositeMessage\(m\)\{.*?\n\}",
            "RND-206 rich-media/composite block",
        ),
        _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()"),
        _extract(r"function safeRenderMessageBody\(m\)\{.*?\n\}", "safeRenderMessageBody()"),
        _extract(r"function timelineSignature\(msgs\)\{.*?\n\}", "timelineSignature()"),
        _extract(r"function renderTimeline\(scrollToBottom\)\{.*?\n\}", "renderTimeline()"),
        _extract(r"function isNearTop\(\)\{.*?\n\}", "isNearTop()"),
        _extract(
            r"function preserveScrollPosition\(body,beforeHeight\)\{.*?\n\}",
            "preserveScrollPosition()",
        ),
        _extract(r"function historyStatusEl\(\)\{.*?\}", "historyStatusEl()"),
        _extract(r"function showLoadingOlder\(\)\{.*?\n\}", "showLoadingOlder()"),
        _extract(r"function showEndOfHistory\(\)\{.*?\n\}", "showEndOfHistory()"),
        _extract(r"function historyRetryHtml\(\)\{.*?\n\}", "historyRetryHtml()"),
        _extract(r"function showHistoryRetry\(\)\{.*?\n\}", "showHistoryRetry()"),
        _extract(r"function fetchOlderMessages\(convId,before\)\{.*?\n\}", "fetchOlderMessages()"),
        _extract(r"function loadOlderAutomatically\(\)\{.*?\n\}", "loadOlderAutomatically()"),
        _extract(r"function retryLoadOlder\(\)\{.*?\n\}", "retryLoadOlder()"),
        _extract(r"function startHistoryObserver\(\)\{.*?\n\}", "startHistoryObserver()"),
        _extract(r"function stopHistoryObserver\(\)\{.*?\n\}", "stopHistoryObserver()"),
    ]
    return "\n".join(parts)


def _run(script_body: str) -> dict:
    """Run `script_body` (an async IIFE body) after the bundle + fixture
    globals, and return the JSON object it writes to stdout."""
    assert NODE, "node executable not found"
    harness = f"""
{_bundle()}

(async function() {{
{script_body}
}})().catch(function(e) {{
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
}});
"""
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def _fixture_preamble(
    *,
    timeline_has_older: bool = True,
    timeline_msgs: list | None = None,
    timeline_loading_older: bool = False,
    timeline_history_error=None,
    fetch_impl: str = "function(url){ fetchCalls.push(url); return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve({messages:[],pagination:{has_older:false,next_before:null}});}}); }",
) -> str:
    # NOTE: these are deliberately *not* `var`-declared. This preamble is
    # spliced inside the async IIFE in _run(), while the bundled functions
    # (renderTimeline, loadOlderAutomatically, ...) are defined at top-level
    # *outside* that IIFE. A `var` here would be local to the IIFE and
    # invisible to those functions; a bare assignment in Node's sloppy-mode
    # script creates a real global, which both scopes can see.
    return f"""
mode='staff';
selEntityId=null;
window={{location:{{href:''}}}};
timelineConvId='conv-1';
timelineMsgs={json.dumps(timeline_msgs or [])};
timelineHasOlder={json.dumps(timeline_has_older)};
timelineNextBefore='cursor-1';
timelineLoadingOlder={json.dumps(timeline_loading_older)};
timelineHistoryError={json.dumps(timeline_history_error)};
timelineTopObserver=null;
fetchCalls=[];
fetch={fetch_impl};

historyStatusHtml='';
sentinelEl={{}};
timelineBodyEl={{
  _html:'',
  scrollTop:0,
  scrollHeight:100,
  clientHeight:80,
  get innerHTML(){{return this._html;}},
  set innerHTML(v){{
    // A real renderTimeline() rebuild replaces #timeline-history-status's
    // subtree wholesale (it's baked into the new innerHTML string), which
    // clears any transient loading/retry text a prior showLoadingOlder()/
    // showHistoryRetry() call had written directly. Mirror that here so the
    // stub matches real DOM parent/child replacement semantics.
    this._html=v;
    var m=/<div id="timeline-history-status">([\\s\\S]*?)<div id="timeline-top-sentinel">/.exec(v);
    historyStatusHtml=m?m[1]:'';
  }},
  querySelectorAll:function(){{return [];}}
}};
historyStatusElObj={{
  get innerHTML(){{return historyStatusHtml;}},
  set innerHTML(v){{historyStatusHtml=v;}}
}};
indicatorEl={{style:{{display:'none'}}}};
document={{
  getElementById:function(id){{
    if(id==='timeline-body')return timelineBodyEl;
    if(id==='timeline-history-status')return historyStatusElObj;
    if(id==='timeline-top-sentinel')return sentinelEl;
    if(id==='new-msg-indicator')return indicatorEl;
    throw new Error('unexpected getElementById('+id+')');
  }}
}};
function flush(){{ return new Promise(function(res){{ setImmediate(res); }}); }}
"""


def _msg(msgid: str, msgtime: int) -> dict:
    return {
        "msgid": msgid,
        "sender": "staff_alice",
        "sender_display_name": "Alice",
        "sender_raw_id": "staff_alice",
        "recipients": ["contact_zhangsan"],
        "recipient_display_names": ["Zhang San"],
        "recipient_raw_ids": ["contact_zhangsan"],
        "msgtime": msgtime,
        "msgtype": "text",
        "content_text": f"msg {msgid}",
        "roomid": None,
        "decrypt_status": "success",
        "media_type": "text",
        "media_status": None,
        "unsupported_reason": None,
        "media_url": None,
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_is_near_top_true_when_scroll_top_small() -> None:
    out = _run(
        _fixture_preamble()
        + """
timelineBodyEl.scrollTop = 10;
process.stdout.write(JSON.stringify({near: isNearTop()}));
"""
    )
    assert out["near"] is True


def test_is_near_top_false_when_scrolled_away_from_top() -> None:
    out = _run(
        _fixture_preamble()
        + """
timelineBodyEl.scrollTop = 500;
process.stdout.write(JSON.stringify({near: isNearTop()}));
"""
    )
    assert out["near"] is False


def test_preserve_scroll_position_applies_height_delta() -> None:
    out = _run(
        _fixture_preamble()
        + """
var body = {scrollTop: 20, scrollHeight: 500};
preserveScrollPosition(body, 300);
process.stdout.write(JSON.stringify({scrollTop: body.scrollTop}));
"""
    )
    # delta = 500 - 300 = 200; scrollTop 20 + 200 = 220
    assert out["scrollTop"] == 220


# ---------------------------------------------------------------------------
# Loading guard / duplicate request prevention
# ---------------------------------------------------------------------------


def test_load_older_automatically_noop_when_already_loading() -> None:
    out = _run(
        _fixture_preamble(timeline_loading_older=True)
        + """
loadOlderAutomatically();
process.stdout.write(JSON.stringify({fetchCalls: fetchCalls.length}));
"""
    )
    assert out["fetchCalls"] == 0


def test_load_older_automatically_noop_when_no_more_history() -> None:
    out = _run(
        _fixture_preamble(timeline_has_older=False)
        + """
loadOlderAutomatically();
process.stdout.write(JSON.stringify({fetchCalls: fetchCalls.length}));
"""
    )
    assert out["fetchCalls"] == 0


def test_load_older_automatically_noop_when_retry_error_pending() -> None:
    out = _run(
        _fixture_preamble(timeline_history_error="boom")
        + """
loadOlderAutomatically();
process.stdout.write(JSON.stringify({fetchCalls: fetchCalls.length}));
"""
    )
    assert out["fetchCalls"] == 0


def test_load_older_automatically_sets_guard_and_shows_loading_synchronously() -> None:
    out = _run(
        _fixture_preamble()
        + """
loadOlderAutomatically();
process.stdout.write(JSON.stringify({
  loading: timelineLoadingOlder,
  statusHtml: historyStatusHtml
}));
"""
    )
    assert out["loading"] is True
    assert "Loading older messages" in out["statusHtml"]


def test_load_older_automatically_prevents_duplicate_concurrent_requests() -> None:
    out = _run(
        _fixture_preamble()
        + """
loadOlderAutomatically();
loadOlderAutomatically();
loadOlderAutomatically();
process.stdout.write(JSON.stringify({fetchCalls: fetchCalls.length}));
"""
    )
    assert out["fetchCalls"] == 1


# ---------------------------------------------------------------------------
# Async resolution: success / end-of-history / failure / retry
# ---------------------------------------------------------------------------

_SUCCESS_FETCH = (
    "function(url){ fetchCalls.push(url); return Promise.resolve({ok:true,status:200,"
    "json:function(){return Promise.resolve({messages:[{msgid:'old-1',msgtime:1},"
    "{msgid:'old-2',msgtime:2}],pagination:{has_older:true,next_before:'cursor-2'}});}}); }"
)


def test_load_older_success_prepends_messages_in_order_and_resets_guard() -> None:
    out = _run(
        _fixture_preamble(
            timeline_msgs=[_msg("existing-1", 100)],
            fetch_impl=_SUCCESS_FETCH,
        )
        + """
loadOlderAutomatically();
await flush();
process.stdout.write(JSON.stringify({
  ids: timelineMsgs.map(function(m){return m.msgid;}),
  loading: timelineLoadingOlder,
  hasOlder: timelineHasOlder,
  nextBefore: timelineNextBefore,
  statusHtml: historyStatusHtml
}));
"""
    )
    assert out["ids"] == ["old-1", "old-2", "existing-1"]
    assert out["loading"] is False
    assert out["hasOlder"] is True
    assert out["nextBefore"] == "cursor-2"
    assert "Loading older messages" not in out["statusHtml"]
    assert "No more history" not in out["statusHtml"]


_END_OF_HISTORY_FETCH = (
    "function(url){ fetchCalls.push(url); return Promise.resolve({ok:true,status:200,"
    "json:function(){return Promise.resolve({messages:[{msgid:'old-1',msgtime:1}],"
    "pagination:{has_older:false,next_before:null}});}}); }"
)


def test_load_older_shows_end_of_history_and_stops_further_requests() -> None:
    out = _run(
        _fixture_preamble(fetch_impl=_END_OF_HISTORY_FETCH)
        + """
loadOlderAutomatically();
await flush();
var firstRoundFetches = fetchCalls.length;
loadOlderAutomatically();  // must be a no-op now that hasOlder is false
process.stdout.write(JSON.stringify({
  statusHtml: historyStatusHtml,
  hasOlder: timelineHasOlder,
  firstRoundFetches: firstRoundFetches,
  totalFetches: fetchCalls.length
}));
"""
    )
    assert "No more history" in out["statusHtml"]
    assert out["hasOlder"] is False
    assert out["firstRoundFetches"] == 1
    assert out["totalFetches"] == 1


_FAILURE_FETCH = "function(url){ fetchCalls.push(url); return Promise.resolve({ok:false,status:500}); }"


def test_load_older_failure_shows_retry_and_keeps_existing_messages_intact() -> None:
    out = _run(
        _fixture_preamble(
            timeline_msgs=[_msg("existing-1", 100)],
            fetch_impl=_FAILURE_FETCH,
        )
        + """
loadOlderAutomatically();
await flush();
process.stdout.write(JSON.stringify({
  ids: timelineMsgs.map(function(m){return m.msgid;}),
  loading: timelineLoadingOlder,
  error: timelineHistoryError,
  statusHtml: historyStatusHtml
}));
"""
    )
    assert out["ids"] == ["existing-1"]
    assert out["loading"] is False
    assert out["error"]
    assert "Failed to load history" in out["statusHtml"]
    assert "history-retry-btn" in out["statusHtml"]


def test_retry_clears_error_and_reissues_request_via_same_pipeline() -> None:
    out = _run(
        _fixture_preamble(
            timeline_msgs=[_msg("existing-1", 100)],
            fetch_impl=_FAILURE_FETCH,
        )
        + """
loadOlderAutomatically();
await flush();
var fetchesAfterFailure = fetchCalls.length;
var errorAfterFailure = timelineHistoryError;

// swap in a success stub for the retry attempt, mirroring a transient outage
fetch = function(url){ fetchCalls.push(url); return Promise.resolve({ok:true,status:200,
  json:function(){return Promise.resolve({messages:[{msgid:'old-1',msgtime:1}],
  pagination:{has_older:false,next_before:null}});}}); };

retryLoadOlder();
var errorClearedSynchronously = (timelineHistoryError === null);
await flush();
process.stdout.write(JSON.stringify({
  fetchesAfterFailure: fetchesAfterFailure,
  errorAfterFailure: !!errorAfterFailure,
  errorClearedSynchronously: errorClearedSynchronously,
  totalFetches: fetchCalls.length,
  ids: timelineMsgs.map(function(m){return m.msgid;}),
  finalError: timelineHistoryError
}));
"""
    )
    assert out["fetchesAfterFailure"] == 1
    assert out["errorAfterFailure"] is True
    assert out["errorClearedSynchronously"] is True
    assert out["totalFetches"] == 2
    assert out["ids"] == ["old-1", "existing-1"]
    assert out["finalError"] is None


def test_load_older_unauthorized_response_does_not_surface_retry_ui() -> None:
    """A 401 mid-scroll should defer to the existing handleUnauth() redirect
    flow, not show a retry button that would just 401 again."""
    unauth_fetch = "function(url){ fetchCalls.push(url); return Promise.resolve({ok:false,status:401}); }"
    out = _run(
        _fixture_preamble(fetch_impl=unauth_fetch)
        + """
loadOlderAutomatically();
await flush();
process.stdout.write(JSON.stringify({
  redirected: window.location.href,
  statusHtml: historyStatusHtml,
  loading: timelineLoadingOlder,
  error: timelineHistoryError
}));
"""
    )
    assert out["redirected"] == "/admin/login"
    assert "history-retry-btn" not in out["statusHtml"]
    assert out["loading"] is False
    assert out["error"] is None


# ---------------------------------------------------------------------------
# IntersectionObserver wiring
# ---------------------------------------------------------------------------


_OBSERVER_STUB = """
observerInstances = [];
IntersectionObserver = function(cb, opts) {
  this.cb = cb;
  this.opts = opts;
  this.observed = [];
  this.disconnected = false;
  observerInstances.push(this);
};
IntersectionObserver.prototype.observe = function(el) { this.observed.push(el); };
IntersectionObserver.prototype.disconnect = function() { this.disconnected = true; };
"""


def test_start_history_observer_observes_sentinel_with_body_as_root() -> None:
    out = _run(
        _OBSERVER_STUB
        + _fixture_preamble()
        + """
startHistoryObserver();
var inst = observerInstances[observerInstances.length - 1];
process.stdout.write(JSON.stringify({
  count: observerInstances.length,
  rootIsBody: inst.opts.root === timelineBodyEl,
  observedSentinel: inst.observed.indexOf(sentinelEl) !== -1
}));
"""
    )
    assert out["count"] == 1
    assert out["rootIsBody"] is True
    assert out["observedSentinel"] is True


def test_history_observer_triggers_load_when_intersecting_near_top() -> None:
    out = _run(
        _OBSERVER_STUB
        + _fixture_preamble()
        + """
startHistoryObserver();
timelineBodyEl.scrollTop = 5;
var inst = observerInstances[0];
inst.cb([{isIntersecting: true}]);
process.stdout.write(JSON.stringify({fetchCalls: fetchCalls.length}));
"""
    )
    assert out["fetchCalls"] == 1


def test_history_observer_ignores_intersection_when_not_near_top() -> None:
    out = _run(
        _OBSERVER_STUB
        + _fixture_preamble()
        + """
startHistoryObserver();
timelineBodyEl.scrollTop = 999;
var inst = observerInstances[0];
inst.cb([{isIntersecting: true}]);
process.stdout.write(JSON.stringify({fetchCalls: fetchCalls.length}));
"""
    )
    assert out["fetchCalls"] == 0


def test_history_observer_ignores_non_intersecting_entries() -> None:
    out = _run(
        _OBSERVER_STUB
        + _fixture_preamble()
        + """
startHistoryObserver();
timelineBodyEl.scrollTop = 5;
var inst = observerInstances[0];
inst.cb([{isIntersecting: false}]);
process.stdout.write(JSON.stringify({fetchCalls: fetchCalls.length}));
"""
    )
    assert out["fetchCalls"] == 0


def test_stop_history_observer_disconnects_active_observer() -> None:
    out = _run(
        _OBSERVER_STUB
        + _fixture_preamble()
        + """
startHistoryObserver();
var inst = observerInstances[0];
stopHistoryObserver();
process.stdout.write(JSON.stringify({
  disconnected: inst.disconnected,
  clearedGlobal: (timelineTopObserver === null)
}));
"""
    )
    assert out["disconnected"] is True
    assert out["clearedGlobal"] is True


def test_start_history_observer_replaces_previous_observer() -> None:
    out = _run(
        _OBSERVER_STUB
        + _fixture_preamble()
        + """
startHistoryObserver();
var first = observerInstances[0];
startHistoryObserver();
process.stdout.write(JSON.stringify({
  count: observerInstances.length,
  firstDisconnected: first.disconnected
}));
"""
    )
    assert out["count"] == 2
    assert out["firstDisconnected"] is True


# ---------------------------------------------------------------------------
# renderTimeline: sentinel / end-of-history rendering
# ---------------------------------------------------------------------------


def test_render_timeline_shows_sentinel_and_no_end_marker_when_more_history() -> None:
    out = _run(
        _fixture_preamble(timeline_has_older=True, timeline_msgs=[_msg("m1", 1)])
        + """
renderTimeline(false);
process.stdout.write(JSON.stringify({html: timelineBodyEl.innerHTML}));
"""
    )
    html = out["html"]
    assert 'id="timeline-top-sentinel"' in html
    assert "No more history" not in html
    assert "load-older-btn" not in html


def test_render_timeline_shows_end_of_history_when_no_older() -> None:
    out = _run(
        _fixture_preamble(timeline_has_older=False, timeline_msgs=[_msg("m1", 1)])
        + """
timelineBodyEl.innerHTML = '';
renderTimeline(false);
process.stdout.write(JSON.stringify({html: timelineBodyEl.innerHTML}));
"""
    )
    assert "No more history" in out["html"]


# ---------------------------------------------------------------------------
# Coexistence with auto-refresh (RND-153)
# ---------------------------------------------------------------------------


def test_refresh_timeline_skips_update_while_older_history_is_loading() -> None:
    refresh_src = _extract(
        r"function refreshTimelineIfSelected\(\)\{.*?\n\}", "refreshTimelineIfSelected()"
    )
    out = _run(
        _fixture_preamble(timeline_loading_older=True)
        + f"""
{refresh_src}
await refreshTimelineIfSelected();
process.stdout.write(JSON.stringify({{fetchCalls: fetchCalls.length}}));
"""
    )
    assert out["fetchCalls"] == 0


def test_refresh_timeline_guard_checks_loading_flag_in_source() -> None:
    """Structural guard: refreshTimelineIfSelected() must consult
    timelineLoadingOlder before touching the timeline, per RND-152's
    coexistence requirement with RND-153 auto-refresh."""
    refresh_src = _extract(
        r"function refreshTimelineIfSelected\(\)\{.*?\n\}", "refreshTimelineIfSelected()"
    )
    assert "timelineLoadingOlder" in refresh_src
    # Still must never touch "Load older" pagination cursor state (RND-153 contract).
    assert "timelineHasOlder=" not in refresh_src
    assert "timelineNextBefore=" not in refresh_src


# ---------------------------------------------------------------------------
# QA blocker 1: old-history error/retry state must survive a timeline
# rerender (e.g. triggered by RND-153 refresh), so a hidden error flag can
# never keep auto-load disabled while no retry UI is visible.
# ---------------------------------------------------------------------------


def test_render_timeline_preserves_retry_ui_when_error_is_pending() -> None:
    """A plain renderTimeline() rebuild (which is what a RND-153 refresh
    triggers) must re-show the retry banner while timelineHistoryError is
    set, instead of silently reverting to the plain 'has more' empty state."""
    out = _run(
        _fixture_preamble(
            timeline_has_older=True,
            timeline_msgs=[_msg("existing-1", 100)],
            timeline_history_error="HTTP 500",
        )
        + """
renderTimeline(false);
process.stdout.write(JSON.stringify({html: timelineBodyEl.innerHTML}));
"""
    )
    html = out["html"]
    assert "Failed to load history" in html
    assert "history-retry-btn" in html
    assert "No more history" not in html


def test_refresh_timeline_rerender_keeps_retry_ui_visible_while_error_pending() -> None:
    """End-to-end for blocker 1: run the real refreshTimelineIfSelected()
    (not just renderTimeline) while an old-history error is pending, and
    confirm the retry banner is still visible afterward — it must never be
    silently cleared by an unrelated auto-refresh."""
    refresh_src = _extract(
        r"function refreshTimelineIfSelected\(\)\{.*?\n\}", "refreshTimelineIfSelected()"
    )
    refresh_deps = "\n".join(
        [
            _extract(r"var timelineRequestGen=0;", "timelineRequestGen"),
            _extract(r"var lastRenderedTimelineSignature=null;", "lastRenderedTimelineSignature"),
            _extract(r"function timelineSignature\(msgs\)\{.*?\n\}", "timelineSignature()"),
            _extract(r"function isNearBottom\(\)\{.*?\n\}", "isNearBottom()"),
            _extract(r"function showNewMessageIndicator\(\)\{.*?\n\}", "showNewMessageIndicator()"),
            _extract(r"function hideNewMessageIndicator\(\)\{.*?\n\}", "hideNewMessageIndicator()"),
            _extract(r"function mergeMessagesByMsgid\(existing,incoming\)\{.*?\n\}", "mergeMessagesByMsgid()"),
        ]
    )
    refresh_fetch = (
        "function(url){ fetchCalls.push(url); return Promise.resolve({ok:true,status:200,"
        "json:function(){return Promise.resolve({messages:[{msgid:'existing-1',msgtime:100}],"
        "pagination:{has_older:true,next_before:'cursor-1'}});}}); }"
    )
    out = _run(
        _fixture_preamble(
            timeline_has_older=True,
            timeline_msgs=[_msg("existing-1", 100)],
            timeline_history_error="HTTP 500",
            fetch_impl=refresh_fetch,
        )
        + f"""
{refresh_deps}
{refresh_src}
await refreshTimelineIfSelected();
process.stdout.write(JSON.stringify({{
  html: timelineBodyEl.innerHTML,
  error: timelineHistoryError
}}));
"""
    )
    assert "Failed to load history" in out["html"]
    assert "history-retry-btn" in out["html"]
    # The error flag itself is untouched by refresh (Option A) — Retry still
    # goes through retryLoadOlder(), not some refresh-driven side effect.
    assert out["error"] == "HTTP 500"


def test_error_flag_never_blocks_auto_load_without_visible_retry_ui() -> None:
    """Invariant behind blocker 1: whenever timelineHistoryError blocks
    loadOlderAutomatically(), a renderTimeline() pass must show the retry
    UI — it can never be a *hidden* flag with no way for the user to act."""
    out = _run(
        _fixture_preamble(
            timeline_has_older=True,
            timeline_msgs=[_msg("existing-1", 100)],
            timeline_history_error="HTTP 500",
        )
        + """
renderTimeline(false);
loadOlderAutomatically();  // must stay blocked ...
var blockedWithHiddenUi = (fetchCalls.length === 0) && !/history-retry-btn/.test(timelineBodyEl.innerHTML);
process.stdout.write(JSON.stringify({
  fetchCalls: fetchCalls.length,
  blockedWithHiddenUi: blockedWithHiddenUi
}));
"""
    )
    assert out["fetchCalls"] == 0
    assert out["blockedWithHiddenUi"] is False


# ---------------------------------------------------------------------------
# QA blocker 2: stale-response guard when the conversation changes while an
# older-history request is in flight.
# ---------------------------------------------------------------------------


_DEFERRED_FETCH_PREAMBLE = """
var resolveFetch, rejectFetch;
fetch = function(url){
  fetchCalls.push(url);
  return new Promise(function(res, rej){ resolveFetch = res; rejectFetch = rej; });
};
"""


def _switch_to_conversation_b_snippet() -> str:
    # Mirrors exactly what production loadTimeline('conv-2') does: swap the
    # active conversation id and reset all per-conversation timeline state,
    # independent of whatever conv-1 request is still in flight.
    return """
timelineConvId = 'conv-2';
timelineMsgs = [{msgid:'b-1', msgtime:900, sender:'staff_alice', sender_display_name:'Alice',
  sender_raw_id:'staff_alice', recipients:['contact_zhangsan'], recipient_display_names:['Zhang San'],
  recipient_raw_ids:['contact_zhangsan'], msgtime:900, msgtype:'text', content_text:'conv b message',
  roomid:null, decrypt_status:'success', media_type:'text', media_status:null,
  unsupported_reason:null, media_url:null}];
timelineHasOlder = false;
timelineNextBefore = null;
timelineLoadingOlder = false;
timelineHistoryError = null;
timelineBodyEl.scrollTop = 42;
"""


def test_stale_success_response_does_not_touch_new_conversation_state() -> None:
    out = _run(
        _fixture_preamble(timeline_msgs=[_msg("a-1", 100)])
        + _DEFERRED_FETCH_PREAMBLE
        + """
loadOlderAutomatically();  // in-flight request for conv-1
"""
        + _switch_to_conversation_b_snippet()
        + """
// conv-1's request now resolves late, with conv-1 data.
resolveFetch({ok:true,status:200,json:function(){return Promise.resolve({
  messages:[{msgid:'a-old-1',msgtime:1},{msgid:'a-old-2',msgtime:2}],
  pagination:{has_older:true,next_before:'a-cursor-2'}
});}});
await flush();
process.stdout.write(JSON.stringify({
  convId: timelineConvId,
  ids: timelineMsgs.map(function(m){return m.msgid;}),
  hasOlder: timelineHasOlder,
  nextBefore: timelineNextBefore,
  loading: timelineLoadingOlder,
  error: timelineHistoryError,
  scrollTop: timelineBodyEl.scrollTop
}));
"""
    )
    assert out["convId"] == "conv-2"
    assert out["ids"] == ["b-1"], "conv-1's stale older page must not be prepended into conv-2"
    assert out["hasOlder"] is False
    assert out["nextBefore"] is None
    assert out["loading"] is False
    assert out["error"] is None
    assert out["scrollTop"] == 42, "stale response must not touch conv-2's scroll position"


def test_stale_failure_response_does_not_set_error_on_new_conversation() -> None:
    out = _run(
        _fixture_preamble(timeline_msgs=[_msg("a-1", 100)])
        + _DEFERRED_FETCH_PREAMBLE
        + """
loadOlderAutomatically();  // in-flight request for conv-1
"""
        + _switch_to_conversation_b_snippet()
        + """
// conv-1's request now fails, after the user has already moved to conv-2.
resolveFetch({ok:false,status:500});
await flush();
process.stdout.write(JSON.stringify({
  convId: timelineConvId,
  ids: timelineMsgs.map(function(m){return m.msgid;}),
  hasOlder: timelineHasOlder,
  loading: timelineLoadingOlder,
  error: timelineHistoryError,
  scrollTop: timelineBodyEl.scrollTop
}));
"""
    )
    assert out["convId"] == "conv-2"
    assert out["ids"] == ["b-1"]
    assert out["hasOlder"] is False
    assert out["loading"] is False
    assert out["error"] is None, "conv-1's failure must not surface a retry banner on conv-2"
    assert out["scrollTop"] == 42


def test_stale_response_leaves_loading_guard_usable_for_new_conversation() -> None:
    """After a stale response is discarded, conv-2 must still be able to run
    its own loadOlderAutomatically() normally — the guard must not be stuck
    true forever because of conv-1's abandoned request."""
    success_fetch_for_b = (
        "function(url){ fetchCalls.push(url); return Promise.resolve({ok:true,status:200,"
        "json:function(){return Promise.resolve({messages:[{msgid:'b-old-1',msgtime:1}],"
        "pagination:{has_older:false,next_before:null}});}}); }"
    )
    out = _run(
        _fixture_preamble(timeline_msgs=[_msg("a-1", 100)])
        + _DEFERRED_FETCH_PREAMBLE
        + """
loadOlderAutomatically();  // in-flight request for conv-1
"""
        + _switch_to_conversation_b_snippet()
        + f"""
timelineHasOlder = true;  // conv-2 has its own older page available
timelineNextBefore = 'b-cursor-1';
fetch = {success_fetch_for_b};

// conv-1's stale request resolves first ...
resolveFetch({{ok:true,status:200,json:function(){{return Promise.resolve({{
  messages:[{{msgid:'a-old-1',msgtime:1}}],
  pagination:{{has_older:true,next_before:'a-cursor-2'}}
}});}}}});
await flush();
var loadingStuckAfterStaleResponse = timelineLoadingOlder;

// ... and conv-2 must still be able to load its own older history.
loadOlderAutomatically();
await flush();
process.stdout.write(JSON.stringify({{
  loadingStuckAfterStaleResponse: loadingStuckAfterStaleResponse,
  convId: timelineConvId,
  ids: timelineMsgs.map(function(m){{return m.msgid;}}),
  hasOlder: timelineHasOlder,
  loading: timelineLoadingOlder
}}));
"""
    )
    assert out["loadingStuckAfterStaleResponse"] is False
    assert out["convId"] == "conv-2"
    assert out["ids"] == ["b-old-1", "b-1"]
    assert out["hasOlder"] is False
    assert out["loading"] is False


def test_fetch_older_messages_itself_guards_against_stale_conversation() -> None:
    """Unit-level guard on fetchOlderMessages(): even called in isolation,
    it must not mutate timelineMsgs/timelineHasOlder/timelineNextBefore if
    timelineConvId has moved on from the id the request was made for."""
    out = _run(
        _fixture_preamble(timeline_msgs=[_msg("b-1", 900)], fetch_impl=_SUCCESS_FETCH)
        + """
timelineConvId = 'conv-2';
timelineHasOlder = false;
timelineNextBefore = null;
await fetchOlderMessages('conv-1', 'some-cursor');
process.stdout.write(JSON.stringify({
  ids: timelineMsgs.map(function(m){return m.msgid;}),
  hasOlder: timelineHasOlder,
  nextBefore: timelineNextBefore
}));
"""
    )
    assert out["ids"] == ["b-1"]
    assert out["hasOlder"] is False
    assert out["nextBefore"] is None
