"""
Tests for RND-206 — Unified Message Viewer & Rich Media Rendering.

Scope: the new embedded JS added to app/main.py's _REVIEW_CONSOLE_HTML —
MediaAccessCache, the shared Viewer, video/voice/file/emotion lazy
renderers, and the composite (mixed/chatrecord) node renderer. Does not
re-test image hydration (test_media_hydration.py), structured cards
(test_message_type_registry.py), revoke (test_revoke_frontend_render.py),
or the legacy unsupported-placeholder mechanism
(test_unsupported_message_labels.py) — those are unchanged by this ticket
except where noted in their own files.

Executes the real embedded JS under Node (same technique used throughout
this test suite — see test_media_hydration.py's module docstring).

Run (from backend/):
    pytest tests/test_rnd_206_rich_media.py -v
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


def _rnd206_block() -> str:
    return _extract(
        r"var MediaAccessCache=\(function\(\)\{.*?\nfunction renderCompositeMessage\(m\)\{.*?\n\}",
        "RND-206 rich-media/composite block",
    )


def _bundle(extra: list[str] | None = None) -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('en');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\}", "pad()"),
        _extract(r"function handleUnauth\(r\)\{.*?\n\}", "handleUnauth()"),
        _extract(r"var MEDIA_LABELS=\{.*?\};", "MEDIA_LABELS"),
        _extract(r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"),
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
        _extract(r"var STRUCTURED_CARD_RENDERERS=\{.*?\n\};", "STRUCTURED_CARD_RENDERERS"),
        _extract(r"function renderStructuredCard\(m\)\{.*?\n\}", "renderStructuredCard()"),
        _rnd206_block(),
        _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()"),
        _extract(r"function safeRenderMessageBody\(m\)\{.*?\n\}", "safeRenderMessageBody()"),
        _extract(r"function timelineSignature\(msgs\)\{.*?\n\}", "timelineSignature()"),
        _extract(r"function renderTimeline\(scrollToBottom\)\{.*?\n\}", "renderTimeline()"),
    ]
    parts.extend(extra or [])
    return "\n".join(parts)


def _run(script_body: str, extra: list[str] | None = None) -> str:
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
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


def _msg(msgtype, media_type, **overrides) -> dict:
    base = {
        "msgid": "msg-1",
        "sender": "staff_alice",
        "sender_display_name": "Alice",
        "sender_raw_id": "staff_alice",
        "recipients": [],
        "recipient_display_names": [],
        "recipient_raw_ids": [],
        "msgtime": 1751702400000,
        "msgtype": msgtype,
        "content_text": None,
        "roomid": None,
        "decrypt_status": "success",
        "media_type": media_type,
        "media_status": None,
        "unsupported_reason": None,
        "media_url": None,
        "media_access_url": None,
        "normalized_type": msgtype,
        "category": "media",
        "support_status": "partial",
        # RND-206: renderer_strategy=="media_preview" is the registry-driven
        # gate app.main.renderMessageBody now checks for video/voice/file
        # (message_type_registry.py) -- matches what a real TimelineMessageOut
        # row for these types actually carries.
        "renderer_strategy": "media_preview",
        "display_label_key": f"messageType.{msgtype}",
        "structured_content": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Static presence checks
# ---------------------------------------------------------------------------


def test_video_placeholder_no_longer_hardcoded_unsupported() -> None:
    # RND-206 requirement: the old blanket "不支持视频消息" copy must no
    # longer be the ONLY thing ever shown for a video message — a real
    # <video> element must be reachable once media_status=="available".
    assert "data-rnd206-kind" in _REVIEW_CONSOLE_HTML
    assert "renderVideoPreview" in _REVIEW_CONSOLE_HTML
    assert "media-video" in _REVIEW_CONSOLE_HTML


def test_viewer_markup_present() -> None:
    assert "rnd206-viewer" in _REVIEW_CONSOLE_HTML
    assert "function openViewer(" in _REVIEW_CONSOLE_HTML
    assert "function closeViewer(" in _REVIEW_CONSOLE_HTML
    assert "ArrowLeft" in _REVIEW_CONSOLE_HTML and "ArrowRight" in _REVIEW_CONSOLE_HTML
    assert "Escape" in _REVIEW_CONSOLE_HTML


def test_media_access_cache_never_persists_to_localstorage() -> None:
    src = _extract(r"var MediaAccessCache=\(function\(\)\{.*?\n\}\)\(\);", "MediaAccessCache")
    assert "localStorage" not in src
    assert "sessionStorage" not in src


# ---------------------------------------------------------------------------
# renderMessageBody: video / voice / file / emotion
# ---------------------------------------------------------------------------


def test_video_available_renders_lazy_placeholder_with_access_url() -> None:
    msg = _msg("video", "video", media_status="available", media_access_url="/api/x/media/access")
    out = _run(
        f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));"
    )
    html = json.loads(out)
    assert 'data-rnd206-kind="video"' in html
    assert 'data-rnd206-access-url="/api/x/media/access"' in html
    assert "media-rich-loading" in html


def test_voice_available_renders_lazy_placeholder() -> None:
    msg = _msg("voice", "voice", media_status="available", media_access_url="/api/x/media/access")
    out = _run(f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));")
    html = json.loads(out)
    assert 'data-rnd206-kind="voice"' in html


def test_file_available_renders_lazy_placeholder() -> None:
    msg = _msg("file", "file", media_status="available", media_access_url="/api/x/media/access")
    out = _run(f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));")
    html = json.loads(out)
    assert 'data-rnd206-kind="file"' in html


def test_video_not_available_shows_type_and_status_not_broken_player() -> None:
    msg = _msg("video", "video", media_status="not_downloaded")
    out = _run(f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));")
    html = json.loads(out)
    assert "<video" not in html
    assert "media-placeholder" in html


def test_emotion_with_access_url_renders_preview() -> None:
    msg = _msg(
        "emotion",
        "unsupported",
        media_status="unsupported",
        media_access_url="/api/x/media/access",
    )
    out = _run(f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));")
    html = json.loads(out)
    assert 'data-rnd206-kind="emotion"' in html


def test_emotion_without_access_url_falls_back_to_registry_placeholder() -> None:
    # No backend URL available (not downloaded / not servable) -- must
    # fall through to the same MessageTypeRegistry-driven placeholder every
    # other still-unsupported type uses, not a bespoke string.
    msg = _msg("emotion", "unsupported", media_status="unsupported")
    out = _run(f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));")
    html = json.loads(out)
    assert "data-rnd206-kind" not in html
    assert "media-placeholder" in html


def test_video_voice_file_never_hardcode_per_msgtype_conditional_beyond_media_type() -> None:
    # Mirrors test_unsupported_message_labels.py's architectural guard: the
    # only new msgtype-literal branch introduced by RND-206 is the emotion
    # access-URL check (which must key off normalized_type, not msgtype, to
    # satisfy that guard) -- video/voice/file dispatch off media_type, which
    # is already registry-derived.
    src = _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()")
    assert re.findall(r"m\.msgtype\s*===", src) == []


# ---------------------------------------------------------------------------
# MediaAccessCache
# ---------------------------------------------------------------------------


def test_media_access_cache_reuses_proxy_descriptor_without_refetching() -> None:
    out = _run(
        """
