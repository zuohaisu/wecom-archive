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

Run (from backend/):
    pytest tests/test_structured_message_parser.py -v
"""

from __future__ import annotations

import pytest

from app.structured_message_parser import (
    parse_link_message,
    parse_location_message,
    parse_markdown_message,
    parse_miniprogram_message,
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


@pytest.mark.parametrize("msgtype", ["text", "image", "video", "voice", "file", "revoke", "mixed"])
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
