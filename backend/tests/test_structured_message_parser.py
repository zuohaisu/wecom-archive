"""
Tests for RND-197 — app.structured_message_parser.

Covers the field-extraction parsers for the six high-confidence structured
message types (link/location/markdown/news/miniprogram — text is handled
elsewhere) plus the raw-passthrough dispatch for the three low-confidence
types (card/docmsg/audio_doc) this ticket deliberately does not attempt to
parse fields for (see module docstring). Each parser test covers: a valid
complete payload, missing optional fields, and malformed/partial input —
matching the ticket's "valid / missing optional / malformed" test matrix
requirement.

RND-200 adds a second section below covering parse_mixed_message /
parse_chatrecord_message — recursive nested-message extraction, including
every nesting combination the ticket names by example (mixed->image,
mixed->file, mixed->mixed, chatrecord->mixed, chatrecord->image/voice/
video), ordering/timestamp/participant preservation, malformed-payload
recovery, and the safety budget (item cap / depth cap) that protects
against a pathological payload.

Run (from backend/):
    pytest tests/test_structured_message_parser.py -v
"""

from __future__ import annotations

import json

import pytest

from app.structured_message_parser import (
    parse_chatrecord_message,
    parse_link_message,
    parse_location_message,
    parse_markdown_message,
    parse_miniprogram_message,
    parse_mixed_message,
    parse_news_message,
    parse_structured_content,
    safe_url,
)


# ---------------------------------------------------------------------------
# safe_url — shared URL-scheme allowlist used by every parser below.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a",
        "https://example.com/a?b=c",
        "https://example.com:8443/path",
    ],
)
def test_safe_url_accepts_http_and_https(url) -> None:
    assert safe_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "ftp://example.com/a",
        "file:///etc/passwd",
        "//example.com/a",  # schemeless/protocol-relative — ambiguous, rejected
        "example.com/a",  # no scheme at all
        "",
        None,
        123,
        {"not": "a string"},
    ],
)
def test_safe_url_rejects_unsafe_or_malformed(url) -> None:
    assert safe_url(url) is None


# ---------------------------------------------------------------------------
# parse_link_message
# ---------------------------------------------------------------------------


def test_parse_link_message_valid_complete_payload() -> None:
    fields, warnings = parse_link_message(
        {
            "title": "Example",
            "description": "An example link",
            "link_url": "https://example.com/page",
            "image_url": "https://example.com/thumb.jpg",
        }
    )
    assert fields == {
        "title": "Example",
        "description": "An example link",
        "url": "https://example.com/page",
        "image_url": "https://example.com/thumb.jpg",
    }
    assert warnings == []


def test_parse_link_message_missing_optional_fields() -> None:
    fields, warnings = parse_link_message({"title": "Example", "link_url": "https://example.com"})
    assert fields["title"] == "Example"
    assert fields["description"] is None
    assert fields["image_url"] is None
    assert fields["url"] == "https://example.com"


def test_parse_link_message_malformed_unsafe_url_is_dropped_with_warning() -> None:
    fields, warnings = parse_link_message({"title": "x", "link_url": "javascript:alert(1)"})
    assert fields["url"] is None
    assert "unsafe_or_malformed_url" in warnings


def test_parse_link_message_completely_empty_payload_degrades_safely() -> None:
    fields, warnings = parse_link_message({})
    assert fields == {"title": None, "description": None, "url": None, "image_url": None}
    assert "missing_title" in warnings
    assert "missing_url" in warnings


def test_parse_link_message_tolerates_wrong_types() -> None:
    fields, warnings = parse_link_message({"title": 12345, "link_url": None, "description": []})
    assert fields["title"] == "12345"
    assert fields["url"] is None


# ---------------------------------------------------------------------------
# parse_location_message
# ---------------------------------------------------------------------------


def test_parse_location_message_valid_complete_payload() -> None:
    fields, warnings = parse_location_message(
        {"latitude": 39.9042, "longitude": 116.4074, "address": "Beijing", "title": "Office", "zoom": 15}
    )
    assert fields == {
        "name": "Office",
        "address": "Beijing",
        "latitude": 39.9042,
        "longitude": 116.4074,
        "zoom": 15,
    }
    assert warnings == []


def test_parse_location_message_missing_optional_fields() -> None:
    fields, warnings = parse_location_message({"latitude": 1.0, "longitude": 2.0})
    assert fields["name"] is None
    assert fields["address"] is None
    assert fields["latitude"] == 1.0
    assert fields["zoom"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"latitude": 999, "longitude": 1},
        {"latitude": 1, "longitude": -999},
    ],
)
def test_parse_location_message_out_of_range_coordinates_are_dropped(payload) -> None:
    fields, warnings = parse_location_message(payload)
    assert fields["latitude"] is None
    assert fields["longitude"] is None
    assert "coordinates_out_of_range" in warnings


