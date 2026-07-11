"""
Tests for RND-173 — Standardized MessageType Registry and Unsupported
Message Placeholders.

Scope: the review console's embedded JS (_REVIEW_CONSOLE_HTML) only.
`renderMessageBody()` no longer decides unsupported/placeholder text with a
single hardcoded string — it resolves the message's msgtype through a new
`MessageTypeRegistry` (text/image keep their existing renderer branches;
video/voice/file/location/link/card/emotion/miniprogram/todo each get a
distinct, i18n-backed placeholder; anything not registered — including a
missing msgtype, which the backend already classifies as media_type
"unknown" — falls back to a safe generic placeholder). No backend, schema,
API-contract, media-download, or pagination changes; classify_media()
(RND-133/144/147) is untouched, and its "unsupported"/"unknown" media_type
buckets still exist — the registry decides which *placeholder text* to
show for the "unsupported" bucket instead of always using one generic
string.

These tests execute the real embedded JS under Node (same technique as
test_admin_group_participant_overflow.py / test_admin_auto_load_older.py /
test_i18n_foundation.py), so assertions exercise real behavior rather than
only pattern-matching source text.

Run (from backend/):
    pytest tests/test_message_type_registry.py -v
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
# Static presence checks
# ---------------------------------------------------------------------------


def test_message_type_registry_exists_in_console_html() -> None:
    assert "var MessageTypeRegistry=" in _REVIEW_CONSOLE_HTML


def test_render_message_body_references_the_registry() -> None:
    src = _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()")
    assert "MessageTypeRegistry" in src


# ---------------------------------------------------------------------------
# Bundle assembly for Node execution
# ---------------------------------------------------------------------------


def _bundle() -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        # MEDIA_LABELS/MEDIA_STATUS_LABELS below are computed once at
        # declaration time from whatever locale is active then (same
        # ordering constraint as _REVIEW_CONSOLE_HTML itself, where they're
        # declared once at page load and only refreshed via
        # rebuildMediaLabels() on an explicit language switch) — pin English
        # here before they're declared so this file's literal-English
        # assertions mean what they say.
        "I18N.setLocale('en');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\}", "pad()"),
        _extract(r"var MEDIA_LABELS=\{.*?\};", "MEDIA_LABELS"),
        _extract(r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"),
        _extract(
            r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"
        ),
        _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()"),
        # RND-187: renderTimeline() now calls hydrateMediaImages() after
        # every render — pull those in too so the bundle is self-contained.
        _extract(r"function loadMediaImage\(img\)\{.*?\n\}", "loadMediaImage()"),
        _extract(r"function onMediaImageError\(img\)\{.*?\n\}", "onMediaImageError()"),
        _extract(r"function showMediaError\(img\)\{.*?\n\}", "showMediaError()"),
        _extract(r"function hydrateMediaImages\(root\)\{.*?\n\}", "hydrateMediaImages()"),
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


def _msg(msgtype: str, media_type: str, **overrides) -> dict:
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
    }
    base.update(overrides)
    return base


def _render(msg: dict) -> str:
    out = _run(
        f"""
I18N.setLocale('en');
process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));
"""
    )
    return out


# ---------------------------------------------------------------------------
# Registry structure
# ---------------------------------------------------------------------------


def test_registry_has_entries_for_every_documented_msgtype() -> None:
    out = _run(
        "process.stdout.write(JSON.stringify(Object.keys(MessageTypeRegistry.entries).sort()));"
    )
    assert out == sorted(
        [
            "text",
            "image",
            "video",
            "voice",
            "file",
            "location",
            "link",
            "card",
            "emotion",
            "miniprogram",
            "todo",
        ]
    )


def test_resolve_returns_null_for_unregistered_or_missing_msgtype() -> None:
    out = _run(
        """
process.stdout.write(JSON.stringify({
  missing: MessageTypeRegistry.resolve(null),
  empty: MessageTypeRegistry.resolve(''),
  unknownType: MessageTypeRegistry.resolve('some_future_type')
}));
"""
    )
    assert out == {"missing": None, "empty": None, "unknownType": None}


def test_resolve_placeholder_only_matches_placeholder_category_entries() -> None:
    out = _run(
        """
