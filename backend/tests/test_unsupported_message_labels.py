"""
Focused tests for RND-177 — Show specific unsupported message type labels in
the admin timeline.

Scope: RND-173 (MessageTypeRegistry) already routed every unsupported
msgtype through `renderMessageBody()` -> `MessageTypeRegistry` ->
`placeholderKey` -> `I18N.t()`. That mechanism is unchanged here. RND-177
only replaces the *translation values* the registry's placeholderKeys
resolve to (app/assets/i18n.js) so each unsupported type gets its own
readable Chinese label instead of the generic "【待支持消息类型】" bucket, and
does the same for the missing/unregistered fallback (media.unknownType /
placeholder.unsupported). No renderer branching, registry shape, backend,
schema, or API-contract changes.

RND-197 note: location/link/card/miniprogram were moved off this legacy
placeholder mechanism onto dedicated structured cards (see
test_message_type_registry.py's "Structured cards" section) and were
removed from the exact-label tables below. The broader "no legacy 【】
garbage text" guard tests below still reference them deliberately — that
property must hold regardless of which mechanism (placeholder vs
structured card) ends up rendering a given type; this file's own `_msg()`
does not set renderer_strategy/structured_content, so those particular
assertions exercise the legacy generic-fallback path for those four types,
not their new structured-card rendering.

These tests execute the real embedded JS under Node (same technique as
test_message_type_registry.py / test_i18n_foundation.py), so assertions
exercise real behavior rather than only pattern-matching source text.

Run (from backend/):
    pytest tests/test_unsupported_message_labels.py -v
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

GENERIC_LEGACY_STRING = "待支持消息类型"


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_JS, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_JS"
    return match.group(0)


def _bundle() -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\}", "pad()"),
        _extract(r"var MEDIA_LABELS=\{.*?\};", "MEDIA_LABELS"),
        _extract(r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"),
        # Archive Console v2 (design import): graded media_status placeholder.
        _extract(
            r"var MEDIA_STATUS_DOT=\{.*?\nfunction renderGradedMediaPlaceholder\(typeLabel,status\)\{.*?\n\}",
            "renderGradedMediaPlaceholder",
        ),
        f"var RND216_MTR_ENTRIES = {_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON};",
        _extract(
            r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"
        ),
        # RND-206: renderMessageBody()/renderTimeline() now also depend on
        # the MediaAccessCache/Viewer/rich-media/composite renderer block —
        # pull the whole contiguous block in so the bundle is self-contained.
        _extract(
            r"var MediaAccessCache=\(function\(\)\{.*?\nfunction renderCompositeMessage\(m\)\{.*?\n\}",
            "RND-206 rich-media/composite block",
        ),
        _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()"),
        _extract(r"function safeRenderMessageBody\(m\)\{.*?\n\}", "safeRenderMessageBody()"),
        _extract(r"function timelineSignature\(msgs\)\{.*?\n\}", "timelineSignature()"),
        _extract(r"function timelineRowHtml\(m\)\{.*?\n\}", "timelineRowHtml()"),
        _extract(r"function renderTimeline\(scrollToBottom\)\{.*?\n\}", "renderTimeline()"),
    ]
    return "\n".join(parts)


def _run(js_body: str) -> object:
    assert NODE, "node executable not found"
    harness = f"""
{_bundle()}
{js_body}
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def _msg(msgtype, media_type, **overrides) -> dict:
    base = {
        "msgid": "msg-1",
        "sender": "staff_alice",
        "sender_display_name": "Alice",
        "sender_raw_id": "staff_alice",
        "recipients": ["contact_zhangsan"],
        "recipient_display_names": ["Zhang San"],
        "recipient_raw_ids": ["contact_zhangsan"],
        "msgtime": 1751702400000,
        "msgtype": msgtype,
        "content_text": None,
        "roomid": None,
        "decrypt_status": "success",
        "media_type": media_type,
        "media_status": None,
        "unsupported_reason": None,
        "media_url": None,
        # RND-197: normalized_type defaults to msgtype, matching every
        # real registry entry except the weapp/miniprogram alias — see
        # test_message_type_registry.py's _msg() for the full rationale.
        # renderMessageBody()'s legacy placeholder path now resolves by
        # normalized_type, not raw msgtype.
        "normalized_type": msgtype,
    }
    base.update(overrides)
    return base


def _render(locale: str, msg: dict) -> str:
    return _run(
        f"""
I18N.setLocale({json.dumps(locale)});
process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));
"""
    )


# ---------------------------------------------------------------------------
# zh-CN mapping table — the product requirement's exact wording
# ---------------------------------------------------------------------------