def test_parse_location_message_malformed_coordinates_do_not_raise() -> None:
    fields, warnings = parse_location_message({"latitude": "not-a-number", "longitude": "also-bad"})
    assert fields["latitude"] is None
    assert fields["longitude"] is None
    assert "malformed_coordinates" in warnings


def test_parse_location_message_empty_payload_degrades_safely() -> None:
    fields, warnings = parse_location_message({})
    assert fields["name"] is None
    assert fields["latitude"] is None
    assert "missing_location_data" in warnings


# ---------------------------------------------------------------------------
# parse_markdown_message
# ---------------------------------------------------------------------------


def test_parse_markdown_message_valid_payload() -> None:
    fields, warnings = parse_markdown_message({"content": "**bold** text"})
    assert fields == {"content": "**bold** text"}
    assert warnings == []


def test_parse_markdown_message_missing_content() -> None:
    fields, warnings = parse_markdown_message({})
    assert fields["content"] is None
    assert "missing_content" in warnings


def test_parse_markdown_message_malformed_payload_does_not_raise() -> None:
    fields, warnings = parse_markdown_message({"content": None})
    assert fields["content"] is None


# ---------------------------------------------------------------------------
# parse_news_message
# ---------------------------------------------------------------------------


def test_parse_news_message_valid_complete_payload() -> None:
    fields, warnings = parse_news_message(
        {
            "item": [
                {
                    "title": "Article 1",
                    "description": "Desc 1",
                    "url": "https://example.com/1",
                    "pic_url": "https://example.com/1.jpg",
                },
                {"title": "Article 2", "url": "https://example.com/2"},
            ]
        }
    )
    assert len(fields["articles"]) == 2
    assert fields["articles"][0]["title"] == "Article 1"
    assert fields["articles"][0]["image_url"] == "https://example.com/1.jpg"
    assert fields["articles"][1]["description"] is None
    assert warnings == []


def test_parse_news_message_preserves_source_order() -> None:
    fields, _ = parse_news_message(
        {"item": [{"title": f"Article {i}"} for i in range(5)]}
    )
    assert [a["title"] for a in fields["articles"]] == [f"Article {i}" for i in range(5)]


def test_parse_news_message_empty_article_list() -> None:
    fields, warnings = parse_news_message({"item": []})
    assert fields["articles"] == []
    assert "empty_article_list" in warnings


def test_parse_news_message_missing_item_key() -> None:
    fields, warnings = parse_news_message({})
    assert fields["articles"] == []
    assert "empty_article_list" in warnings


def test_parse_news_message_malformed_item_is_not_a_list() -> None:
    fields, warnings = parse_news_message({"item": "not-a-list"})
    assert fields["articles"] == []
    assert "malformed_item_list" in warnings


def test_parse_news_message_malformed_individual_article_degrades_without_dropping_others() -> None:
    fields, warnings = parse_news_message(
        {"item": [{"title": "Good"}, "not-a-dict", {"title": "Also good"}]}
    )
    assert len(fields["articles"]) == 3
    assert fields["articles"][0]["title"] == "Good"
    assert fields["articles"][1]["title"] is None
    assert fields["articles"][2]["title"] == "Also good"


def test_parse_news_message_caps_oversized_article_list() -> None:
    fields, warnings = parse_news_message({"item": [{"title": f"A{i}"} for i in range(50)]})
    assert len(fields["articles"]) == 20
    assert any(w.startswith("article_list_truncated") for w in warnings)


def test_parse_news_message_rejects_unsafe_article_urls() -> None:
    fields, _ = parse_news_message({"item": [{"title": "x", "url": "javascript:alert(1)"}]})
    assert fields["articles"][0]["url"] is None


# ---------------------------------------------------------------------------
# parse_miniprogram_message
# ---------------------------------------------------------------------------


def test_parse_miniprogram_message_valid_complete_payload() -> None:
    fields, warnings = parse_miniprogram_message(
        {
            "title": "My Mini Program",
            "displayname": "MiniApp",
            "appid": "wx1234567890",
            "username": "gh_abcdef",
            "pagepath": "pages/index/index",
            "cover_url": "https://example.com/icon.png",
        }
    )
    assert fields["title"] == "My Mini Program"
    assert fields["appid"] == "wx1234567890"
    assert fields["username"] == "gh_abcdef"
    assert fields["pagepath"] == "pages/index/index"
    assert fields["icon_url"] == "https://example.com/icon.png"
    assert warnings == []


def test_parse_miniprogram_message_missing_optional_fields() -> None:
    fields, warnings = parse_miniprogram_message({"title": "x", "appid": "wx1"})
    assert fields["pagepath"] is None
    assert fields["icon_url"] is None


def test_parse_miniprogram_message_empty_payload_degrades_safely() -> None:
    fields, warnings = parse_miniprogram_message({})
    assert fields["title"] is None
    assert "missing_title" in warnings
    assert "missing_appid" in warnings