var fetchCalls=0;
fetch=function(){fetchCalls++;return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve({url:'/proxy/path',access_type:'proxy'});}});};
var d1=await MediaAccessCache.get('/access1');
var d2=await MediaAccessCache.get('/access1');
process.stdout.write(JSON.stringify({fetchCalls:fetchCalls,url1:d1.url,url2:d2.url}));
"""
    )
    result = json.loads(out)
    assert result["fetchCalls"] == 1
    assert result["url1"] == result["url2"] == "/proxy/path"


def test_media_access_cache_refetches_after_signed_url_expiry() -> None:
    out = _run(
        """
var fetchCalls=0;
fetch=function(){
  fetchCalls++;
  var expires=new Date(Date.now()+1000).toISOString();
  return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve({url:'/signed/'+fetchCalls,access_type:'signed_url',expires_at:expires});}});
};
var d1=await MediaAccessCache.get('/access1');
await new Promise(function(res){setTimeout(res,1100);});
var d2=await MediaAccessCache.get('/access1');
process.stdout.write(JSON.stringify({fetchCalls:fetchCalls,url1:d1.url,url2:d2.url}));
""",
    )
    result = json.loads(out)
    assert result["fetchCalls"] == 2
    assert result["url1"] != result["url2"]


def test_media_access_cache_surfaces_unauthorized_and_missing() -> None:
    # RND-206 QA fix #8: errors now carry a numeric .status (or .network)
    # so callers can classify 401/403/404/network distinctly via
    # classifyMediaError() instead of a fixed two-bucket boolean shape.
    out = _run(
        """
