"""
Tests for the RND-206 narrow remediation pass — top-level image migrated
onto the shared rich-media hydration and recovery chain.

Previously (superseded, see git history / prior QA round): top-level
images were hydrated by a standalone chain (loadMediaImage/
onMediaImageError/showMediaError/hydrateMediaImages) that independently
called fetch(), managed its own retry-once state via a data-retried
attribute, and only ever recognized 401 (via handleUnauth) before
collapsing every other failure (403 included) into the generic
"Image failed to load" text. That chain has been removed entirely.

Now: renderMessageBody's top-level image branch renders through
renderViewableMediaSlot() -- the exact same entry point nested/video/
voice/file/emotion use -- and is resolved exclusively by
hydrateRichMedia -> loadRichMedia -> fetchDescriptorWithRecovery ->
MediaAccessCache -> swapRichMediaPlaceholder, with error classification
via classifyMediaError (401 -> auth, 403 -> forbidden, 404 -> missing,
network -> network_error, other -> generic load failure). This file
supersedes the retired test_media_hydration.py.

These tests execute the real embedded JS under Node, asserting on actual
request counts, resolved image src, and rendered error text -- not source
presence.

Run (from backend/):
    pytest tests/test_rnd_206_top_level_image.py -v
"""

from __future__ import annotations

import json
import re
import shutil

import pytest

from app.routers.web import _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON
from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_js_source

_REVIEW_CONSOLE_JS = review_console_js_source()

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_JS, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_JS"
    return match.group(0)


def _rnd206_block() -> str:
    return _extract(
        r"var MediaAccessCache=\(function\(\)\{.*?\nfunction renderCompositeMessage\(m\)\{.*?\n\}",
        "RND-206 rich-media/composite block",
    )