def test_parse_miniprogram_message_icon_field_name_checked_defensively() -> None:
    """Icon field name confidence is lower than title/appid/username/
    pagepath (RND-197 dev report) — check a couple of plausible key names
    rather than assuming one."""
    for key in ("cover_url", "thumb_url", "icon_url"):
        fields, _ = parse_miniprogram_message({"title": "x", key: "https://example.com/i.png"})
        assert fields["icon_url"] == "https://example.com/i.png"


# ---------------------------------------------------------------------------
# parse_structured_content — the msgtype -> parser dispatcher.
# ---------------------------------------------------------------------------


def test_dispatch_structured_fields_type_returns_fields_raw_and_warnings() -> None:
    result = parse_structured_content(
        "link", {"msgtype": "link", "link": {"title": "x", "link_url": "https://example.com"}}
    )
    assert result["fields"]["title"] == "x"
    assert result["raw"] == {"title": "x", "link_url": "https://example.com"}
    assert isinstance(result["parse_warnings"], list)


@pytest.mark.parametrize("msgtype", ["card", "docmsg", "audio_doc"])
def test_dispatch_raw_passthrough_types_preserve_raw_without_field_extraction(msgtype) -> None:
    sub_payload = {"some_unconfirmed_field": "value"}
    result = parse_structured_content(msgtype, {"msgtype": msgtype, msgtype: sub_payload})
    assert result["fields"] is None
    assert result["raw"] == sub_payload
    assert result["parse_warnings"] == ["unconfirmed_schema"]


@pytest.mark.parametrize("msgtype", ["text", "image", "video", "voice", "file", "revoke"])
def test_dispatch_returns_none_for_out_of_scope_types(msgtype) -> None:
    assert parse_structured_content(msgtype, {"msgtype": msgtype}) is None


def test_dispatch_returns_none_for_unregistered_msgtype() -> None:
    assert parse_structured_content("totally_unknown_future_type", {}) is None


def test_dispatch_handles_missing_sub_payload_without_raising() -> None:
    result = parse_structured_content("link", {"msgtype": "link"})
    assert result["fields"]["url"] is None
    assert result["raw"] == {}


def test_dispatch_handles_malformed_sub_payload_without_raising() -> None:
    result = parse_structured_content("location", {"msgtype": "location", "location": "not-a-dict"})
    assert result["fields"]["latitude"] is None
    assert result["raw"] == {}


def test_dispatch_never_raises_for_a_parser_that_throws() -> None:
    """Defensive regression guard: even if a parser implementation bug
    slips through, the dispatcher must degrade, not propagate — a single
    malformed historical row must never fail an entire decrypt run or
    timeline page."""
    import app.structured_message_parser as parser_module

    original = parser_module._STRUCTURED_FIELD_PARSERS["link"]

    def _boom(payload):
        raise RuntimeError("simulated parser bug")

    parser_module._STRUCTURED_FIELD_PARSERS["link"] = _boom
    try:
        result = parser_module.parse_structured_content(
            "link", {"msgtype": "link", "link": {"title": "x"}}
        )
    finally:
        parser_module._STRUCTURED_FIELD_PARSERS["link"] = original

    assert result["fields"] is None
    assert result["parse_warnings"] == ["parse_failed"]


# ---------------------------------------------------------------------------
# RND-200 — parse_mixed_message / parse_chatrecord_message
#
# WeCom's real payload shape is unconfirmed against any fixture in this
# repo (see module docstring) — item.content is treated as a JSON-encoded
# string that decodes to that item type's normal payload shape (e.g.
# image's {"sdkfileid": ...}), matching the ticket's documented examples
# and the defensive-decode approach _parse_nested_item implements.
# ---------------------------------------------------------------------------


def _text_item(text: str, **extra) -> dict:
    return {"type": "text", "content": json.dumps({"content": text}), **extra}


def _media_item(item_type: str, sdkfileid: str) -> dict:
    return {"type": item_type, "content": json.dumps({"sdkfileid": sdkfileid})}


def _mixed_item(items: list) -> dict:
    return {"type": "mixed", "content": json.dumps({"item": items})}


def _chatrecord_item(title: str, items: list) -> dict:
    return {"type": "chatrecord", "content": json.dumps({"title": title, "item": items})}


# --- mixed: one test per ticket-named child type -----------------------------


def test_parse_mixed_message_text_child() -> None:
    fields, warnings, media_refs = parse_mixed_message({"item": [_text_item("hello world")]})
    assert fields["item_count"] == 1
    item = fields["items"][0]
    assert item["path"] == "0"
    assert item["type"] == "text"
    assert item["supported"] is True
    assert item["text"] == "hello world"
    assert item["media"] is None
    assert warnings == []
    assert media_refs == []


def test_parse_mixed_message_text_child_accepts_bare_string_content() -> None:
    """The exact WeCom text-content encoding is unconfirmed — a bare
    string (not JSON-wrapped) must also be accepted, not treated as
    malformed."""
    fields, warnings, _refs = parse_mixed_message(
        {"item": [{"type": "text", "content": "plain string, not JSON"}]}
    )
    assert fields["items"][0]["text"] == "plain string, not JSON"
    assert fields["items"][0].get("malformed") is not True