process.stdout.write(JSON.stringify({
  text: MessageTypeRegistry.resolvePlaceholder('text'),
  image: MessageTypeRegistry.resolvePlaceholder('image'),
  video: MessageTypeRegistry.resolvePlaceholder('video') && MessageTypeRegistry.resolvePlaceholder('video').placeholderKey
}));
"""
    )
    assert out["text"] is None
    assert out["image"] is None
    assert out["video"] == "placeholder.video"


# ---------------------------------------------------------------------------
# Supported types unchanged
# ---------------------------------------------------------------------------


def test_text_renderer_unchanged() -> None:
    msg = _msg("text", "text", content_text="hello there")
    assert _render(msg) == "hello there"


def test_text_renderer_empty_text_placeholder_unchanged() -> None:
    msg = _msg("text", "text", content_text=None)
    assert _render(msg) == '<div class="media-placeholder">Empty text message</div>'


def test_image_renderer_preview_unchanged() -> None:
    """RND-187: <img src> is no longer set synchronously by
    renderMessageBody() — see test_unsupported_message_labels.py's
    test_zh_cn_image_preview_unchanged for the rationale. What must stay
    unchanged is that a preview element is still produced, carrying both
    the access-descriptor URL and the proxy fallback URL."""
    msg = _msg(
        "image",
        "image",
        media_status="available",
        media_access_url="/api/conversations/c1/messages/msg-1/media/access",
        media_url="/api/conversations/c1/messages/msg-1/media",
    )
    html = _render(msg)
    assert '<img class="media-preview"' in html
    assert 'data-access-url="/api/conversations/c1/messages/msg-1/media/access"' in html
    assert 'data-fallback-url="/api/conversations/c1/messages/msg-1/media"' in html


def test_image_renderer_not_downloaded_placeholder_unchanged() -> None:
    msg = _msg("image", "image", media_status="not_downloaded")
    html = _render(msg)
    assert "Image message" in html and "not downloaded" in html


# ---------------------------------------------------------------------------
# New placeholder types (RND-173)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msgtype,expected_text",
    [
        ("location", "Unsupported location message"),
        ("link", "Unsupported link message"),
        ("card", "Unsupported contact card message"),
        ("emotion", "Unsupported sticker message"),
        ("miniprogram", "Unsupported mini program message"),
        ("todo", "Unsupported to-do message"),
    ],
)
def test_new_type_shows_distinct_placeholder(msgtype: str, expected_text: str) -> None:
    msg = _msg(msgtype, "unsupported")
    html = _render(msg)
    assert html == f'<div class="media-placeholder">{expected_text}</div>'


def test_video_placeholder_is_distinct_and_readable() -> None:
    msg = _msg("video", "unsupported")
    html = _render(msg)
    assert html == '<div class="media-placeholder">Unsupported video message</div>'


def test_voice_placeholder_is_distinct_and_readable() -> None:
    msg = _msg("voice", "unsupported")
    html = _render(msg)
    assert html == '<div class="media-placeholder">Unsupported voice message</div>'


def test_file_placeholder_is_distinct_and_readable() -> None:
    msg = _msg("file", "unsupported")
    html = _render(msg)
    assert html == '<div class="media-placeholder">Unsupported file message</div>'


def test_different_unsupported_types_render_different_text() -> None:
    location_html = _render(_msg("location", "unsupported"))
    link_html = _render(_msg("link", "unsupported"))
    card_html = _render(_msg("card", "unsupported"))
    assert len({location_html, link_html, card_html}) == 3


# ---------------------------------------------------------------------------
# Unknown / missing registry entry fallback
# ---------------------------------------------------------------------------


def test_unknown_missing_msgtype_placeholder_unchanged() -> None:
    """Backend classify_media reports media_type='unknown' when msgtype is
    missing (RND-133) — this must keep using the pre-existing generic
    "unknown" copy, not a registry placeholder."""
    msg = _msg(None, "unknown")
    html = _render(msg)
    assert html == '<div class="media-placeholder">Unknown message type</div>'


def test_missing_registry_entry_falls_back_to_generic_unsupported_placeholder() -> None:
    """A msgtype the backend buckets as 'unsupported' but that isn't in
    MessageTypeRegistry (a brand-new WeCom type we haven't mapped yet) must
    still render a safe, generic placeholder rather than breaking."""
    msg = _msg("some_future_wecom_type", "unsupported")
    html = _render(msg)
    assert html == '<div class="media-placeholder">Unknown message type</div>'


def test_pre_existing_generic_unsupported_placeholder_text_unchanged() -> None:
    """RND-150 regression: a msgtype the registry itself owns for another
    purpose (here 'text') combined with a backend media_type of
    'unsupported' — an unrealistic combination used only to exercise the
    generic-fallback branch — must still route through the registry
    fallback (RND-177 updated its copy from "Unsupported message type" to
    "Unknown message type" to match the missing/unknown-msgtype branch)."""
    msg = _msg("text", "unsupported", content_text=None)
    html = _render(msg)
    assert html == '<div class="media-placeholder">Unknown message type</div>'


# ---------------------------------------------------------------------------
# Never throws / timeline keeps rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msgtype,media_type",
    [
        (None, "unknown"),
        ("", "unsupported"),
        (123, "unsupported"),
        ("weapp", "unsupported"),
        ("video", "video"),
        ("text", "text"),
    ],
)
def test_render_message_body_never_throws(msgtype, media_type) -> None:
    msg = _msg(msgtype, media_type)
    out = _run(
        f"""
try {{
  var html = renderMessageBody({json.dumps(msg)});
  process.stdout.write(JSON.stringify({{ok:true,html:html}}));
}} catch (e) {{
  process.stdout.write(JSON.stringify({{ok:false,error:String(e)}}));
}}
"""
    )
    assert out["ok"] is True, out


def test_timeline_continues_rendering_after_an_unknown_message() -> None:
    known_before = _msg("text", "text", content_text="before")
    known_before["msgid"] = "m1"
    known_before["msgtime"] = 1000
    unknown = _msg("brand_new_type", "unsupported")
    unknown["msgid"] = "m2"
    unknown["msgtime"] = 2000
    known_after = _msg("text", "text", content_text="after")
    known_after["msgid"] = "m3"
    known_after["msgtime"] = 3000

    out = _run(
        f"""
I18N.setLocale('en');
var mode='staff', selEntityId=null, timelineHasOlder=false, timelineHistoryError=null;
var timelineMsgs={json.dumps([known_before, unknown, known_after])};
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
    assert "after" in out
    assert "Unknown message type" in out


# ---------------------------------------------------------------------------
# i18n integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "locale,expected",
    [
        ("zh-CN", "不支持视频消息"),
        ("zh-TW", "不支援影片訊息"),
        ("en", "Unsupported video message"),
    ],
)
def test_placeholder_uses_i18n_per_locale(locale: str, expected: str) -> None:
    out = _run(
        f"""
I18N.setLocale({json.dumps(locale)});
process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(_msg('video', 'unsupported'))})));
"""
    )
    assert out == f'<div class="media-placeholder">{expected}</div>'