fetch=function(){return Promise.resolve({ok:false,status:401});};
var unauthorized=null;
try{await MediaAccessCache.get('/a');}catch(e){unauthorized=classifyMediaError(e);}
fetch=function(){return Promise.resolve({ok:false,status:404});};
var missing=null;
try{await MediaAccessCache.get('/b');}catch(e){missing=classifyMediaError(e);}
fetch=function(){return Promise.resolve({ok:false,status:403});};
var forbidden=null;
try{await MediaAccessCache.get('/c');}catch(e){forbidden=classifyMediaError(e);}
fetch=function(){return Promise.reject(new TypeError('Failed to fetch'));};
var network=null;
try{await MediaAccessCache.get('/d');}catch(e){network=classifyMediaError(e);}
process.stdout.write(JSON.stringify({unauthorized:unauthorized,missing:missing,forbidden:forbidden,network:network}));
"""
    )
    result = json.loads(out)
    assert result["unauthorized"] == "auth"
    assert result["missing"] == "missing"
    assert result["forbidden"] == "forbidden"
    assert result["network"] == "network"


def test_media_access_cache_malformed_expiry_is_not_cached_forever() -> None:
    # RND-206 QA fix #8: a malformed/unparseable expires_at must be treated
    # as non-cacheable (expired), never as "cache forever" -- the exact
    # opposite of the pre-fix bug.
    out = _run(
        """
var fetchCalls=0;
fetch=function(){
  fetchCalls++;
  return Promise.resolve({ok:true,status:200,json:function(){
    return Promise.resolve({url:'/x/'+fetchCalls,access_type:'signed_url',expires_at:'not-a-real-date'});
  }});
};
var d1=await MediaAccessCache.get('/access1');
var d2=await MediaAccessCache.get('/access1');
process.stdout.write(JSON.stringify({fetchCalls:fetchCalls,url1:d1.url,url2:d2.url}));
"""
    )
    result = json.loads(out)
    assert result["fetchCalls"] == 2
    assert result["url1"] != result["url2"]


def test_media_access_cache_403_triggers_one_controlled_refresh() -> None:
    # RND-206 QA fix #8: a 403 (permission denied / expired grant) is
    # retried exactly once via fetchDescriptorWithRecovery, invalidating
    # the cache first -- never an infinite loop.
    out = _run(
        """
var fetchCalls=0;
fetch=function(){
  fetchCalls++;
  if(fetchCalls===1)return Promise.resolve({ok:false,status:403});
  return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve({url:'/recovered'});}});
};
var desc=await fetchDescriptorWithRecovery('/access1');
process.stdout.write(JSON.stringify({fetchCalls:fetchCalls,url:desc.url}));
"""
    )
    result = json.loads(out)
    assert result["fetchCalls"] == 2
    assert result["url"] == "/recovered"


def test_media_access_cache_403_twice_does_not_loop_forever() -> None:
    out = _run(
        """
