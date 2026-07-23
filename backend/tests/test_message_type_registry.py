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
        # RND-197 — structured card rendering, pulled in ahead of
        # renderMessageBody() since it calls renderStructuredCard().
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
        # RND-198 — interactive business card renderers
        _extract(r"function renderVoteCard\(m\)\{.*?\n\}", "renderVoteCard()"),
        _extract(r"function renderTodoCard\(m\)\{.*?\n\}", "renderTodoCard()"),
        _extract(r"function renderCollectCard\(m\)\{.*?\n\}", "renderCollectCard()"),
        _extract(r"function renderMeetingCard\(m\)\{.*?\n\}", "renderMeetingCard()"),
        _extract(r"function renderScheduleCard\(m\)\{.*?\n\}", "renderScheduleCard()"),
        _extract(r"function renderRedpacketCard\(m\)\{.*?\n\}", "renderRedpacketCard()"),
        _extract(r"function renderSwitchCorpCard\(m\)\{.*?\n\}", "renderSwitchCorpCard()"),
        _extract(r"function renderSystemCard\(m\)\{.*?\n\}", "renderSystemCard()"),
        # RND-210 — business-card (名片) renderer; the STRUCTURED_CARD_RENDERERS
        # map now references it, so it must be in the bundle for the map to evaluate.
        _extract(r"function renderCardMessage\(m\)\{.*?\n\}", "renderCardMessage()"),
        # RND-210: STRUCTURED_CARD_RENDERERS now also references these two
        # audio renderers — extracted for real (test_audio_doc_fallback_*
        # exercises renderAudioDocMessage's null-fields fallback path).
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\n\}", "pad()"),
        "function isSafeUrl(u){return typeof u==='string'&&/^https?:/i.test(u);}",
        _extract(r"function renderAudioArchiveMessage\(m\)\{.*?\n\}", "renderAudioArchiveMessage()"),
        _extract(r"function renderAudioDocMessage\(m\)\{.*?\n\}", "renderAudioDocMessage()"),
        _extract(r"var STRUCTURED_CARD_RENDERERS=\{.*?\n\};", "STRUCTURED_CARD_RENDERERS"),
        _extract(r"function renderStructuredCard\(m\)\{.*?\n\}", "renderStructuredCard()"),
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
        # RND-197 — Message Type Registry metadata + structured content.
        # normalized_type defaults to msgtype: true for every real
        # registry entry except weapp (normalized "miniprogram") — tests
        # exercising that alias pass normalized_type explicitly, exactly
        # as the real Timeline API does (see conversations.py's
        # describe_message_type()).
        "normalized_type": msgtype,
        "category": None,
        "support_status": None,
        "renderer_strategy": None,
        "display_label_key": "messageType.unknown",
        "structured_content": None,
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
            # RND-197
            "markdown",
            "news",
            "docmsg",
            "audio_archive",
            "audio_doc",
            # RND-198 interactive business types
            "system",
            "vote",
            "collect",
            "meeting",
            "schedule",
            "redpacket",
            "switch_corp",
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
    # RND-206: video is now category "media" (MEDIA_PREVIEW), not
    # "placeholder" -- it genuinely renders playback now, so
    # resolvePlaceholder() (which only ever matches category=="placeholder")
    # correctly no longer matches it. Its placeholderKey is still readable
    # via the broader MessageTypeRegistry.resolve(...).placeholderKey (used
    # by renderMessageBody's terminal fallback) -- see
    # test_resolve_still_exposes_placeholder_key_for_media_preview_types.
    out = _run(
        """
process.stdout.write(JSON.stringify({
  text: MessageTypeRegistry.resolvePlaceholder('text'),
  image: MessageTypeRegistry.resolvePlaceholder('image'),
  video: MessageTypeRegistry.resolvePlaceholder('video'),
  location: MessageTypeRegistry.resolvePlaceholder('location')
}));
"""
    )
    assert out["text"] is None
    assert out["image"] is None
    assert out["video"] is None
    # RND-197: location is now category "structured", not "placeholder" —
    # resolvePlaceholder must not match it (structured cards don't go
    # through this legacy lookup at all, see renderMessageBody's
    # structured_card branch, checked first).
    assert out["location"] is None