# RND-197: location/link/card/miniprogram moved off this legacy
# generic-placeholder mechanism onto dedicated structured cards (see
# test_message_type_registry.py's "Structured cards" section for their
# label/copy coverage) — they are intentionally no longer in these
# tables. video/voice/file/emotion/todo remain UNSUPPORTED_PLACEHOLDER,
# unchanged.
ZH_CN_LABELS = {
    "video": "不支持视频消息",
    "voice": "不支持语音消息",
    "file": "不支持文件消息",
    "emotion": "不支持表情消息",
}


@pytest.mark.parametrize("msgtype,expected", sorted(ZH_CN_LABELS.items()))
def test_zh_cn_shows_specific_unsupported_label(msgtype: str, expected: str) -> None:
    html = _render("zh-CN", _msg(msgtype, "unsupported"))
    assert html == f'<div class="media-placeholder">{expected}</div>'


def test_zh_cn_unknown_msgtype_shows_unknown_label() -> None:
    """Missing msgtype (backend media_type='unknown', RND-133)."""
    html = _render("zh-CN", _msg(None, "unknown"))
    assert html == '<div class="media-placeholder">不支持未知消息类型</div>'


def test_zh_cn_unregistered_msgtype_shows_unknown_label() -> None:
    """A future WeCom msgtype not yet in MessageTypeRegistry."""
    html = _render("zh-CN", _msg("some_future_wecom_type", "unsupported"))
    assert html == '<div class="media-placeholder">不支持未知消息类型</div>'


# ---------------------------------------------------------------------------
# Guard: the old generic bucket text must never appear for these cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msgtype", ["video", "miniprogram", "voice", "file", "location", "link", "card", "emotion", "todo"]
)
def test_no_generic_legacy_placeholder_for_known_unsupported_types(msgtype: str) -> None:
    html = _render("zh-CN", _msg(msgtype, "unsupported"))
    assert GENERIC_LEGACY_STRING not in html
    assert "【" not in html and "】" not in html


def test_no_generic_legacy_placeholder_for_unknown_or_unregistered_types() -> None:
    for msg in (_msg(None, "unknown"), _msg("some_future_wecom_type", "unsupported")):
        html = _render("zh-CN", msg)
        assert GENERIC_LEGACY_STRING not in html


def test_no_generic_legacy_placeholder_anywhere_in_a_rendered_timeline() -> None:
    """End-to-end guard across renderTimeline(), not just renderMessageBody(),
    covering every known unsupported type plus an unregistered one in a
    single active timeline render."""
    msgtypes = [
        "video",
        "miniprogram",
        "voice",
        "file",
        "location",
        "link",
        "card",
        "emotion",
        "todo",
        "some_future_wecom_type",
    ]
    msgs = []
    for i, mt in enumerate(msgtypes):
        m = _msg(mt, "unsupported")
        m["msgid"] = f"m{i}"
        m["msgtime"] = 1000 + i
        msgs.append(m)

    out = _run(
        f"""
I18N.setLocale('zh-CN');
var mode='staff', selEntityId=null, timelineHasOlder=false, timelineHistoryError=null;
var auditMode=false, selectedMsgId=null;
var timelineMsgs={json.dumps(msgs)};
var capturedHtml=null;
var timelineBodyEl={{
  get innerHTML(){{return capturedHtml;}},
  set innerHTML(v){{capturedHtml=v;}},
  scrollHeight:0, scrollTop:0,
  querySelectorAll:function(){{return [];}}
}};
var document={{getElementById:function(id){{
  if(id==='timeline-body')return timelineBodyEl;
  throw new Error('unexpected getElementById('+id+')');
}}}};
renderTimeline(false);
process.stdout.write(JSON.stringify(capturedHtml));
"""
    )
    assert GENERIC_LEGACY_STRING not in out
    assert "【" not in out and "】" not in out


# ---------------------------------------------------------------------------
# Direct / group timeline consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("msgtype", ["video", "voice", "location", "todo"])
def test_direct_and_group_timeline_show_the_same_label(msgtype: str) -> None:
    direct_html = _render("zh-CN", _msg(msgtype, "unsupported", roomid=None))
    group_html = _render("zh-CN", _msg(msgtype, "unsupported", roomid="room-1"))
    assert direct_html == group_html


