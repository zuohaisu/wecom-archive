"""
Frontend rendering tests for RND-198 — business cards, system event cards,
and the unknown sys subtype i18n fallback fix.

Executes the real embedded JS from app.main._REVIEW_CONSOLE_HTML under
Node (same technique as test_message_type_registry.py /
test_unsupported_message_labels.py), so assertions exercise live behavior.

Run (from backend/):
    pytest tests/test_rnd_198_frontend.py -v
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


def _bundle() -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('en');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\}", "pad()"),
        _extract(r"var MEDIA_LABELS=\{.*?\};", "MEDIA_LABELS"),
        _extract(r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"),
        # Archive Console v2 (design import): the static media_status
        # placeholder renderMessageBody falls back to is now graded by
        # recoverability instead of one flat "label · status" line.
        _extract(
            r"var MEDIA_STATUS_DOT=\{.*?\nfunction renderGradedMediaPlaceholder\(typeLabel,status\)\{.*?\n\}",
            "renderGradedMediaPlaceholder",
        ),
        # RND-216: the MessageTypeRegistry IIFE now reads its `entries` off
        # a page-level RND216_MTR_ENTRIES global (injected by
        # templates/review_console.html ahead of the externalized
        # review-console.js) instead of an inlined JSON literal — this
        # bundle must define that global itself before the IIFE runs.
        f"var RND216_MTR_ENTRIES = {_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON};",
        _extract(
            r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"
        ),
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
        _extract(r"function sphfeedTypeLabel\(feedType\)\{.*?\n\}", "sphfeedTypeLabel()"),
        _extract(r"function renderSphfeedCard\(m\)\{.*?\n\}", "renderSphfeedCard()"),
        # Archive Console v2 (design import): todo/vote/collect/meeting/
        # schedule/switch_corp (+ audio_archive/audio_doc, stubbed below)
        # now share one card header (structuredCardHeader) instead of a
        # bare type-label div.
        _extract(
            r"var CARD_DOT_COLORS=\{.*?\nfunction structuredCardHeader\(labelKey,rawType,dotColor\)\{.*?\n\}",
            "structuredCardHeader",
        ),
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
        "function renderAudioArchiveMessage(m){return '';} function renderAudioDocMessage(m){return '';}",
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
        _extract(r"function renderTimeline\(scrollToBottom\)\{.*?\n\}", "renderTimeline()"),
    ]
    return "\n".join(parts)


def _run(js_body: str) -> object:
    assert NODE, "node executable not found"
    harness = f"{_bundle()}\n{js_body}"
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def _msg(msgtype, media_type, **overrides) -> dict:
    return {
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
        "normalized_type": overrides.pop("normalized_type", msgtype),
        "category": None,
        "support_status": None,
        "renderer_strategy": overrides.pop("renderer_strategy", None),
        "display_label_key": f"messageType.{msgtype}" if msgtype else "messageType.unknown",
        "structured_content": overrides.pop("structured_content", None),
        **overrides,
    }


def _render(msg: dict) -> str:
    return _run(
        f"""process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));"""
    )


# ============================================================================
# QA Fix #2 — Unknown sys subtype must never render raw i18n keys
# ============================================================================


def test_unknown_sys_subtype_renders_generic_fallback() -> None:
    msg = _msg(
        "sys", "system",
        renderer_strategy="system_card", normalized_type="system",
        structured_content={
            "fields": {"subtype": "future_unknown_action", "display_text": None},
            "parse_warnings": [],
        },
    )
    html = _render(msg)
    assert "system.event.future_unknown_action" not in html, f"Raw i18n key rendered: {html}"
    assert "System event" in html, f"Missing generic fallback in: {html}"


def test_unknown_sys_subtype_renders_generic_fallback_zh_cn() -> None:
    msg = _msg(
        "sys", "system",
        renderer_strategy="system_card", normalized_type="system",
        structured_content={
            "fields": {"subtype": "some_unknown_event", "display_text": None},
            "parse_warnings": [],
        },
    )
    html = _run(
        f"I18N.setLocale('zh-CN');process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));"
    )
    assert "系统消息" in html, f"Missing zh-CN fallback in: {html}"
    assert "system.event.some_unknown_event" not in html


def test_unknown_sys_subtype_renders_generic_fallback_zh_tw() -> None:
    msg = _msg(
        "sys", "system",
        renderer_strategy="system_card", normalized_type="system",
        structured_content={
            "fields": {"subtype": "some_unknown_event", "display_text": None},
            "parse_warnings": [],
        },
    )
    html = _run(
        f"I18N.setLocale('zh-TW');process.stdout.write(JSON.stringify(renderMessageBody({json.dumps(msg)})));"
    )
    assert "系統訊息" in html, f"Missing zh-TW fallback in: {html}"
    assert "system.event.some_unknown_event" not in html


def test_known_sys_subtype_uses_localized_label() -> None:
    msg = _msg(
        "sys", "system",
        renderer_strategy="system_card", normalized_type="system",
        structured_content={
            "fields": {"subtype": "switch_corp", "display_text": None},
            "parse_warnings": [],
        },
    )
    html = _render(msg)
    assert "Switched corp" in html


def test_sys_display_text_takes_priority() -> None:
    msg = _msg(
        "sys", "system",
        renderer_strategy="system_card", normalized_type="system",
        structured_content={
            "fields": {"subtype": "create_room", "display_text": "Custom room event text"},
            "parse_warnings": [],
        },
    )
    html = _render(msg)
    assert "Custom room event text" in html


# ============================================================================
# Business card rendering — every RND-198 type
# ============================================================================


def test_vote_card_renders_title_and_items() -> None:
    msg = _msg(
        "vote", "structured", renderer_strategy="structured_card", normalized_type="vote",
        structured_content={
            "fields": {
                "title": "Best Language?",
                "items": [{"name": "Python", "count": 10}, {"name": "Rust", "count": 7}],
                "type": "single", "status": "closed",
            }
        },
    )
    html = _render(msg)
    assert "Best Language?" in html
    assert "Python" in html
    assert "Rust" in html


def test_vote_card_no_items_shows_degraded() -> None:
    msg = _msg(
        "vote", "structured", renderer_strategy="structured_card", normalized_type="vote",
        structured_content={"fields": {"title": "Poll", "items": []}},
    )
    html = _render(msg)
    assert "No vote items" in html


def test_todo_card_renders_title_and_content() -> None:
    msg = _msg(
        "todo", "structured", renderer_strategy="structured_card", normalized_type="todo",
        structured_content={"fields": {"title": "Buy milk", "content": "2% lactose free"}},
    )
    html = _render(msg)
    assert "Buy milk" in html
    assert "2% lactose free" in html


def test_todo_card_null_fields_degrade() -> None:
    msg = _msg(
        "todo", "structured", renderer_strategy="structured_card", normalized_type="todo",
        structured_content=None,
    )
    html = _render(msg)
    assert "structured-card" in html
    assert "{" not in html and "}" not in html


def test_collect_card_renders_title() -> None:
    msg = _msg(
        "collect", "structured", renderer_strategy="structured_card", normalized_type="collect",
        structured_content={
            "fields": {
                "title": "Team survey",
                "details": [{"value": "Response 1"}, {"value": "Response 2"}],
                "type": "text",
            }
        },
    )
    html = _render(msg)
    assert "Team survey" in html
    assert "entries" in html


def test_meeting_card_renders_title_time_place() -> None:
    msg = _msg(
        "meeting", "structured", renderer_strategy="structured_card", normalized_type="meeting",
        structured_content={
            "fields": {
                "title": "Sprint planning",
                "time": 1751702400000,
                "place": "Room 401",
                "agenda": "Discuss scope",
            }
        },
    )
    html = _render(msg)
    assert "Sprint planning" in html
    assert "Room 401" in html


def test_schedule_card_renders_dates() -> None:
    msg = _msg(
        "schedule", "structured", renderer_strategy="structured_card", normalized_type="schedule",
        structured_content={
            "fields": {
                "title": "Vacation",
                "starttime": 1750330800000, "endtime": 1750417200000,
                "place": "Beach", "description": None,
            }
        },
    )
    html = _render(msg)
    assert "Vacation" in html
    assert "Beach" in html


def test_redpacket_card_never_shows_amount() -> None:
    msg = _msg(
        "redpacket", "structured", renderer_strategy="structured_card", normalized_type="redpacket",
        structured_content={
            "fields": {"type": "normal", "wishing": "Happy birthday", "totalnum": 8}
        },
    )
    html = _render(msg)
    assert "Red Packet" in html
    assert "Happy birthday" in html
    assert "$" not in html
    assert "amount" not in html.lower()


def test_switch_corp_card_renders_corp_name() -> None:
    msg = _msg(
        "switch_corp", "structured", renderer_strategy="structured_card", normalized_type="switch_corp",
        structured_content={"fields": {"corp_name": "Acme Inc."}},
    )
    html = _render(msg)
    assert "Acme Inc." in html


def test_sphfeed_card_renders_video_channel_metadata_without_a_player() -> None:
    msg = _msg(
        "sphfeed", "structured", renderer_strategy="structured_card", normalized_type="sphfeed",
        display_label_key="messageType.sphfeed",
        structured_content={
            "fields": {
                "feed_type": 4,
                "sph_name": "Travel Channel",
                "feed_desc": "A mountain video",
            }
        },
    )
    html = _render(msg)
    assert "Video Channel post" in html
    assert "Travel Channel" in html
    assert "Video post" in html
    assert "A mountain video" in html
    assert "<video" not in html


def test_sphfeed_card_escapes_channel_metadata() -> None:
    msg = _msg(
        "sphfeed", "structured", renderer_strategy="structured_card", normalized_type="sphfeed",
        display_label_key="messageType.sphfeed",
        structured_content={
            "fields": {"feed_type": 2, "sph_name": "<img src=x>", "feed_desc": "<script>x</script>"}
        },
    )
    html = _render(msg)
    assert "<img" not in html
    assert "<script>" not in html
    assert "&lt;img" in html
    assert "&lt;script&gt;" in html


# ============================================================================
# No raw JSON, no undefined, no [object Object]
# ============================================================================


@pytest.mark.parametrize("normalized_type", ["vote", "todo", "collect", "meeting", "schedule", "redpacket", "switch_corp"])
def test_no_raw_json_in_business_card_html(normalized_type) -> None:
    msg = _msg(
        normalized_type, "structured", renderer_strategy="structured_card",
        normalized_type=normalized_type,
        structured_content={"fields": {"title": "Test"}},
    )
    html = _render(msg)
    assert "{" not in html and "}" not in html
    assert "undefined" not in html
    assert "[object Object]" not in html


def test_no_raw_json_in_system_card_html() -> None:
    msg = _msg(
        "sys", "system", renderer_strategy="system_card", normalized_type="system",
        structured_content={"fields": {"subtype": "create_room", "display_text": None}},
    )
    html = _render(msg)
    assert "{" not in html and "}" not in html
    assert "undefined" not in html
    assert "[object Object]" not in html


# ============================================================================
# Escaped content (XSS safety)
# ============================================================================


def test_business_card_escapes_angle_brackets() -> None:
    msg = _msg(
        "todo", "structured", renderer_strategy="structured_card", normalized_type="todo",
        structured_content={"fields": {"title": "<script>alert(1)</script>", "content": "safe"}},
    )
    html = _render(msg)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_system_card_escapes_display_text() -> None:
    msg = _msg(
        "sys", "system", renderer_strategy="system_card", normalized_type="system",
        structured_content={
            "fields": {"subtype": "create_room", "display_text": "<img src=x onerror=alert(1)>"},
        },
    )
    html = _render(msg)
    assert "<img" not in html
    assert "&lt;img" in html
