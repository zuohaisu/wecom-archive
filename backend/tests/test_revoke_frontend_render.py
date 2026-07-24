"""
Tests for RND-201 — frontend rendering of revoke association state.

Executes the real embedded JS under Node (same technique as
test_unsupported_message_labels.py / test_message_type_registry.py):
renderMessageBody()'s new revoke_association_status dispatch
(renderRevokePlaceholder), and renderTimeline()'s "已撤回" secondary badge
for an already-revoked original message.

Scope: RND-201 only covers the basic revoked-state presentation this file
tests (inline original content + secondary badge, or a minimal placeholder
for an unresolved standalone revoke event) — the richer unified
Viewer/Renderer experience is RND-206 scope, not tested here.

Run (from backend/):
    pytest tests/test_revoke_frontend_render.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from app.main import _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON
from tests._rnd216_web_shims import review_console_js_source

_REVIEW_CONSOLE_JS = review_console_js_source()

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


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
        # RND-216: the MessageTypeRegistry IIFE now reads its `entries` off
        # a page-level RND216_MTR_ENTRIES global (injected by
        # templates/review_console.html ahead of the externalized
        # review-console.js) instead of an inlined JSON literal — this
        # bundle must define that global itself before the IIFE runs.
        f"var RND216_MTR_ENTRIES = {_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON};",
        _extract(
            r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"
        ),
        _extract(r"function renderRevokePlaceholder\(m\)\{.*?\n\}", "renderRevokePlaceholder()"),
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
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def _msg(**overrides) -> dict:
    base = {
        "msgid": "msg-1",
        "sender": "staff_alice",
        "sender_display_name": "Alice",
        "sender_raw_id": "staff_alice",
        "recipients": ["contact_zhangsan"],
        "recipient_display_names": ["Zhang San"],
        "recipient_raw_ids": ["contact_zhangsan"],
        "msgtime": 1751702400000,
        "msgtype": "text",
        "content_text": "hello",
        "roomid": None,
        "decrypt_status": "success",
        "media_type": "text",
        "media_status": None,
        "unsupported_reason": None,
        "media_url": None,
        "normalized_type": "text",
        "renderer_strategy": "text_body",
        "is_revoked": False,
        "revoked_at": None,
        "revoke_event_msgid": None,
        "revoke_association_status": None,
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


def _render_timeline(locale: str, msgs: list) -> str:
    return _run(
        f"""
I18N.setLocale({json.dumps(locale)});
var mode='staff', selEntityId=null, timelineHasOlder=false, timelineHistoryError=null;
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


# ---------------------------------------------------------------------------
# Standalone unresolved revoke event -- placeholder, no fabricated content
# ---------------------------------------------------------------------------


def test_zh_cn_pending_revoke_placeholder() -> None:
    msg = _msg(
        msgtype="revoke", normalized_type="revoke", media_type="structured",
        content_text=None, renderer_strategy="unsupported_placeholder",
        revoke_association_status="pending",
    )
    assert _render("zh-CN", msg) == '<div class="media-placeholder">撤回关联中</div>'


def test_zh_cn_original_missing_revoke_placeholder() -> None:
    msg = _msg(
        msgtype="revoke", normalized_type="revoke", media_type="structured",
        content_text=None, renderer_strategy="unsupported_placeholder",
        revoke_association_status="original_missing",
    )
    assert _render("zh-CN", msg) == '<div class="media-placeholder">原始消息不可用</div>'


def test_zh_cn_malformed_revoke_placeholder() -> None:
    msg = _msg(
        msgtype="revoke", normalized_type="revoke", media_type="structured",
        content_text=None, renderer_strategy="unsupported_placeholder",
        revoke_association_status="malformed",
    )
    assert _render("zh-CN", msg) == '<div class="media-placeholder">撤回事件异常</div>'