def test_resolve_still_exposes_placeholder_key_for_media_preview_types() -> None:
    # RND-206 QA fix: MessageTypeRegistry.resolve() (unlike the narrower
    # resolvePlaceholder()) still exposes .placeholderKey for a
    # MEDIA_PREVIEW type -- this is what renderMessageBody's terminal
    # fallback reads so a message that reaches that line in an unexpected
    # shape still gets video/voice/file/emotion's own label instead of the
    # generic "unknown message type" text.
    out = _run(
        """
process.stdout.write(JSON.stringify({
  video: MessageTypeRegistry.resolve('video').placeholderKey,
  voice: MessageTypeRegistry.resolve('voice').placeholderKey,
  file: MessageTypeRegistry.resolve('file').placeholderKey,
  emotion: MessageTypeRegistry.resolve('emotion').placeholderKey
}));
"""
    )
    assert out == {
        "video": "placeholder.video",
        "voice": "placeholder.voice",
        "file": "placeholder.file",
        "emotion": "placeholder.emotion",
    }


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
    test_zh_cn_image_preview_unchanged for the rationale. RND-206 (narrow
    remediation pass): top-level image now renders through the same
    hydratable-placeholder contract as nested/video/voice/file/emotion
    (data-rnd206-kind="image" + data-rnd206-access-url), resolved by the
    shared hydrateRichMedia/loadRichMedia/MediaAccessCache chain instead of
    the retired standalone loadMediaImage(). What must stay unchanged is
    that a real, hydratable preview placeholder carrying the correct
    access-descriptor URL is still produced."""
    msg = _msg(
        "image",
        "image",
        media_status="available",
        media_access_url="/api/conversations/c1/messages/msg-1/media/access",
        media_url="/api/conversations/c1/messages/msg-1/media",
    )
    html = _render(msg)
    assert 'data-rnd206-kind="image"' in html
    assert 'data-rnd206-access-url="/api/conversations/c1/messages/msg-1/media/access"' in html


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
        ("emotion", "Unsupported sticker message"),
    ],
)
def test_new_type_shows_distinct_placeholder(msgtype: str, expected_text: str) -> None:
    """RND-197: location/link/card/miniprogram moved off this legacy
    generic-placeholder path onto dedicated structured cards — see the
    "Structured cards (RND-197)" section below for their coverage.
    RND-198: todo is now SUPPORTED (structured card), removed from
    this legacy placeholder test. emotion remains UNSUPPORTED."""
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
    emotion_html = _render(_msg("emotion", "unsupported"))
    video_html = _render(_msg("video", "unsupported"))
    assert len({emotion_html, video_html}) == 2


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
var before = renderMessageBody({msgtype:'weapp_vote', normalized_type:'weapp_vote', media_type:'unsupported', content_text:null});
MessageTypeRegistry.entries.weapp_vote = {category:'placeholder', placeholderKey:'placeholder.card'};
var after = renderMessageBody({msgtype:'weapp_vote', normalized_type:'weapp_vote', media_type:'unsupported', content_text:null});
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


# ---------------------------------------------------------------------------
# Structured cards (RND-197). renderMessageBody() dispatches to
# renderStructuredCard() whenever m.renderer_strategy === 'structured_card'
# (checked before the legacy resolvePlaceholder() path) — link/location/
# markdown/news/miniprogram get full field-based cards; card/docmsg/
# audio_doc get the shared generic structured fallback (no field
# extraction attempted — see app.structured_message_parser).
# ---------------------------------------------------------------------------


def _structured_msg(normalized_type: str, fields, warnings=None, **overrides) -> dict:
    msg = _msg(
        overrides.pop("msgtype", normalized_type),
        "structured",
        renderer_strategy="structured_card",
        normalized_type=normalized_type,
        display_label_key=overrides.pop("display_label_key", f"messageType.{normalized_type}"),
        structured_content=(
            None if fields is None else {"fields": fields, "parse_warnings": warnings or []}
        ),
    )
    msg.update(overrides)
    return msg


def test_link_card_renders_title_description_url_and_image() -> None:
    msg = _structured_msg(
        "link",
        {
            "title": "Example",
            "description": "An example link",
            "url": "https://example.com/page",
            "image_url": "https://example.com/thumb.jpg",
        },
    )
    html = _render(msg)
    assert "structured-card-link" in html
    assert "Example" in html
    assert "An example link" in html
    assert 'href="https://example.com/page"' in html
    assert 'src="https://example.com/thumb.jpg"' in html
    assert 'target="_blank"' in html and "noopener" in html


def test_link_card_missing_title_falls_back_to_hostname() -> None:
    msg = _structured_msg(
        "link", {"title": None, "description": None, "url": "https://example.com/page", "image_url": None}
    )
    html = _render(msg)
    assert "example.com" in html


def test_link_card_missing_url_shows_degraded_unavailable_state() -> None:
    msg = _structured_msg(
        "link", {"title": "Example", "description": None, "url": None, "image_url": None}
    )
    html = _render(msg)
    assert "Link unavailable" in html
    assert "href=" not in html


def test_link_card_never_renders_raw_json() -> None:
    msg = _structured_msg(
        "link", {"title": "Example", "description": None, "url": "https://example.com", "image_url": None}
    )
    html = _render(msg)
    assert "{" not in html and "}" not in html


@pytest.mark.parametrize(
    "fields,expected_substring",
    [
        ({"name": "Office", "address": "Beijing", "latitude": 1.0, "longitude": 2.0, "zoom": None}, "Office"),
        ({"name": None, "address": "Beijing", "latitude": None, "longitude": None, "zoom": None}, "Beijing"),
        ({"name": None, "address": None, "latitude": 1.0, "longitude": 2.0, "zoom": None}, "1.000000"),
        ({"name": None, "address": None, "latitude": None, "longitude": None, "zoom": None}, "Unknown location"),
    ],
)
def test_location_card_fallback_ladder(fields, expected_substring) -> None:
    """name -> address -> coordinates -> localized unknown, per ticket."""
    msg = _structured_msg("location", fields)
    html = _render(msg)
    assert expected_substring in html


def test_markdown_card_renders_bold_and_lists() -> None:
    msg = _structured_msg("markdown", {"content": "**bold** text\n- item1\n- item2"})
    html = _render(msg)
    assert "<strong>bold</strong>" in html
    assert "<ul>" in html and "<li>item1</li>" in html and "<li>item2</li>" in html


def test_markdown_card_renders_safe_link() -> None:
    msg = _structured_msg("markdown", {"content": "[click here](https://example.com)"})
    html = _render(msg)
    assert '<a href="https://example.com"' in html
    assert "click here" in html


def test_markdown_card_unsafe_link_never_becomes_an_href() -> None:
    msg = _structured_msg("markdown", {"content": "[bad](javascript:evil)"})
    html = _render(msg)
    assert "javascript:" not in html
    assert "<a " not in html


def test_markdown_card_escapes_raw_html() -> None:
    msg = _structured_msg("markdown", {"content": "<script>alert(1)</script>"})
    html = _render(msg)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_markdown_card_empty_content_shows_degraded_state() -> None:
    msg = _structured_msg("markdown", {"content": None})
    html = _render(msg)
    assert "Empty Markdown message" in html


def test_news_card_renders_multiple_articles_in_source_order() -> None:
    msg = _structured_msg(
        "news",
        {
            "articles": [
                {"title": "First", "description": "d1", "url": "https://example.com/1", "image_url": None},
                {"title": "Second", "description": "d2", "url": "https://example.com/2", "image_url": None},
            ]
        },
    )
    html = _render(msg)
    assert html.index("First") < html.index("Second")


def test_news_card_missing_image_does_not_break_layout() -> None:
    msg = _structured_msg(
        "news", {"articles": [{"title": "A", "description": None, "url": None, "image_url": None}]}
    )
    html = _render(msg)
    assert "<img" not in html
    assert "A" in html


def test_news_card_missing_title_uses_localized_fallback() -> None:
    msg = _structured_msg(
        "news", {"articles": [{"title": None, "description": None, "url": None, "image_url": None}]}
    )
    html = _render(msg)
    assert "Untitled article" in html


def test_news_card_empty_article_list_shows_localized_empty_state() -> None:
    msg = _structured_msg("news", {"articles": []})
    html = _render(msg)
    assert "No articles" in html


def test_miniprogram_card_renders_title_and_never_treats_pagepath_as_a_link() -> None:
    """RND-197 requirement: pagepath is an internal mini-program route,
    never a clickable browser URL."""
    msg = _structured_msg(
        "miniprogram",
        {
            "title": "My Mini Program",
            "display_name": None,
            "appid": "wx123",
            "username": "gh_abc",
            "pagepath": "pages/index/index",
            "icon_url": None,
        },
    )
    html = _render(msg)
    assert "My Mini Program" in html
    assert "pages/index/index" not in html  # pagepath never surfaced as visible/clickable content
    assert "href=" not in html


def test_miniprogram_card_fixes_the_weapp_normalized_type_key_mismatch() -> None:
    """Regression test for the documented RND-196 bug: the wire msgtype is
    the raw 'weapp', but MessageTypeRegistry/rendering must key off
    normalized_type ('miniprogram') — this used to silently miss and fall
    to the generic fallback."""
    msg = _structured_msg(
        "miniprogram",
        {"title": "MiniApp", "display_name": None, "appid": "wx1", "username": None, "pagepath": None, "icon_url": None},
        msgtype="weapp",
    )
    html = _render(msg)
    assert "structured-card-miniprogram" in html
    assert "MiniApp" in html


@pytest.mark.parametrize(
    "normalized_type,display_label_key,expected_label",
    [
        ("docmsg", "messageType.docmsg", "Document message"),
        ("audio_doc", "messageType.audioDoc", "Audio document message"),
    ],
)
def test_low_confidence_types_render_generic_structured_fallback(
    normalized_type, display_label_key, expected_label
) -> None:
    """docmsg/audio_doc never get field extraction (no confirmed schema) —
    they must show the type label plus a clear unavailable line, never raw
    JSON. (card is RND-210: it now extracts corpname+userid and renders via
    renderCardMessage — covered by test_rnd_210_*.py, not this fallback.)"""
    msg = _structured_msg(normalized_type, None, display_label_key=display_label_key)
    html = _render(msg)
    assert "structured-card-fallback" in html
    assert expected_label in html
    assert "{" not in html and "}" not in html


def test_audio_doc_fallback_uses_the_playback_specific_copy() -> None:
    msg = _structured_msg("audio_doc", None, display_label_key="messageType.audioDoc")
    html = _render(msg)
    assert "Playback not supported" in html


def test_structured_content_null_degrades_to_generic_fallback_not_a_crash() -> None:
    """Malformed/historical row: structured_content itself is null (e.g. a
    row from before this migration) — must still render a safe card, not
    raw JSON, not throw. link/location/miniprogram fall through to the
    shared generic structured fallback; markdown/news have their own
    dedicated empty-state markup (both are equally safe/non-crashing)."""
    for normalized_type in ("link", "location", "markdown", "news", "miniprogram"):
        msg = _structured_msg(normalized_type, None, display_label_key=f"messageType.{normalized_type}")
        html = _render(msg)
        assert "structured-card" in html
        assert "degraded" in html
        assert "{" not in html and "}" not in html


def test_structured_card_dispatch_never_throws_for_any_input() -> None:
    out = _run(
        """