def test_parse_mixed_message_image_child() -> None:
    fields, warnings, media_refs = parse_mixed_message({"item": [_media_item("image", "sdk-img-1")]})
    item = fields["items"][0]
    assert item["type"] == "image"
    assert item["supported"] is True
    assert item["media"] == {"has_reference": True}
    assert media_refs == [{"path": "0", "type": "image", "sdkfileid": "sdk-img-1"}]
    assert warnings == []


def test_parse_mixed_message_file_child() -> None:
    fields, warnings, media_refs = parse_mixed_message({"item": [_media_item("file", "sdk-file-1")]})
    assert fields["items"][0]["type"] == "file"
    assert media_refs == [{"path": "0", "type": "file", "sdkfileid": "sdk-file-1"}]


def test_parse_mixed_message_video_child() -> None:
    fields, warnings, media_refs = parse_mixed_message({"item": [_media_item("video", "sdk-vid-1")]})
    assert fields["items"][0]["type"] == "video"
    assert media_refs == [{"path": "0", "type": "video", "sdkfileid": "sdk-vid-1"}]


def test_parse_mixed_message_voice_child() -> None:
    fields, warnings, media_refs = parse_mixed_message({"item": [_media_item("voice", "sdk-voice-1")]})
    assert fields["items"][0]["type"] == "voice"
    assert media_refs == [{"path": "0", "type": "voice", "sdkfileid": "sdk-voice-1"}]


def test_parse_mixed_message_media_child_without_sdkfileid_has_no_reference() -> None:
    fields, warnings, media_refs = parse_mixed_message(
        {"item": [{"type": "image", "content": json.dumps({"md5sum": "abc"})}]}
    )
    assert fields["items"][0]["media"] == {"has_reference": False}
    assert media_refs == []


def test_parse_mixed_message_nested_mixed_child() -> None:
    """mixed -> mixed: a nested composite recurses, children preserved
    under the parent node's "children" key with a dotted path."""
    inner = _mixed_item([_text_item("inner text"), _media_item("image", "sdk-inner-img")])
    fields, warnings, media_refs = parse_mixed_message({"item": [inner]})
    outer_node = fields["items"][0]
    assert outer_node["type"] == "mixed"
    assert outer_node["children"] is not None
    assert len(outer_node["children"]) == 2
    assert outer_node["children"][0]["path"] == "0.0"
    assert outer_node["children"][0]["text"] == "inner text"
    assert outer_node["children"][1]["path"] == "0.1"
    assert media_refs == [{"path": "0.1", "type": "image", "sdkfileid": "sdk-inner-img"}]


def test_parse_mixed_message_malformed_top_level_item_not_a_list() -> None:
    fields, warnings, media_refs = parse_mixed_message({"item": "not-a-list"})
    assert fields["items"] == []
    assert fields["item_count"] == 0
    assert "malformed_item_list" in warnings
    assert media_refs == []


def test_parse_mixed_message_empty_item_list() -> None:
    fields, warnings, _refs = parse_mixed_message({"item": []})
    assert fields["items"] == []
    assert "empty_item_list" in warnings


def test_parse_mixed_message_missing_item_key() -> None:
    fields, warnings, _refs = parse_mixed_message({})
    assert fields["items"] == []
    assert "empty_item_list" in warnings


def test_parse_mixed_message_non_dict_payload_degrades_safely() -> None:
    fields, warnings, media_refs = parse_mixed_message("not-a-dict")
    assert fields == {"items": [], "item_count": 0}
    assert media_refs == []


def test_parse_mixed_message_malformed_child_does_not_drop_the_others() -> None:
    """One item with undecodable JSON content must not prevent the rest of
    the list from parsing (ticket: unknown/malformed types must not break
    parsing)."""
    items = [
        _text_item("good text"),
        {"type": "image", "content": "not-valid-json{{{"},
        _media_item("file", "sdk-file-ok"),
        "not-even-a-dict",
    ]
    fields, warnings, media_refs = parse_mixed_message({"item": items})
    assert fields["item_count"] == 4
    assert fields["items"][0]["text"] == "good text"
    assert fields["items"][1]["malformed"] is True
    assert fields["items"][2]["type"] == "file"
    assert fields["items"][3]["type"] is None
    assert fields["items"][3]["supported"] is False
    assert "some_items_malformed" in warnings
    # The malformed image item never reached media_refs -- only the good file did.
    assert media_refs == [{"path": "2", "type": "file", "sdkfileid": "sdk-file-ok"}]


def test_parse_mixed_message_preserves_source_order() -> None:
    items = [_text_item(f"msg {i}") for i in range(6)]
    fields, _warnings, _refs = parse_mixed_message({"item": items})
    assert [i["path"] for i in fields["items"]] == [str(i) for i in range(6)]
    assert [i["text"] for i in fields["items"]] == [f"msg {i}" for i in range(6)]