var fetchCalls=0;
fetch=function(){fetchCalls++;return Promise.resolve({ok:false,status:403});};
var errKind=null;
try{await fetchDescriptorWithRecovery('/access1');}catch(e){errKind=classifyMediaError(e);}
process.stdout.write(JSON.stringify({fetchCalls:fetchCalls,errKind:errKind}));
"""
    )
    result = json.loads(out)
    assert result["fetchCalls"] == 2  # exactly one retry, never a third attempt
    assert result["errKind"] == "forbidden"


# ---------------------------------------------------------------------------
# Composite (mixed / chatrecord) rendering
# ---------------------------------------------------------------------------


def _mixed_msg(items):
    return _msg(
        "mixed",
        "structured",
        normalized_type="mixed",
        renderer_strategy="composite_view",
        structured_content={"fields": {"items": items, "item_count": len(items)}, "parse_warnings": []},
    )


def test_mixed_renders_children_in_order_recursively() -> None:
    items = [
        {"path": "0", "type": "text", "text": "hello", "supported": True},
        {
            "path": "1",
            "type": "image",
            "supported": True,
            "media": {
                "status": "available",
                "media_type": "image",
                "mime_type": "image/png",
                "size_bytes": 100,
                "access_url": "/nested/1/access",
            },
        },
        {"path": "2", "type": "text", "text": "world", "supported": True},
    ]
    out = _run(f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(_mixed_msg(items))})));")
    html = json.loads(out)
    # Deterministic ordering check: "hello" (item 0) must appear before the
    # nested image's access-url attribute (item 1), which must appear
    # before "world" (item 2) -- proves children are rendered in document
    # order, not just that all three happen to be present somewhere.
    assert 'data-rnd206-access-url="/nested/1/access"' in html
    idx_hello = html.index("hello")
    idx_image = html.index('data-rnd206-access-url="/nested/1/access"')
    idx_world = html.index("world")
    assert idx_hello < idx_image < idx_world
    assert "composite-wrap" in html


def test_mixed_unknown_child_shows_labeled_fallback_not_raw_json() -> None:
    items = [{"path": "0", "type": "some_future_type", "supported": False}]
    out = _run(f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(_mixed_msg(items))})));")
    html = json.loads(out)
    assert "some_future_type" not in html
    assert '"path"' not in html
    assert "composite-unknown" in html


def test_mixed_empty_items_does_not_crash() -> None:
    out = _run(f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(_mixed_msg([]))})));")
    html = json.loads(out)
    assert "composite-unknown" in html


def test_composite_node_depth_limit_reached_shows_safe_fallback() -> None:
    out = _run(
        """
var node={type:'text',text:'a-sentinel-value-that-must-not-leak'};
process.stdout.write(JSON.stringify(renderCompositeNode(node, 9)));
"""
    )
    html = json.loads(out)
    assert "a-sentinel-value-that-must-not-leak" not in html
    assert "composite-unknown" in html


def test_chatrecord_renders_summary_card_with_title_and_count() -> None:
    fields = {
        "title": "Team standup",
        "items": [
            {"path": "0", "type": "text", "text": "first message", "supported": True},
            {"path": "1", "type": "text", "text": "second message", "supported": True},
        ],
        "item_count": 2,
    }
    msg = _msg(
        "chatrecord",
        "structured",
        normalized_type="chatrecord",
        renderer_strategy="composite_view",
        structured_content={"fields": fields, "parse_warnings": []},
    )
    out = _run(f"process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));")
    html = json.loads(out)
    assert "Team standup" in html
    assert "chatrecord-card" in html
    assert "onclick=\"openChatrecordViewer(" in html


def test_chatrecord_viewer_renders_nested_sender_and_timestamp() -> None:
    node = {
        "fields": {"title": "Team standup"},
        "children": [
            {"path": "0", "type": "text", "text": "hi", "sender_name": "Bob", "timestamp": 1751702400000, "supported": True}
        ],
    }
    out = _run(
        f"""