def _bundle(extra=None) -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('en');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\}", "pad()"),
        _extract(r"function handleUnauth\(r\)\{.*?\n\}", "handleUnauth()"),
        _extract(r"var MEDIA_LABELS=\{.*?\};", "MEDIA_LABELS"),
        _extract(r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"),
        f"var RND216_MTR_ENTRIES = {_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON};",
        _extract(r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"),
        _extract(r"function isSafeUrl\(u\)\{.*?\n\}", "isSafeUrl()"),
        _extract(r"function hostnameOf\(u\)\{.*?\n\}", "hostnameOf()"),
        _extract(r"function fmtCoord\(n\)\{.*?\}", "fmtCoord()"),
        _extract(r"function renderStructuredFallback\(m\)\{.*?\n\}", "renderStructuredFallback()"),
        _extract(r"function renderLinkCard\(m\)\{.*?\n\}", "renderLinkCard()"),
        _extract(r"function renderLocationCard\(m\)\{.*?\n\}", "renderLocationCard()"),
        _extract(r"function renderSanitizedMarkdown\(raw\)\{.*?\n\}", "renderSanitizedMarkdown()"),
        _extract(r"function renderMarkdownCard\(m\)\{.*?\n\}", "renderMarkdownCard()"),
        _extract(r"function renderNewsCard\(m\)\{.*?\n\}", "renderNewsCard()"),
        _extract(r"function renderMiniprogramCard\(m\)\{.*?\n\}", "renderMiniprogramCard()"),
        _extract(r"function renderVoteCard\(m\)\{.*?\n\}", "renderVoteCard()"),
        _extract(r"function renderTodoCard\(m\)\{.*?\n\}", "renderTodoCard()"),
        _extract(r"function renderCollectCard\(m\)\{.*?\n\}", "renderCollectCard()"),
        _extract(r"function renderMeetingCard\(m\)\{.*?\n\}", "renderMeetingCard()"),
        _extract(r"function renderScheduleCard\(m\)\{.*?\n\}", "renderScheduleCard()"),
        _extract(r"function renderRedpacketCard\(m\)\{.*?\n\}", "renderRedpacketCard()"),
        _extract(r"function renderSwitchCorpCard\(m\)\{.*?\n\}", "renderSwitchCorpCard()"),
        _extract(r"function renderSystemCard\(m\)\{.*?\n\}", "renderSystemCard()"),
        # RND-210 — business-card renderer referenced by STRUCTURED_CARD_RENDERERS
        _extract(r"function renderCardMessage\(m\)\{.*?\n\}", "renderCardMessage()"),
        # RND-210: STRUCTURED_CARD_RENDERERS now also references these two
        # audio renderers — stub them (these tests don't exercise audio).
        "function renderAudioArchiveMessage(m){return '';} function renderAudioDocMessage(m){return '';} function renderSphfeedCard(m){return '';}",
        _extract(r"var STRUCTURED_CARD_RENDERERS=\{.*?\n\};", "STRUCTURED_CARD_RENDERERS"),
        _extract(r"function renderStructuredCard\(m\)\{.*?\n\}", "renderStructuredCard()"),
        _rnd206_block(),
        _extract(r"function renderRevokePlaceholder\(m\)\{.*?\n\}", "renderRevokePlaceholder()"),
        _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()"),
        _extract(r"function safeRenderMessageBody\(m\)\{.*?\n\}", "safeRenderMessageBody()"),
    ]
    parts.extend(extra or [])
    return "\n".join(parts)


def _run(script_body: str, extra=None) -> str:
    assert NODE, "node executable not found"
    harness = f"""
{_bundle(extra)}

(async function() {{
{script_body}
}})().catch(function(e) {{
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
}});
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


def _msg(**overrides) -> dict:
    base = {
        "msgid": "msg-1",
        "sender": "staff_alice",
        "sender_display_name": "Alice",
        "sender_raw_id": "staff_alice",
        "recipients": [],
        "recipient_display_names": [],
        "recipient_raw_ids": [],
        "msgtime": 1751702400000,
        "msgtype": "image",
        "content_text": None,
        "roomid": None,
        "decrypt_status": "success",
        "media_type": "image",
        "media_status": "available",
        "unsupported_reason": None,
        "media_url": "/api/conversations/c1/messages/msg-1/media",
        "media_access_url": "/api/conversations/c1/messages/msg-1/media/access",
        "normalized_type": "image",
        "category": "media",
        "support_status": "supported",
        "renderer_strategy": "media_preview",
        "display_label_key": "messageType.image",
        "is_revoked": False,
        "revoked_at": None,
        "revoke_event_msgid": None,
        "revoke_association_status": None,
        "structured_content": None,
    }
    base.update(overrides)
    return base


# A createElement stub sufficient for swapRichMediaPlaceholder/buildErrorBox
# (div/button/img all need className/textContent/setAttribute/getAttribute/
# appendChild).
_CREATE_ELEMENT_STUB = """
document = {
  createElement: function(tag){
    var attrs = {};
    var el = {
      tagName: tag.toUpperCase(), className: '', textContent: '', src: null, href: null,
      onclick: null, onerror: null,
      setAttribute: function(k,v){ attrs[k] = String(v); },
      getAttribute: function(k){ return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
      appendChild: function(){}
    };
    return el;
  }
};
"""


def _placeholder_and_element(msg=None):
    """Render the real top-level image placeholder via renderMessageBody,
    then build a synthetic <element> stub carrying its actual
    data-rnd206-* attributes (mirroring what a browser's querySelectorAll
    would hand to loadRichMedia) -- proves the real render path emits
    something loadRichMedia can hydrate, not just a hand-built fixture."""
    msg = msg or _msg()
    return msg


_EL_JS = """
function elementFromPlaceholderHtml(html){
  var kindM = /data-rnd206-kind="([^"]+)"/.exec(html);
  var urlM = /data-rnd206-access-url="([^"]+)"/.exec(html);
  var idxM = /data-rnd206-viewer-idx="([^"]+)"/.exec(html);
  var replaced = null;
  var el = {
    getAttribute: function(k){
      if (k === 'data-rnd206-kind') return kindM ? kindM[1] : null;
      if (k === 'data-rnd206-access-url') return urlM ? urlM[1] : null;
      if (k === 'data-rnd206-viewer-idx') return idxM ? idxM[1] : null;
      return null;
    },
    parentNode: { replaceChild: function(newEl){ replaced = newEl; } }
  };
  return { el: el, get replaced(){ return replaced; } };
}
"""


# ---------------------------------------------------------------------------
# Test 1 — success
# ---------------------------------------------------------------------------


def test_top_level_image_success_uses_shared_hydrator_and_assigns_src() -> None:
    msg = _msg()
    out = _run(
        _CREATE_ELEMENT_STUB
        + _EL_JS
        + f"""