def test_placeholder_never_contains_hardcoded_chinese_in_registry_source() -> None:
    """The registry itself must only carry placeholderKey references — all
    translated copy stays in the Locale Registry (RND-157), never inlined
    here."""
    src = _extract(
        r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"
    )
    assert not re.search(r"[一-鿿]", src), src


# ---------------------------------------------------------------------------
# Registry drives the renderer (extensibility)
# ---------------------------------------------------------------------------


def test_adding_a_registry_entry_changes_rendering_with_no_renderer_code_change() -> None:
    """Mirrors RND-157's LocaleRegistry-extensibility test: registering a new
    msgtype (simulating 'Step 1/Step 2' in the RND-173 future-extensibility
    contract) must change renderMessageBody's output without touching
    renderMessageBody itself."""
    out = _run(
        """
I18N.setLocale('en');
var before = renderMessageBody({msgtype:'weapp_vote', media_type:'unsupported', content_text:null});
MessageTypeRegistry.entries.weapp_vote = {category:'placeholder', placeholderKey:'placeholder.card'};
var after = renderMessageBody({msgtype:'weapp_vote', media_type:'unsupported', content_text:null});
process.stdout.write(JSON.stringify({before:before, after:after}));
"""
    )
    assert out["before"] == '<div class="media-placeholder">Unknown message type</div>'
    assert out["after"] == '<div class="media-placeholder">Unsupported contact card message</div>'


def test_registry_fallback_is_exposed_and_used_for_unregistered_types() -> None:
    out = _run(
        """
process.stdout.write(JSON.stringify(MessageTypeRegistry.fallback.placeholderKey));
"""
    )
    assert out == "placeholder.unsupported"
