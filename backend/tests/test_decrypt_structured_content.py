"""
Tests for RND-197's wiring of app.structured_message_parser into
scripts/decrypt_wecom_messages_once.py — _normalise_fields() must populate
"structured_content" for every in-scope type, and the existing SF-1
constraint (never persist the full decrypted_payload envelope) must not
regress.

Run (from backend/):
    pytest tests/test_decrypt_structured_content.py -v
"""

from __future__ import annotations

import inspect

from scripts.decrypt_wecom_messages_once import _normalise_fields, main as _decrypt_main


def test_normalise_fields_returns_structured_content_for_link() -> None:
    decrypted = {
        "msgtype": "link",
        "from": "userid1",
        "roomid": "",
        "msgtime": 123,
        "tolist": ["userid2"],
        "link": {
            "title": "Example",
            "description": "desc",
            "link_url": "https://example.com",
            "image_url": "https://example.com/i.jpg",
        },
    }
    normalised = _normalise_fields(decrypted)
    assert normalised["structured_content"]["fields"]["title"] == "Example"
    assert normalised["structured_content"]["fields"]["url"] == "https://example.com"
    assert normalised["structured_content"]["raw"] == decrypted["link"]


def test_normalise_fields_returns_structured_content_for_location() -> None:
    decrypted = {
        "msgtype": "location",
        "location": {"latitude": 1.0, "longitude": 2.0, "address": "somewhere", "title": "Home"},
    }
    normalised = _normalise_fields(decrypted)
    fields = normalised["structured_content"]["fields"]
    assert fields["latitude"] == 1.0
    assert fields["name"] == "Home"


def test_normalise_fields_returns_structured_content_for_markdown() -> None:
    decrypted = {"msgtype": "markdown", "markdown": {"content": "**hi**"}}
    normalised = _normalise_fields(decrypted)
    assert normalised["structured_content"]["fields"]["content"] == "**hi**"


def test_normalise_fields_returns_structured_content_for_news() -> None:
    decrypted = {"msgtype": "news", "news": {"item": [{"title": "A", "url": "https://example.com"}]}}
    normalised = _normalise_fields(decrypted)
    assert len(normalised["structured_content"]["fields"]["articles"]) == 1


def test_normalise_fields_returns_structured_content_for_weapp() -> None:
    decrypted = {
        "msgtype": "weapp",
        "weapp": {"title": "MiniApp", "appid": "wx1", "username": "gh_1", "pagepath": "pages/index"},
    }
    normalised = _normalise_fields(decrypted)
    fields = normalised["structured_content"]["fields"]
    assert fields["title"] == "MiniApp"
    assert fields["appid"] == "wx1"


def test_normalise_fields_returns_raw_passthrough_for_docmsg_only() -> None:
    # RND-210: card/audio_doc are now STRUCTURED_FIELDS (they extract real
    # fields); docmsg remains the only RAW_PASSTHROUGH (unconfirmed schema)
    # type among the former group.
    for msgtype in ("docmsg",):
        decrypted = {"msgtype": msgtype, msgtype: {"unconfirmed_field": "value"}}
        normalised = _normalise_fields(decrypted)
        structured = normalised["structured_content"]
        assert structured["fields"] is None
        assert structured["raw"] == {"unconfirmed_field": "value"}
        assert structured["parse_warnings"] == ["unconfirmed_schema"]


def test_normalise_fields_structured_content_is_none_for_text_and_media_types() -> None:
    for msgtype, payload_key in (("text", "text"), ("image", "image"), ("video", "video")):
        decrypted = {"msgtype": msgtype, payload_key: {"content": "x", "sdkfileid": "y"}}
        normalised = _normalise_fields(decrypted)
        assert normalised["structured_content"] is None


def test_normalise_fields_handles_missing_sub_payload_without_raising() -> None:
    normalised = _normalise_fields({"msgtype": "link"})
    assert normalised["structured_content"]["fields"]["url"] is None


def test_normalise_fields_handles_malformed_historical_payload_without_raising() -> None:
    # A non-dict where the type-specific sub-payload should be, matching
    # the ticket's "malformed historical data" scenario (e.g. a string
    # where a nested object is expected).
    normalised = _normalise_fields({"msgtype": "location", "location": "not-a-dict"})
    assert normalised["structured_content"]["fields"]["latitude"] is None


def test_normalise_fields_handles_unregistered_msgtype_without_raising() -> None:
    normalised = _normalise_fields({"msgtype": "some_future_type_not_registered"})
    assert normalised["structured_content"] is None