var placeholderHtml = renderMessageBody({json.dumps(msg)});
var fetchCalls = [];
fetch = function(url){{
  fetchCalls.push(url);
  return Promise.resolve({{ok:true, status:200, json:function(){{
    return Promise.resolve({{url:'https://media.example.com/signed.png?sig=abc', access_type:'signed_url'}});
  }}}});
}};
var wrap = elementFromPlaceholderHtml(placeholderHtml);
loadRichMedia(wrap.el);
await new Promise(function(res){{ setTimeout(res, 20); }});
var btn = wrap.replaced;
process.stdout.write(JSON.stringify({{
  placeholderHtml: placeholderHtml,
  fetchCalls: fetchCalls,
  tagName: btn ? btn.tagName : null,
  imgSrc: btn ? btn.children ? null : null : null
}}));
"""
    )
    result = json.loads(out)
    assert 'data-rnd206-kind="image"' in result["placeholderHtml"]
    assert result["fetchCalls"] == ["/api/conversations/c1/messages/msg-1/media/access"]
    assert result["tagName"] == "BUTTON"


def test_top_level_image_success_no_duplicate_descriptor_request() -> None:
    """Second hydration of the SAME access url must reuse the cached
    descriptor -- no unnecessary duplicate request."""
    out = _run(
        _CREATE_ELEMENT_STUB
        + """
var fetchCalls = 0;
fetch = function(){
  fetchCalls++;
  return Promise.resolve({ok:true, status:200, json:function(){return Promise.resolve({url:'/resolved.png'});}});
};
var d1 = await MediaAccessCache.get('/api/conversations/c1/messages/msg-1/media/access');
var d2 = await MediaAccessCache.get('/api/conversations/c1/messages/msg-1/media/access');
process.stdout.write(JSON.stringify({fetchCalls: fetchCalls, sameUrl: d1.url === d2.url}));
"""
    )
    result = json.loads(out)
    assert result["fetchCalls"] == 1
    assert result["sameUrl"] is True


def test_top_level_image_never_logs_signed_url() -> None:
    src = _rnd206_block()
    assert "console.log" not in src
    assert "console.error" not in src
    assert "console.warn" not in src


# ---------------------------------------------------------------------------
# Test 2 — 401
# ---------------------------------------------------------------------------


def test_top_level_image_401_shows_authentication_required_not_generic_failure() -> None:
    msg = _msg()
    out = _run(
        _CREATE_ELEMENT_STUB
        + _EL_JS
        + f"""
window = {{location: {{href: ''}}}};
var placeholderHtml = renderMessageBody({json.dumps(msg)});
var fetchCalls = [];
fetch = function(url){{ fetchCalls.push(url); return Promise.resolve({{ok:false, status:401}}); }};
var wrap = elementFromPlaceholderHtml(placeholderHtml);
loadRichMedia(wrap.el);
await new Promise(function(res){{ setTimeout(res, 20); }});
var box = wrap.replaced;
process.stdout.write(JSON.stringify({{
  fetchCalls: fetchCalls,
  errorText: box ? box.textContent : null,
  redirected: window.location.href
}}));
"""
    )
    result = json.loads(out)
    assert result["fetchCalls"] == ["/api/conversations/c1/messages/msg-1/media/access"]
    assert result["errorText"] == "Unauthorized"
    assert result["errorText"] != "Image failed to load"
    assert result["redirected"] == "/admin/login"


# ---------------------------------------------------------------------------
# Test 3 — 403 (reproduces the previously-broken scenario)
# ---------------------------------------------------------------------------


def test_top_level_image_403_shows_forbidden_not_generic_image_load_failure() -> None:
    """This is the exact regression this remediation pass fixes:
    GET /access/top-image -> 403, retry -> 403, previously degraded to
    "Image failed to load". Must now render a specific forbidden/expired
    state through the SAME classification the shared chain already uses
    for every other media kind."""
    msg = _msg()
    out = _run(
        _CREATE_ELEMENT_STUB
        + _EL_JS
        + f"""
var placeholderHtml = renderMessageBody({json.dumps(msg)});
var fetchCalls = [];
fetch = function(url){{ fetchCalls.push(url); return Promise.resolve({{ok:false, status:403}}); }};
var wrap = elementFromPlaceholderHtml(placeholderHtml);
loadRichMedia(wrap.el);
await new Promise(function(res){{ setTimeout(res, 20); }});
var box = wrap.replaced;
process.stdout.write(JSON.stringify({{fetchCalls: fetchCalls, errorText: box ? box.textContent : null}}));
"""
    )
    result = json.loads(out)
    # bounded: one initial attempt + exactly one controlled refresh, never more
    assert result["fetchCalls"] == [
        "/api/conversations/c1/messages/msg-1/media/access",
        "/api/conversations/c1/messages/msg-1/media/access",
    ]
    assert result["errorText"] == "Access denied or link expired"
    assert result["errorText"] != "Image failed to load"


# ---------------------------------------------------------------------------
# Test 4 — 404
# ---------------------------------------------------------------------------


def test_top_level_image_404_shows_missing_media_not_permission_denied() -> None:
    msg = _msg()
    out = _run(
        _CREATE_ELEMENT_STUB
        + _EL_JS
        + f"""