def test_voice_not_downloaded_status_renders_specific_placeholder() -> None:
    # RND-206: voice now renders real playback once media_status=="available"
    # (see render_voice_preview()); a not-yet-downloaded voice message shows
    # the same type+status placeholder pattern image already used
    # ("语音消息 · 未下载") instead of the old generic "unsupported" copy.
    msg = _msg(
        "voice",
        "voice",
        media_status="not_downloaded",
        content_text=None,
        # RND-206: renderer_strategy is now the registry-driven gate for
        # the media-preview dispatch (message_type_registry.py) — a real
        # TimelineMessageOut row always carries this; set it explicitly
        # here since this test hand-builds the message.
        renderer_strategy="media_preview",
    )
    html = _render("zh-CN", msg)
    # Archive Console v2 (design import): not_downloaded is now a GRADED
    # placeholder (icon dot + explanatory reason line) instead of one flat
    # "label · status" line -- the type+status text itself is unchanged,
    # just no longer the whole of the markup, so this checks the substring
    # rather than exact HTML equality.
    assert "语音消息 · 未下载" in html
    assert "media-placeholder-reason" in html
    assert GENERIC_LEGACY_STRING not in html


def test_timeline_continues_rendering_before_and_after_voice() -> None:
    before = _msg("text", "text", content_text="before")
    before["msgid"] = "m-before"
    before["msgtime"] = 1000
    voice = _msg(
        "voice",
        "voice",
        media_status="not_downloaded",
        content_text=None,
        renderer_strategy="media_preview",
    )
    voice["msgid"] = "m-voice"
    voice["msgtime"] = 2000
    after = _msg("text", "text", content_text="after")
    after["msgid"] = "m-after"
    after["msgtime"] = 3000

    out = _run(
        f"""
I18N.setLocale('zh-CN');
var mode='staff', selEntityId=null, timelineHasOlder=false, timelineHistoryError=null;
var auditMode=false, selectedMsgId=null;
var timelineMsgs={json.dumps([before, voice, after])};
var capturedHtml=null;
var timelineBodyEl={{
  get innerHTML(){{return capturedHtml;}},
  set innerHTML(v){{capturedHtml=v;}},
  scrollHeight:0, scrollTop:0,
  querySelectorAll:function(){{return [];}}
}};
var document={{getElementById:function(id){{
  if(id==='timeline-body')return timelineBodyEl;
  throw new Error('unexpected getElementById('+id+')');
}}}};
renderTimeline(false);
process.stdout.write(JSON.stringify(capturedHtml));
"""
    )
    assert "before" in out
    assert "语音消息 · 未下载" in out  # RND-206: see test_voice_not_downloaded_status_renders_specific_placeholder
    assert "after" in out
    assert GENERIC_LEGACY_STRING not in out
    assert "【" not in out and "】" not in out


def test_group_context_and_sender_metadata_survive_unsupported_rendering() -> None:
    msg = _msg("video", "unsupported", roomid="room-1")
    msg["msgid"] = "m1"
    msg["msgtime"] = 1000

    out = _run(
        f"""
I18N.setLocale('zh-CN');
var mode='staff', selEntityId=null, timelineHasOlder=false, timelineHistoryError=null;
var auditMode=false, selectedMsgId=null;
var timelineMsgs={json.dumps([msg])};
var capturedHtml=null;
var timelineBodyEl={{
  get innerHTML(){{return capturedHtml;}},
  set innerHTML(v){{capturedHtml=v;}},
  scrollHeight:0, scrollTop:0,
  querySelectorAll:function(){{return [];}}
}};
var document={{getElementById:function(id){{
  if(id==='timeline-body')return timelineBodyEl;
  throw new Error('unexpected getElementById('+id+')');
}}}};
renderTimeline(false);
process.stdout.write(JSON.stringify(capturedHtml));
"""
    )
    assert "不支持视频消息" in out
    assert "Alice" in out  # sender metadata
    assert "群" in out  # group badge (timeline.groupBadge, zh-CN)


# ---------------------------------------------------------------------------
# Known supported types must not regress
# ---------------------------------------------------------------------------


def test_zh_cn_text_rendering_unchanged() -> None:
    msg = _msg("text", "text", content_text="你好")
    assert _render("zh-CN", msg) == "你好"


def test_zh_cn_image_preview_unchanged() -> None:
    """RND-187: renderMessageBody() no longer sets <img src> synchronously
    — the actual URL comes from a post-render fetch of the media access
    descriptor. RND-206 (narrow remediation pass): top-level image now
    renders through the same shared hydratable-placeholder contract as
    nested/video/voice/file/emotion (data-rnd206-kind="image" +
    data-rnd206-access-url), resolved by hydrateRichMedia/loadRichMedia/
    MediaAccessCache -- see test_rnd_206_top_level_image.py for the full
    behavioral hydration coverage this replaces. What must stay unchanged
    here is that a real, hydratable image preview placeholder is still
    produced and still exposes the access-descriptor URL."""
    msg = _msg(
        "image",
        "image",
        media_status="available",
        media_access_url="/api/conversations/c1/messages/msg-1/media/access",
        media_url="/api/conversations/c1/messages/msg-1/media",
    )
    html = _render("zh-CN", msg)
    assert 'data-rnd206-kind="image"' in html
    assert 'data-rnd206-access-url="/api/conversations/c1/messages/msg-1/media/access"' in html