try {
  var results = [
    renderMessageBody({media_type:'structured', renderer_strategy:'structured_card', normalized_type:'link', display_label_key:'messageType.link', structured_content:undefined}),
    renderMessageBody({media_type:'structured', renderer_strategy:'structured_card', normalized_type:'totally_unmapped_type', display_label_key:'messageType.unknown', structured_content:null}),
    renderMessageBody({media_type:'structured', renderer_strategy:'structured_card', normalized_type:'news', display_label_key:'messageType.news', structured_content:{fields:{articles:'not-a-list'}, parse_warnings:[]}}),
  ];
  process.stdout.write(JSON.stringify({ok:true}));
} catch (e) {
  process.stdout.write(JSON.stringify({ok:false,error:String(e)}));
}
"""
    )
    assert out["ok"] is True, out


@pytest.mark.parametrize(
    "locale,expected",
    [
        ("zh-CN", "位置未知"),
        ("zh-TW", "位置未知"),
        ("en", "Unknown location"),
    ],
)
def test_structured_card_copy_uses_i18n_per_locale(locale: str, expected: str) -> None:
    msg = _structured_msg(
        "location", {"name": None, "address": None, "latitude": None, "longitude": None, "zoom": None}
    )
    out = _run(
        f"""
I18N.setLocale({json.dumps(locale)});
process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));
"""
    )
    assert expected in out