viewerItems=[{{kind:'chatrecord',node:{json.dumps(node)}}}];
viewerIndex=-1;
var calls=[];
var bodyEl={{ innerHTML:'', querySelectorAll:function(sel){{ return []; }} }};
var rootEl={{
  style:{{}},
  querySelector:function(sel){{ return {{style:{{}}}}; }}
}};
document={{
  getElementById:function(id){{
    if(id==='rnd206-viewer')return rootEl;
    if(id==='rnd206-viewer-body')return bodyEl;
    return null;
  }},
  createElement:function(){{return {{}};}},
  body:{{appendChild:function(){{}}, style:{{}}}},
  addEventListener:function(){{}}
}};
viewerShow(0);
process.stdout.write(JSON.stringify(bodyEl.innerHTML));
"""
    )
    html = json.loads(out)
    assert "hi" in html
    assert "Bob" in html


# ---------------------------------------------------------------------------
# Viewer item registration (image click -> shared Viewer, not new tab)
# ---------------------------------------------------------------------------


def test_image_click_opens_shared_viewer_not_new_tab() -> None:
    """RND-206 (narrow remediation pass): top-level image now renders as a
    hydratable placeholder (no synchronous <a target="_blank"> at all) and,
    once hydrateRichMedia resolves it, becomes a real <button> whose click
    handler opens the shared Viewer at the registered index -- proven
    behaviorally by actually invoking swapRichMediaPlaceholder, not just
    grepping for an onclick substring in the pre-hydration placeholder."""
    msg = _msg("image", "image", media_status="available", media_access_url="/api/x/media/access")
    out = _run(
        f"""
timelineViewerItems=[];
var placeholderHtml=renderMessageBody({json.dumps(msg)});
var m = /data-rnd206-viewer-idx="([^"]+)"/.exec(placeholderHtml);
var openedWith = null;
openViewer = function(items, idx){{ openedWith = {{items: items, idx: idx}}; }};
document = {{ createElement: function(tag){{
  var attrs = {{}};
  return {{tagName: tag.toUpperCase(), setAttribute: function(k,v){{attrs[k]=String(v);}}, getAttribute: function(k){{return attrs[k];}}, appendChild: function(){{}}}};
}} }};
var el = {{getAttribute: function(k){{
  if (k === 'data-rnd206-kind') return 'image';
  if (k === 'data-rnd206-access-url') return '/api/x/media/access';
  if (k === 'data-rnd206-viewer-idx') return m[1];
  return null;
}}, parentNode: {{replaceChild: function(newEl){{ this.lastReplacedWith = newEl; }}}}}};
swapRichMediaPlaceholder(el, 'image', {{url:'/resolved.png'}});
var btn = el.parentNode.lastReplacedWith;
btn.onclick();
process.stdout.write(JSON.stringify({{
  placeholderHtml: placeholderHtml,
  viewerIdx: m ? m[1] : null,
  tagName: btn.tagName,
  openedWith: openedWith,
  registeredItem: timelineViewerItems[0]
}}));
"""
    )
    result = json.loads(out)
    assert 'target="_blank"' not in result["placeholderHtml"]
    assert result["viewerIdx"] == "0"
    assert result["tagName"] == "BUTTON"
    assert result["registeredItem"]["kind"] == "image"
    assert result["registeredItem"]["accessUrl"] == "/api/x/media/access"
    # clicking the hydrated button actually calls openViewer with the
    # shared timelineViewerItems array at the registered index -- proof
    # top-level image opens the SAME shared Viewer nested/emotion/video use.
    assert result["openedWith"]["idx"] == 0
    assert result["openedWith"]["items"][0]["kind"] == "image"