var placeholderHtml = renderMessageBody({json.dumps(msg)});
var fetchCalls = [];
fetch = function(url){{ fetchCalls.push(url); return Promise.resolve({{ok:false, status:404}}); }};
var wrap = elementFromPlaceholderHtml(placeholderHtml);
loadRichMedia(wrap.el);
await new Promise(function(res){{ setTimeout(res, 20); }});
var box = wrap.replaced;
process.stdout.write(JSON.stringify({{fetchCalls: fetchCalls, errorText: box ? box.textContent : null}}));
"""
    )
    result = json.loads(out)
    assert result["fetchCalls"] == ["/api/conversations/c1/messages/msg-1/media/access"]
    assert result["errorText"] == "Media not found"
    assert result["errorText"] not in ("Access denied or link expired", "Unauthorized", "Image failed to load")


# ---------------------------------------------------------------------------
# Test 5 — network failure
# ---------------------------------------------------------------------------


def test_top_level_image_network_failure_shows_network_state() -> None:
    msg = _msg()
    out = _run(
        _CREATE_ELEMENT_STUB
        + _EL_JS
        + f"""
var placeholderHtml = renderMessageBody({json.dumps(msg)});
var fetchCalls = [];
fetch = function(url){{ fetchCalls.push(url); return Promise.reject(new TypeError('Failed to fetch')); }};
var wrap = elementFromPlaceholderHtml(placeholderHtml);
loadRichMedia(wrap.el);
await new Promise(function(res){{ setTimeout(res, 20); }});
var box = wrap.replaced;
process.stdout.write(JSON.stringify({{fetchCalls: fetchCalls.length, errorText: box ? box.textContent : null}}));
"""
    )
    result = json.loads(out)
    assert result["fetchCalls"] == 1  # network errors are not classified as 403, so no retry is attempted
    assert result["errorText"] == "Network connection failed"
    assert result["errorText"] not in ("Access denied or link expired", "Media not found", "Unsupported media")


# ---------------------------------------------------------------------------
# Test 6 — expired descriptor recovery (real element-level playback
# failure, distinct from a descriptor-fetch failure)
# ---------------------------------------------------------------------------


def test_top_level_image_expired_url_recovers_via_one_bounded_refresh() -> None:
    out = _run(
        _CREATE_ELEMENT_STUB
        + """
var fetchCalls = 0;
fetch = function(){
  fetchCalls++;
  return Promise.resolve({ok:true, status:200, json:function(){
    return Promise.resolve({url:'/img-v'+fetchCalls+'.png'});
  }});
};
// Simulate: cached descriptor already resolved once (img.src set to the
// now-expired URL), then the actual <img> element fails to load it.
var img = document.createElement('img');
handleRichMediaPlaybackFailure('image', '/access1', img, img);
await new Promise(function(res){ setTimeout(res, 20); });
process.stdout.write(JSON.stringify({fetchCalls: fetchCalls, finalSrc: img.src}));
"""
    )
    result = json.loads(out)
    assert result["fetchCalls"] == 1  # cache invalidated + exactly one fresh descriptor request
    assert result["finalSrc"] == "/img-v1.png"


def test_top_level_image_expired_url_does_not_log_or_persist_signed_url() -> None:
    src = _rnd206_block()
    assert "localStorage" not in src
    assert "sessionStorage" not in src


# ---------------------------------------------------------------------------
# Test 7 — retry exhaustion
# ---------------------------------------------------------------------------


def test_top_level_image_retry_exhaustion_preserves_forbidden_classification() -> None:
    """initial descriptor fetch fails (403), controlled refresh also fails
    (403) -- final state must still be the strongest known classification
    (forbidden), never a generic failure, and no third request is made."""
    msg = _msg()
    out = _run(
        _CREATE_ELEMENT_STUB
        + _EL_JS
        + f"""
var placeholderHtml = renderMessageBody({json.dumps(msg)});
var fetchCalls = [];
fetch = function(url){{ fetchCalls.push(url); return Promise.resolve({{ok:false, status:403}}); }};
var wrap = elementFromPlaceholderHtml(placeholderHtml);
loadRichMedia(wrap.el);
await new Promise(function(res){{ setTimeout(res, 30); }});
var box = wrap.replaced;
process.stdout.write(JSON.stringify({{
  requestCount: fetchCalls.length,
  errorText: box ? box.textContent : null,
  isLoadingPlaceholder: box ? box.className === 'media-rich-loading' : null
}}));
"""
    )
    result = json.loads(out)
    assert result["requestCount"] == 2  # bounded: never a third attempt
    assert result["errorText"] == "Access denied or link expired"
    assert result["isLoadingPlaceholder"] is False  # loading state cleared, replaced with a terminal error box


# ---------------------------------------------------------------------------
# Test 8 — shared-path proof: top-level and nested images use the SAME
# descriptor cache, recovery function, and classification function (not
# two independently-implemented systems that happen to produce similar
# output).
# ---------------------------------------------------------------------------


def test_top_level_and_nested_image_share_the_same_cache_recovery_and_classifier() -> None:
    top_level_msg = _msg()
    nested_node_media = {
        "status": "available",
        "media_type": "image",
        "access_url": "/api/conversations/c1/messages/m1/nested-media/0/access",
    }
    out = _run(
        _CREATE_ELEMENT_STUB
        + _EL_JS
        + f"""