# --- unknown/unsupported child types ----------------------------------------


def test_parse_mixed_message_unknown_child_type_stays_visible() -> None:
    """Ticket requirement: unknown embedded message types must not be
    discarded — they remain visible with their raw type and a best-effort
    text fallback, and are never flagged as malformed (there is no known
    schema for them to fail to conform to)."""
    fields, warnings, _refs = parse_mixed_message(
        {"item": [{"type": "some_future_wecom_type", "content": "raw fallback text"}]}
    )
    item = fields["items"][0]
    assert item["type"] == "some_future_wecom_type"
    assert item["supported"] is False
    assert item["text"] == "raw fallback text"
    assert item.get("malformed") is not True
    assert "some_items_unsupported_type" in warnings


def test_parse_mixed_message_unknown_child_type_with_dict_content() -> None:
    fields, _warnings, _refs = parse_mixed_message(
        {"item": [{"type": "revoke", "content": {"content": "revoked text"}}]}
    )
    assert fields["items"][0]["text"] == "revoked text"


# --- safety budget: item cap / nesting depth cap ----------------------------


def test_parse_mixed_message_caps_oversized_flat_item_list() -> None:
    from app.structured_message_parser import _MIXED_ITEM_CAP

    items = [_text_item(f"m{i}") for i in range(_MIXED_ITEM_CAP + 50)]
    fields, warnings, _refs = parse_mixed_message({"item": items})
    assert fields["item_count"] == _MIXED_ITEM_CAP
    assert any(w.startswith("item_tree_truncated_at_") for w in warnings)


def test_parse_mixed_message_caps_excessive_nesting_depth() -> None:
    """Protects against a pathological (malformed or adversarial)
    mixed-in-mixed-in-mixed... payload exceeding the recursion safety
    budget — must degrade (drop the excess levels, warn), never raise
    RecursionError, never hang."""
    from app.structured_message_parser import _MIXED_MAX_DEPTH

    def _make_deeply_nested(depth: int) -> dict:
        if depth == 0:
            return _text_item("leaf")
        return _mixed_item([_make_deeply_nested(depth - 1)])

    deep_payload = _mixed_item([_make_deeply_nested(_MIXED_MAX_DEPTH + 10)])
    fields, warnings, _refs = parse_mixed_message({"item": [deep_payload]})
    assert any(w.startswith("nesting_depth_truncated_at_") for w in warnings)
    # Never raises RecursionError or hangs -- reaching this assertion is
    # itself the primary regression guard.


def test_parse_mixed_message_wide_and_deep_payload_never_raises() -> None:
    """Combined stress case: many siblings, each moderately nested --
    confirms the shared budget (not a per-branch counter) bounds total
    work across the whole tree, not just one path."""
    from app.structured_message_parser import _MIXED_MAX_DEPTH

    def _make_nested(depth: int) -> dict:
        if depth == 0:
            return _media_item("image", "sdk-x")
        return _mixed_item([_make_nested(depth - 1), _make_nested(depth - 1)])

    payload = {"item": [_make_nested(_MIXED_MAX_DEPTH) for _ in range(5)]}
    fields, warnings, media_refs = parse_mixed_message(payload)
    assert isinstance(fields["items"], list)
    assert isinstance(media_refs, list)


# --- chatrecord --------------------------------------------------------------


def test_parse_chatrecord_message_simple() -> None:
    fields, warnings, media_refs = parse_chatrecord_message(
        {
            "title": "Group chat history",
            "item": [_text_item("hi there", **{"from": "zhangsan", "fromname": "Zhang San", "msgtime": 1720000000000})],
        }
    )
    assert fields["title"] == "Group chat history"
    assert fields["item_count"] == 1
    item = fields["items"][0]
    assert item["text"] == "hi there"
    assert item["sender"] == "zhangsan"
    assert item["sender_name"] == "Zhang San"
    assert item["timestamp"] == 1720000000000
    assert warnings == []
    assert media_refs == []


def test_parse_chatrecord_message_missing_title_warns_but_still_parses_items() -> None:
    fields, warnings, _refs = parse_chatrecord_message({"item": [_text_item("x")]})
    assert fields["title"] is None
    assert "missing_title" in warnings
    assert fields["item_count"] == 1


def test_parse_chatrecord_message_nested_chatrecord_child() -> None:
    inner = _chatrecord_item("inner digest", [_text_item("nested msg")])
    fields, warnings, _refs = parse_chatrecord_message({"title": "outer", "item": [inner]})
    outer_node = fields["items"][0]
    assert outer_node["type"] == "chatrecord"
    assert outer_node["fields"] == {"title": "inner digest"}
    assert outer_node["children"][0]["text"] == "nested msg"