def test_zh_cn_image_not_downloaded_placeholder_unchanged() -> None:
    msg = _msg("image", "image", media_status="not_downloaded")
    html = _render("zh-CN", msg)
    assert "图片消息" in html and "未下载" in html


# ---------------------------------------------------------------------------
# zh-TW and en must not be left with missing/stale keys
# ---------------------------------------------------------------------------

# RND-197: see the ZH_CN_LABELS comment above — location/link/card/
# miniprogram intentionally excluded here too.
ZH_TW_LABELS = {
    "video": "不支援影片訊息",
    "voice": "不支援語音訊息",
    "file": "不支援檔案訊息",
    "emotion": "不支援表情訊息",
}

EN_LABELS = {
    "video": "Unsupported video message",
    "voice": "Unsupported voice message",
    "file": "Unsupported file message",
    "emotion": "Unsupported sticker message",
}


@pytest.mark.parametrize("msgtype,expected", sorted(ZH_TW_LABELS.items()))
def test_zh_tw_shows_specific_unsupported_label(msgtype: str, expected: str) -> None:
    html = _render("zh-TW", _msg(msgtype, "unsupported"))
    assert html == f'<div class="media-placeholder">{expected}</div>'


@pytest.mark.parametrize("msgtype,expected", sorted(EN_LABELS.items()))
def test_en_shows_specific_unsupported_label(msgtype: str, expected: str) -> None:
    html = _render("en", _msg(msgtype, "unsupported"))
    assert html == f'<div class="media-placeholder">{expected}</div>'


def test_zh_tw_unknown_fallback_updated() -> None:
    html = _render("zh-TW", _msg(None, "unknown"))
    assert html == '<div class="media-placeholder">不支援未知訊息類型</div>'


def test_en_unknown_fallback_updated() -> None:
    html = _render("en", _msg(None, "unknown"))
    assert html == '<div class="media-placeholder">Unknown message type</div>'


# ---------------------------------------------------------------------------
# Registry / i18n wiring — no scattered msgtype conditionals in the renderer
# ---------------------------------------------------------------------------


def test_renderer_source_has_no_added_msgtype_conditionals() -> None:
    """RND-177 must not introduce new per-msgtype if/else branches — the
    label still has to come from MessageTypeRegistry's placeholderKey via
    I18N.t(), same as RND-173."""
    src = _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()")
    msgtype_conditionals = re.findall(r"m\.msgtype\s*===", src)
    assert msgtype_conditionals == []


def test_labels_are_driven_by_registry_placeholder_key_not_hardcoded() -> None:
    """Changing a registry entry's placeholderKey changes the rendered text
    with zero renderer changes — proves the label is metadata-driven."""
    out = _run(
        """
I18N.setLocale('zh-CN');
var before = renderMessageBody({msgtype:'video', normalized_type:'video', media_type:'unsupported', content_text:null});
var originalKey = MessageTypeRegistry.entries.video.placeholderKey;
MessageTypeRegistry.entries.video.placeholderKey = 'placeholder.card';
var after = renderMessageBody({msgtype:'video', normalized_type:'video', media_type:'unsupported', content_text:null});
MessageTypeRegistry.entries.video.placeholderKey = originalKey;
process.stdout.write(JSON.stringify({before:before, after:after}));
"""
    )
    assert out["before"] == '<div class="media-placeholder">不支持视频消息</div>'
    assert out["after"] == '<div class="media-placeholder">不支持名片消息</div>'


# ---------------------------------------------------------------------------
# Never throws / never blanks the timeline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msgtype,media_type",
    [
        (None, "unknown"),
        ("", "unsupported"),
        ("weapp", "unsupported"),
        ("some_future_wecom_type", "unsupported"),
    ],
)
def test_renderer_does_not_throw_on_unknown_type(msgtype, media_type) -> None:
    msg = _msg(msgtype, media_type)
    out = _run(
        f"""
I18N.setLocale('zh-CN');
try {{
  var html = renderMessageBody({json.dumps(msg)});
  process.stdout.write(JSON.stringify({{ok:true,html:html}}));
}} catch (e) {{
  process.stdout.write(JSON.stringify({{ok:false,error:String(e)}}));
}}
"""
    )
    assert out["ok"] is True, out