@pytest.mark.parametrize(
    "locale,status,expected",
    [
        ("zh-TW", "pending", "撤回關聯中"),
        ("zh-TW", "original_missing", "原始訊息不可用"),
        ("zh-TW", "malformed", "撤回事件異常"),
        ("en", "pending", "Revoke pending"),
        ("en", "original_missing", "Original message unavailable"),
        ("en", "malformed", "Malformed revoke event"),
    ],
)
def test_other_locales_revoke_placeholder(locale, status, expected) -> None:
    msg = _msg(
        msgtype="revoke", normalized_type="revoke", media_type="structured",
        content_text=None, renderer_strategy="unsupported_placeholder",
        revoke_association_status=status,
    )
    assert _render(locale, msg) == f'<div class="media-placeholder">{expected}</div>'


def test_pending_revoke_placeholder_never_shows_generic_unsupported_type_text() -> None:
    """Before RND-201, a revoke row fell through to the generic
    'unsupported message type' fallback (no placeholder.revoke i18n key
    existed). This must never regress -- a revoke-specific label is
    always shown, never the generic bucket text."""
    msg = _msg(
        msgtype="revoke", normalized_type="revoke", media_type="structured",
        content_text=None, renderer_strategy="unsupported_placeholder",
        revoke_association_status="pending",
    )
    html = _render("zh-CN", msg)
    assert "不支持未知消息类型" not in html
    assert "待支持消息类型" not in html


# ---------------------------------------------------------------------------
# Linked original -- normal content rendering, never the placeholder
# ---------------------------------------------------------------------------


def test_revoked_original_text_message_renders_its_real_content_not_a_placeholder() -> None:
    msg = _msg(
        content_text="this was later revoked",
        is_revoked=True, revoked_at=5000, revoke_event_msgid="revoke-msgid-1",
        revoke_association_status="linked",
    )
    html = _render("zh-CN", msg)
    assert html == "this was later revoked"


def test_ordinary_message_with_no_revoke_fields_renders_unchanged() -> None:
    msg = _msg(content_text="plain message")
    assert _render("zh-CN", msg) == "plain message"


# ---------------------------------------------------------------------------
# renderTimeline() -- secondary "已撤回" badge on the original's own row
# ---------------------------------------------------------------------------


def test_revoked_original_shows_secondary_badge_alongside_its_content() -> None:
    msg = _msg(
        msgid="m1", content_text="revoked but visible", is_revoked=True,
        revoked_at=5000, revoke_event_msgid="revoke-1", revoke_association_status="linked",
    )
    out = _render_timeline("zh-CN", [msg])
    assert "revoked but visible" in out
    assert "已撤回" in out


def test_ordinary_message_never_shows_revoked_badge() -> None:
    msg = _msg(msgid="m1", content_text="just a message")
    out = _render_timeline("zh-CN", [msg])
    assert "已撤回" not in out


def test_timeline_with_original_and_pending_revoke_shows_both_correctly_no_duplication() -> None:
    """Mirrors the real API contract: a linked original (folded, with
    badge) coexisting with an unrelated, separately-standalone pending
    revoke event (its own row, placeholder content) -- exactly the shape
    app.routers.conversations.get_conversation_messages returns."""
    original = _msg(
        msgid="m1", msgtime=1000, content_text="revoked message", is_revoked=True,
        revoked_at=1500, revoke_event_msgid="revoke-1", revoke_association_status="linked",
    )
    pending_event = _msg(
        msgid="revoke-2", msgtime=2000, msgtype="revoke", normalized_type="revoke",
        media_type="structured", content_text=None, renderer_strategy="unsupported_placeholder",
        revoke_association_status="pending",
    )
    out = _render_timeline("zh-CN", [original, pending_event])
    assert "revoked message" in out
    assert "已撤回" in out
    assert "撤回关联中" in out
    # Exactly two rows rendered -- no duplication/collapse of either.
    assert out.count('class="tl-row') == 2


# ---------------------------------------------------------------------------
# Never throws
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"revoke_association_status": "pending", "msgtype": "revoke", "normalized_type": "revoke"},
        {"revoke_association_status": "original_missing", "msgtype": "revoke", "normalized_type": "revoke"},
        {"revoke_association_status": "malformed", "msgtype": "revoke", "normalized_type": "revoke"},
        {"is_revoked": True, "revoke_association_status": "linked"},
        {"is_revoked": False, "revoke_association_status": None},
    ],
)
def test_renderer_does_not_throw_on_revoke_shaped_messages(overrides) -> None:
    msg = _msg(**overrides)
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