def test_parse_chatrecord_message_nested_mixed_child() -> None:
    """chatrecord -> mixed, per the ticket's named nesting example."""
    inner = _mixed_item([_media_item("image", "sdk-1"), _media_item("file", "sdk-2")])
    fields, warnings, media_refs = parse_chatrecord_message({"title": "t", "item": [inner]})
    outer_node = fields["items"][0]
    assert outer_node["type"] == "mixed"
    assert [c["type"] for c in outer_node["children"]] == ["image", "file"]
    assert {r["sdkfileid"] for r in media_refs} == {"sdk-1", "sdk-2"}


@pytest.mark.parametrize("media_type", ["image", "voice", "video"])
def test_parse_chatrecord_message_direct_media_children(media_type) -> None:
    """chatrecord -> image / voice / video, per the ticket's named
    nesting examples."""
    fields, warnings, media_refs = parse_chatrecord_message(
        {"title": "t", "item": [_media_item(media_type, f"sdk-{media_type}")]}
    )
    assert fields["items"][0]["type"] == media_type
    assert media_refs == [{"path": "0", "type": media_type, "sdkfileid": f"sdk-{media_type}"}]


def test_parse_chatrecord_message_media_references_span_multiple_items() -> None:
    fields, warnings, media_refs = parse_chatrecord_message(
        {
            "title": "t",
            "item": [
                _media_item("image", "sdk-a"),
                _text_item("plain"),
                _media_item("voice", "sdk-b"),
            ],
        }
    )
    assert [r["sdkfileid"] for r in media_refs] == ["sdk-a", "sdk-b"]
    assert [r["path"] for r in media_refs] == ["0", "2"]


def test_parse_chatrecord_message_preserves_ordering_and_timestamps() -> None:
    items = [
        _text_item(f"m{i}", **{"msgtime": 1_700_000_000_000 + i}) for i in range(4)
    ]
    fields, _warnings, _refs = parse_chatrecord_message({"title": "t", "item": items})
    timestamps = [i["timestamp"] for i in fields["items"]]
    assert timestamps == [1_700_000_000_000 + i for i in range(4)]
    assert [i["path"] for i in fields["items"]] == ["0", "1", "2", "3"]


def test_parse_chatrecord_message_participant_fields_preserved_per_item() -> None:
    """Each item can have a distinct sender -- a forwarded group chat
    history's whole point is multiple participants' messages in one
    digest."""
    items = [
        _text_item("hi", **{"from": "alice"}),
        _text_item("hello", **{"from": "bob"}),
    ]
    fields, _warnings, _refs = parse_chatrecord_message({"title": "t", "item": items})
    assert fields["items"][0]["sender"] == "alice"
    assert fields["items"][1]["sender"] == "bob"


def test_parse_chatrecord_message_malformed_payload_degrades_safely() -> None:
    fields, warnings, media_refs = parse_chatrecord_message({"title": 123, "item": "garbage"})
    assert fields["title"] == "123"
    assert fields["items"] == []
    assert "malformed_item_list" in warnings
    assert media_refs == []


def test_parse_chatrecord_message_non_dict_payload_never_raises() -> None:
    fields, warnings, media_refs = parse_chatrecord_message(None)
    assert fields == {"title": None, "items": [], "item_count": 0}
    assert media_refs == []


# --- dispatcher-level integration (parse_structured_content) ----------------


def test_dispatch_mixed_returns_fields_raw_warnings_and_media_refs() -> None:
    decrypted = {
        "msgtype": "mixed",
        "mixed": {"item": [_text_item("hi"), _media_item("image", "sdk-1")]},
    }
    result = parse_structured_content("mixed", decrypted)
    assert result["fields"]["item_count"] == 2
    assert result["raw"] == decrypted["mixed"]
    assert result["media_refs"] == [{"path": "1", "type": "image", "sdkfileid": "sdk-1"}]
    assert isinstance(result["parse_warnings"], list)


def test_dispatch_chatrecord_returns_fields_raw_warnings_and_media_refs() -> None:
    decrypted = {
        "msgtype": "chatrecord",
        "chatrecord": {"title": "history", "item": [_media_item("voice", "sdk-v")]},
    }
    result = parse_structured_content("chatrecord", decrypted)
    assert result["fields"]["title"] == "history"
    assert result["raw"] == decrypted["chatrecord"]
    assert result["media_refs"] == [{"path": "0", "type": "voice", "sdkfileid": "sdk-v"}]


def test_dispatch_mixed_raw_is_scoped_to_the_mixed_sub_payload_only() -> None:
    """Same privacy boundary the other dispatch tests enforce: raw must
    never include sender/tolist/msgtime from the outer envelope."""
    decrypted = {
        "msgtype": "mixed",
        "from": "someone",
        "tolist": ["a", "b"],
        "msgtime": 123,
        "mixed": {"item": []},
    }
    result = parse_structured_content("mixed", decrypted)
    assert result["raw"] == {"item": []}
    assert "from" not in result["raw"]
    assert "tolist" not in result["raw"]