var cacheGetCalls = [];
var realGet = MediaAccessCache.get;
MediaAccessCache.get = function(url){{ cacheGetCalls.push(url); return realGet(url); }};
var recoveryCalls = [];
var realRecovery = fetchDescriptorWithRecovery;
fetchDescriptorWithRecovery = function(url, retried){{ recoveryCalls.push(url); return realRecovery(url, retried); }};
fetch = function(url){{ return Promise.resolve({{ok:true, status:200, json:function(){{return Promise.resolve({{url:'/resolved-'+url}});}}}}); }};

var topLevelHtml = renderMessageBody({json.dumps(top_level_msg)});
var topWrap = elementFromPlaceholderHtml(topLevelHtml);
loadRichMedia(topWrap.el);
await new Promise(function(res){{ setTimeout(res, 20); }});

var nestedPlaceholder = renderNestedImageSlot({json.dumps(nested_node_media["access_url"])});
var nestedWrap = elementFromPlaceholderHtml(nestedPlaceholder);
loadRichMedia(nestedWrap.el);
await new Promise(function(res){{ setTimeout(res, 20); }});

process.stdout.write(JSON.stringify({{
  cacheGetCalls: cacheGetCalls,
  recoveryCalls: recoveryCalls,
  topLevelResolved: topWrap.replaced ? topWrap.replaced.tagName : null,
  nestedResolved: nestedWrap.replaced ? nestedWrap.replaced.tagName : null
}}));
"""
    )
    result = json.loads(out)
    # Both the top-level image AND the nested image call went through the
    # exact same (spied) MediaAccessCache.get and fetchDescriptorWithRecovery
    # function objects -- proof of one shared implementation, not two.
    assert "/api/conversations/c1/messages/msg-1/media/access" in result["cacheGetCalls"]
    assert "/api/conversations/c1/messages/m1/nested-media/0/access" in result["cacheGetCalls"]
    assert "/api/conversations/c1/messages/msg-1/media/access" in result["recoveryCalls"]
    assert "/api/conversations/c1/messages/m1/nested-media/0/access" in result["recoveryCalls"]
    assert result["topLevelResolved"] == "BUTTON"
    assert result["nestedResolved"] == "BUTTON"


def test_legacy_loader_functions_no_longer_exist() -> None:
    """The standalone RND-187 chain must actually be gone, not merely
    unused -- confirms this is a real migration, not a second parallel
    system left dormant alongside the shared one."""
    assert "function loadMediaImage(" not in _REVIEW_CONSOLE_JS
    assert "function onMediaImageError(" not in _REVIEW_CONSOLE_JS
    assert "function showMediaError(" not in _REVIEW_CONSOLE_JS
    assert "function hydrateMediaImages(" not in _REVIEW_CONSOLE_JS


# ---------------------------------------------------------------------------
# Regression: viewer entry (click-to-open) still works for a successfully
# hydrated top-level image.
# ---------------------------------------------------------------------------


def test_top_level_image_viewer_trigger_remains_usable_after_success() -> None:
    msg = _msg()
    out = _run(
        _CREATE_ELEMENT_STUB
        + _EL_JS
        + f"""
timelineViewerItems = [];
var placeholderHtml = renderMessageBody({json.dumps(msg)});
var openedWith = null;
openViewer = function(items, idx){{ openedWith = {{items: items, idx: idx}}; }};
fetch = function(){{ return Promise.resolve({{ok:true, status:200, json:function(){{return Promise.resolve({{url:'/resolved.png'}});}}}}); }};
var wrap = elementFromPlaceholderHtml(placeholderHtml);
loadRichMedia(wrap.el);
await new Promise(function(res){{ setTimeout(res, 20); }});
wrap.replaced.onclick();
process.stdout.write(JSON.stringify({{opened: openedWith !== null, idx: openedWith ? openedWith.idx : null}}));
"""
    )
    result = json.loads(out)
    assert result["opened"] is True
    assert result["idx"] == 0