# ---------------------------------------------------------------------------
# RND-200 — mixed/chatrecord wiring through the same _normalise_fields()
# entry point (zero decrypt-script code changes were needed: the existing
# registry-driven dispatch to parse_structured_content already covers
# NESTED_MESSAGES once app.structured_message_parser implements it — these
# guard that continuing to be true).
# ---------------------------------------------------------------------------


def test_normalise_fields_returns_structured_content_for_mixed() -> None:
    decrypted = {
        "msgtype": "mixed",
        "mixed": {
            "item": [
                {"type": "text", "content": '{"content":"hi"}'},
                {"type": "image", "content": '{"sdkfileid":"sdk-img-1"}'},
            ]
        },
    }
    normalised = _normalise_fields(decrypted)
    structured = normalised["structured_content"]
    assert structured["fields"]["item_count"] == 2
    assert structured["raw"] == decrypted["mixed"]
    assert structured["media_refs"] == [{"path": "1", "type": "image", "sdkfileid": "sdk-img-1"}]


def test_normalise_fields_returns_structured_content_for_chatrecord() -> None:
    decrypted = {
        "msgtype": "chatrecord",
        "chatrecord": {
            "title": "forwarded",
            "item": [{"type": "voice", "content": '{"sdkfileid":"sdk-voice-1"}'}],
        },
    }
    normalised = _normalise_fields(decrypted)
    structured = normalised["structured_content"]
    assert structured["fields"]["title"] == "forwarded"
    assert structured["media_refs"] == [{"path": "0", "type": "voice", "sdkfileid": "sdk-voice-1"}]


def test_normalise_fields_mixed_has_no_top_level_sdkfileid() -> None:
    """mixed/chatrecord have no single message-level sdkfileid — media
    lives only inside nested items (structured_content.media_refs),
    discovered by app.media_download's dedicated nested candidate path,
    not the scalar ArchiveMessage.sdkfileid column the existing media
    pipeline reads for every other media-bearing type."""
    decrypted = {
        "msgtype": "mixed",
        "mixed": {"item": [{"type": "image", "content": '{"sdkfileid":"sdk-img-1"}'}]},
    }
    normalised = _normalise_fields(decrypted)
    assert normalised["sdkfileid"] is None


def test_normalise_fields_mixed_malformed_payload_never_raises() -> None:
    decrypted = {"msgtype": "mixed", "mixed": "not-a-dict"}
    normalised = _normalise_fields(decrypted)
    assert normalised["structured_content"]["fields"] == {"items": [], "item_count": 0}
    assert normalised["structured_content"]["media_refs"] == []


def test_structured_content_raw_is_scoped_for_mixed_too() -> None:
    decrypted = {
        "msgtype": "mixed",
        "from": "userid1",
        "roomid": "room1",
        "msgtime": 123,
        "tolist": ["userid2"],
        "mixed": {"item": [{"type": "text", "content": "hi"}]},
    }
    normalised = _normalise_fields(decrypted)
    raw = normalised["structured_content"]["raw"]
    assert "from" not in raw
    assert "tolist" not in raw
    assert "msgtime" not in raw
    assert raw == decrypted["mixed"]


# ---------------------------------------------------------------------------
# SF-1 regression guard — the decrypt script has a deliberate, documented
# constraint against ever persisting the full decrypted_payload envelope
# (data-minimization decision; see the "# SF-1" comment at the row-update
# site). RND-197's structured_content column is additive and scoped to
# only the type-specific sub-payload — it must never become a backdoor
# that reintroduces full-envelope persistence.
# ---------------------------------------------------------------------------


def test_decrypted_payload_column_is_still_never_assigned_in_the_decrypt_script() -> None:
    source = inspect.getsource(_decrypt_main)
    assert "record.decrypted_payload" not in source
    assert "record.structured_content" in source


def test_structured_content_raw_is_scoped_to_the_type_specific_sub_payload_only() -> None:
    """structured_content.raw must never equal (or contain) the full
    decrypted envelope (sender/tolist/msgtime/etc.) — only the
    msgtype-specific sub-object."""
    decrypted = {
        "msgtype": "link",
        "from": "userid1",
        "roomid": "room1",
        "msgtime": 123,
        "tolist": ["userid2"],
        "link": {"title": "x", "link_url": "https://example.com"},
    }
    normalised = _normalise_fields(decrypted)
    raw = normalised["structured_content"]["raw"]
    assert "from" not in raw
    assert "tolist" not in raw
    assert "msgtime" not in raw
    assert raw == decrypted["link"]