def test_dispatch_non_nested_types_never_carry_a_media_refs_key() -> None:
    """media_refs is additive, mixed/chatrecord-only -- every other in-scope
    type's result dict must not gain this key as a side effect."""
    result = parse_structured_content(
        "link", {"msgtype": "link", "link": {"title": "x", "link_url": "https://example.com"}}
    )
    assert "media_refs" not in result


def test_dispatch_mixed_missing_sub_payload_degrades_safely() -> None:
    result = parse_structured_content("mixed", {"msgtype": "mixed"})
    assert result["fields"] == {"items": [], "item_count": 0}
    assert result["raw"] == {}
    assert result["media_refs"] == []


def test_dispatch_never_raises_when_nested_parser_throws() -> None:
    """Same defensive-dispatch guarantee as the existing STRUCTURED_FIELDS
    regression test above, applied to the NESTED_MESSAGES branch."""
    import app.structured_message_parser as parser_module

    original = parser_module._NESTED_MESSAGE_PARSERS["mixed"]

    def _boom(payload):
        raise RuntimeError("simulated nested parser bug")

    parser_module._NESTED_MESSAGE_PARSERS["mixed"] = _boom
    try:
        result = parser_module.parse_structured_content(
            "mixed", {"msgtype": "mixed", "mixed": {"item": []}}
        )
    finally:
        parser_module._NESTED_MESSAGE_PARSERS["mixed"] = original

    assert result["fields"] is None
    assert result["parse_warnings"] == ["parse_failed"]
    assert result["media_refs"] == []


def test_dispatch_chatrecord_no_longer_falls_through_to_none() -> None:
    """RND-200 regression guard mirroring the removed 'mixed' entry in
    test_dispatch_returns_none_for_out_of_scope_types above -- chatrecord
    must never silently return None once NESTED_MESSAGES is implemented."""
    result = parse_structured_content("chatrecord", {"msgtype": "chatrecord"})
    assert result is not None
    assert "items" in result["fields"]


# ---------------------------------------------------------------------------
# RND-200 QA fixes — found by adversarial code review after the initial
# implementation:
#   1. json.loads() on a pathologically deep-nested "content" string raises
#      RecursionError (a RuntimeError subclass), not ValueError/TypeError —
#      uncaught, it used to bubble past per-item degradation and wipe out
#      the ENTIRE message's parse result via parse_structured_content's
#      outer `except Exception`.
#   2. Nested card/docmsg/audio_doc children were misclassified as
#      "unknown/unsupported" instead of matching their top-level PARTIAL/
#      RAW_PASSTHROUGH treatment.
#   3. A nested composite child's own malformed/empty "item" list produced
#      no warning anywhere, because the composite branch hand-copied the
#      item-list loop instead of reusing _parse_nested_item_list.
# ---------------------------------------------------------------------------


def test_pathologically_nested_content_string_does_not_wipe_out_the_whole_message() -> None:
    """A single item whose content string is deeply bracket-nested JSON
    must degrade to malformed=True for just that item — siblings (and
    their media_refs) must survive."""
    evil_content = "[" * 3000
    payload = {
        "item": [
            _media_item("image", "sdk-good"),
            {"type": "link", "content": evil_content},
        ]
    }
    fields, warnings, media_refs = parse_mixed_message(payload)
    assert fields["item_count"] == 2
    assert fields["items"][0]["type"] == "image"
    assert fields["items"][0].get("malformed") is not True
    assert fields["items"][1]["malformed"] is True
    assert media_refs == [{"path": "0", "type": "image", "sdkfileid": "sdk-good"}]
    assert "some_items_malformed" in warnings


def test_pathologically_nested_text_content_string_degrades_safely() -> None:
    """Same RecursionError hazard, reached via _extract_nested_text's own
    json.loads call (the "text" item code path) rather than
    _decode_nested_content's."""
    evil_content = "{" * 3000
    fields, _warnings, _refs = parse_mixed_message({"item": [{"type": "text", "content": evil_content}]})
    # Falls back to treating the literal string as the text itself rather
    # than raising -- never a malformed item for the text-type path (see
    # _extract_nested_text's fallback-to-literal-text contract).
    assert fields["items"][0]["type"] == "text"
    assert isinstance(fields["items"][0]["text"], str)


def test_dispatch_never_raises_for_pathologically_nested_content() -> None:
    """End-to-end through the real dispatcher (not just the parser
    function directly) — the scenario that would previously have wiped
    the whole structured_content to None/parse_failed."""
    decrypted = {
        "msgtype": "mixed",
        "mixed": {"item": [_media_item("image", "sdk-good"), {"type": "video", "content": "[" * 5000}]},
    }
    result = parse_structured_content("mixed", decrypted)
    assert result["fields"] is not None
    assert result["fields"]["item_count"] == 2
    assert result["media_refs"] == [{"path": "0", "type": "image", "sdkfileid": "sdk-good"}]


@pytest.mark.parametrize("msgtype", ["card", "docmsg", "audio_doc"])
def test_nested_raw_passthrough_types_are_supported_with_no_field_extraction(msgtype) -> None:
    """A nested card/docmsg/audio_doc child matches its top-level PARTIAL/
    RAW_PASSTHROUGH treatment: recognized (supported=True), but no field
    extraction is attempted (fields stays None) — it must NOT fall into
    the generic unknown-type bucket."""
    fields, warnings, _refs = parse_mixed_message(
        {"item": [{"type": msgtype, "content": json.dumps({"some_field": "value"})}]}
    )
    item = fields["items"][0]
    assert item["type"] == msgtype
    assert item["supported"] is True
    assert item["fields"] is None
    assert item.get("malformed") is not True
    assert "some_items_unsupported_type" not in warnings


def test_nested_composite_childs_own_malformed_item_list_surfaces_a_warning() -> None:
    """RND-200 QA fix: a nested mixed/chatrecord child whose own "item"
    field is malformed (not a list) must produce a visible warning at the
    top level — not silently vanish because the composite branch used to
    hand-copy the item-list loop instead of reusing the shared helper."""
    inner = {"type": "mixed", "content": json.dumps({"item": "not-a-list"})}
    fields, warnings, _refs = parse_mixed_message({"item": [inner]})
    outer_node = fields["items"][0]
    assert outer_node["type"] == "mixed"
    assert outer_node["children"] == []
    assert any("malformed_item_list" in w for w in warnings)


def test_nested_composite_childs_own_empty_item_list_surfaces_a_warning() -> None:
    inner = {"type": "chatrecord", "content": json.dumps({"title": "t", "item": []})}
    fields, warnings, _refs = parse_chatrecord_message({"title": "outer", "item": [inner]})
    assert any("empty_item_list" in w for w in warnings)


def test_nested_composite_childs_missing_item_key_does_not_warn_malformed() -> None:
    """A composite child with no "item" key at all (vs. one present but
    the wrong type) must only warn empty_item_list, matching the exact
    semantics the top-level dispatcher already uses for a missing key."""
    inner = {"type": "mixed", "content": json.dumps({})}
    fields, warnings, _refs = parse_mixed_message({"item": [inner]})
    assert not any("malformed_item_list" in w for w in warnings)
    assert any("empty_item_list" in w for w in warnings)
    assert fields["items"][0]["children"] == []


def test_nested_composite_childs_own_malformed_children_are_flagged_too() -> None:
    """A grandchild malformed inside a nested composite's own item list
    still degrades that one grandchild only, and the warning about it is
    visible at the top level via the path-tagged nested_warnings bubble."""
    grandchild_bad = {"type": "link", "content": "not-valid-json{{{"}
    inner = {"type": "mixed", "content": json.dumps({"item": [grandchild_bad]})}
    fields, warnings, _refs = parse_mixed_message({"item": [inner]})
    outer_node = fields["items"][0]
    assert outer_node["children"][0]["malformed"] is True
    assert any("some_items_malformed" in w for w in warnings)


def test_iteration_stops_once_safety_budget_is_exhausted() -> None:
    """The item cap must bound CPU work, not just output size -- once the
    tree-wide budget is exhausted, the list walk must stop calling
    _parse_nested_item for remaining siblings instead of iterating a
    possibly enormous remaining list only to discard every result."""
    import app.structured_message_parser as parser_module

    call_count = {"n": 0}
    original = parser_module._parse_nested_item

    def _counting_wrapper(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    parser_module._parse_nested_item = _counting_wrapper
    try:
        items = [_text_item(f"m{i}") for i in range(parser_module._MIXED_ITEM_CAP + 5000)]
        parser_module.parse_mixed_message({"item": items})
    finally:
        parser_module._parse_nested_item = original

    # Called at most once per item up to the cap -- never once per every
    # item in a list thousands of entries past the cap.
    assert call_count["n"] <= parser_module._MIXED_ITEM_CAP + 1


def test_nested_media_types_are_a_subset_of_media_download_generic_types() -> None:
    """Regression guard for the documented coupling between this module's
    _NESTED_MEDIA_TYPES and app.media_download's own downloadable-type
    vocabulary (_SIGNATURE_CATEGORY_BY_MSGTYPE/GENERIC_DOWNLOAD_MSGTYPES) —
    every media_refs entry's "type" must be one download_one() can
    actually handle. No import coupling is introduced between the two
    modules for this (structured_message_parser has no app-internal
    dependency besides message_type_registry) — this test is the
    compiler/CI signal that would otherwise be missing if the two sets
    drift apart."""
    import app.structured_message_parser as parser_module
    from app.media_download import GENERIC_DOWNLOAD_MSGTYPES

    assert parser_module._NESTED_MEDIA_TYPES <= GENERIC_DOWNLOAD_MSGTYPES
