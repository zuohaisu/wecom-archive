"""
Tests for RND-198 — interactive business messages and system events.

Covers every supported type:
  - vote, todo, collect, meeting, schedule, redpacket, switch_corp
  - sys: create_room, update_room, switch_corp, conv_archive_auth, unknown

Each parser test covers: valid complete payload, missing optional fields,
missing required fields, empty payload, malformed payloads, and the
"never raises" contract. Privacy tests verify that sensitive identifiers
(collect.details[].id, sys.member_userid, redpacket totalamount,
switch_corp corpid) are never present in parsed fields.

Also covers parse_structured_content() dispatch for CONTROL_SIGNAL
(sys) types.

Run (from backend/):
    pytest tests/test_rnd_198_parser.py -v
"""

from __future__ import annotations

import pytest

from app.structured_message_parser import (
    parse_vote_message,
    parse_todo_message,
    parse_collect_message,
    parse_meeting_message,
    parse_schedule_message,
    parse_redpacket_message,
    parse_switch_corp_message,
    parse_system_event_message,
    parse_structured_content,
)


# ============================================================================
# parse_vote_message
# ============================================================================


def test_parse_vote_message_valid_complete_payload() -> None:
    fields, warnings = parse_vote_message(
        {
            "votetitle": "Which framework?",
            "voteitem": [
                {"itemname": "React", "count": 15},
                {"itemname": "Vue", "count": 8},
            ],
            "votetype": "single",
            "votestatus": "closed",
        }
    )
    assert fields["title"] == "Which framework?"
    assert fields["type"] == "single"
    assert fields["status"] == "closed"
    assert len(fields["items"]) == 2
    assert fields["items"][0] == {"name": "React", "count": 15}
    assert warnings == []


def test_parse_vote_message_missing_optional_fields() -> None:
    fields, warnings = parse_vote_message({"votetitle": "Title only"})
    assert fields["title"] == "Title only"
    assert fields["type"] is None
    assert fields["status"] is None
    assert fields["items"] == []


def test_parse_vote_message_empty_payload_degrades_safely() -> None:
    fields, warnings = parse_vote_message({})
    assert fields["title"] is None
    assert fields["items"] == []
    assert "missing_title" in warnings


def test_parse_vote_message_malformed_item_list() -> None:
    fields, warnings = parse_vote_message({"votetitle": "x", "voteitem": "not-a-list"})
    assert fields["items"] == []
    assert "malformed_item_list" in warnings


def test_parse_vote_message_non_dict_item_degrades_independently() -> None:
    fields, warnings = parse_vote_message(
        {"votetitle": "x", "voteitem": [{"itemname": "A", "count": 3}, "not-a-dict", 42]}
    )
    assert len(fields["items"]) == 3
    assert fields["items"][0] == {"name": "A", "count": 3}
    assert fields["items"][1] == {"name": None, "count": None}


def test_parse_vote_message_truncates_oversized_list() -> None:
    fields, warnings = parse_vote_message(
        {"votetitle": "x", "voteitem": [{"itemname": f"A{i}"} for i in range(60)]}
    )
    assert len(fields["items"]) == 50
    assert any("truncated" in w for w in warnings)


# ============================================================================
# parse_todo_message
# ============================================================================


def test_parse_todo_message_valid_complete_payload() -> None:
    fields, warnings = parse_todo_message(
        {"title": "Review Q3 plan", "content": "Check budget and timeline"}
    )
    assert fields == {"title": "Review Q3 plan", "content": "Check budget and timeline"}
    assert warnings == []


def test_parse_todo_message_missing_optional_fields() -> None:
    fields, warnings = parse_todo_message({"title": "Just a title"})
    assert fields["title"] == "Just a title"
    assert fields["content"] is None


def test_parse_todo_message_empty_payload_degrades_safely() -> None:
    fields, warnings = parse_todo_message({})
    assert fields["title"] is None
    assert "missing_title" in warnings


# ============================================================================
# parse_collect_message
# ============================================================================


def test_parse_collect_message_valid_complete_payload() -> None:
    fields, warnings = parse_collect_message(
        {
            "title": "Registration form",
            "details": [
                {"id": "user_001", "value": "Alice"},
                {"id": "user_002", "value": "Bob"},
            ],
            "type": "text",
        }
    )
    assert fields["title"] == "Registration form"
    assert fields["type"] == "text"
    assert len(fields["details"]) == 2
    assert warnings == []


def test_parse_collect_message_does_not_expose_id() -> None:
    """Privacy: collect details[].id must not be extracted into fields."""
    fields, warnings = parse_collect_message(
        {
            "title": "Form",
            "details": [{"id": "user_abc_123", "value": "some content"}],
        }
    )
    for detail in fields["details"]:
        assert "id" not in detail, (
            f"collect.details[].id was exposed in fields: {detail}"
        )
    assert fields["details"][0].get("value") == "some content"


def test_parse_collect_message_missing_optional_fields() -> None:
    fields, warnings = parse_collect_message({"title": "Form only"})
    assert fields["title"] == "Form only"
    assert fields["type"] is None
    assert fields["details"] == []


def test_parse_collect_message_empty_payload_degrades_safely() -> None:
    fields, warnings = parse_collect_message({})
    assert fields["title"] is None
    assert "missing_title" in warnings


# ============================================================================
# parse_meeting_message
# ============================================================================


def test_parse_meeting_message_valid_complete_payload() -> None:
    fields, warnings = parse_meeting_message(
        {
            "title": "Q3 Kickoff",
            "meetingtime": 1750348800000,
            "place": "Room 301",
            "agenda": "Review goals",
        }
    )
    assert fields["title"] == "Q3 Kickoff"
    assert fields["time"] == 1750348800000
    assert fields["place"] == "Room 301"
    assert fields["agenda"] == "Review goals"
    assert warnings == []


def test_parse_meeting_message_missing_optional_fields() -> None:
    fields, warnings = parse_meeting_message({"title": "Standup"})
    assert fields["title"] == "Standup"
    assert fields["time"] is None
    assert fields["place"] is None
    assert fields["agenda"] is None


def test_parse_meeting_message_empty_payload_degrades_safely() -> None:
    fields, warnings = parse_meeting_message({})
    assert fields["title"] is None
    assert "missing_title" in warnings


# ============================================================================
# parse_schedule_message
# ============================================================================


def test_parse_schedule_message_valid_complete_payload() -> None:
    fields, warnings = parse_schedule_message(
        {
            "title": "Dentist appointment",
            "starttime": 1750330800000,
            "endtime": 1750334400000,
            "place": "City Clinic",
            "description": "Bring insurance card",
        }
    )
    assert fields["title"] == "Dentist appointment"
    assert fields["starttime"] == 1750330800000
    assert fields["endtime"] == 1750334400000
    assert fields["place"] == "City Clinic"
    assert fields["description"] == "Bring insurance card"
    assert warnings == []


def test_parse_schedule_message_missing_optional_fields() -> None:
    fields, warnings = parse_schedule_message({"title": "Reminder"})
    assert fields["title"] == "Reminder"
    assert fields["starttime"] is None
    assert fields["endtime"] is None
    assert fields["place"] is None
    assert fields["description"] is None


def test_parse_schedule_message_empty_payload_degrades_safely() -> None:
    fields, warnings = parse_schedule_message({})
    assert fields["title"] is None
    assert "missing_title" in warnings


# ============================================================================
# parse_redpacket_message
# ============================================================================


def test_parse_redpacket_message_valid_payload() -> None:
    fields, warnings = parse_redpacket_message(
        {
            "type": "normal",
            "wishing": "Happy New Year!",
            "totalnum": 10,
            "totalamount": 10000,  # MUST NOT be extracted
        }
    )
    assert fields["type"] == "normal"
    assert fields["wishing"] == "Happy New Year!"
    assert fields["totalnum"] == 10
    assert warnings == []


def test_parse_redpacket_message_does_not_expose_monetary_amount() -> None:
    """Privacy: totalamount must never be extracted into fields."""
    fields, warnings = parse_redpacket_message(
        {
            "type": "lucky",
            "wishing": "Good luck",
            "totalamount": 99999,
            "totalnum": 5,
        }
    )
    assert "totalamount" not in fields, (
        f"redpacket.totalamount was exposed in fields: {fields}"
    )


def test_parse_redpacket_message_missing_fields() -> None:
    fields, warnings = parse_redpacket_message({})
    assert fields["type"] is None
    assert fields["wishing"] is None
    assert fields["totalnum"] is None


# ============================================================================
# parse_switch_corp_message
# ============================================================================


def test_parse_switch_corp_message_valid_payload() -> None:
    fields, warnings = parse_switch_corp_message(
        {"corpid": "wpABC123", "corp_name": "Example Corp"}
    )
    assert fields["corp_name"] == "Example Corp"
    assert warnings == []


def test_parse_switch_corp_message_does_not_expose_corpid() -> None:
    """Privacy: corpid must never be extracted into fields."""
    fields, warnings = parse_switch_corp_message(
        {"corpid": "wpSECRET999", "corp_name": "Secret Corp"}
    )
    assert "corpid" not in fields, (
        f"switch_corp.corpid was exposed in fields: {fields}"
    )


def test_parse_switch_corp_message_missing_corp_name() -> None:
    fields, warnings = parse_switch_corp_message({})
    assert fields["corp_name"] is None
    assert "missing_corp_name" in warnings


# ============================================================================
# parse_system_event_message
# ============================================================================


def test_parse_system_event_create_room() -> None:
    fields, warnings = parse_system_event_message(
        {
            "subtype": "create_room",
            "member_userid": "user_abc",
            "member_count": 5,
            "display_text": "Room was created with 5 members",
        }
    )
    assert fields["subtype"] == "create_room"
    assert fields["display_text"] == "Room was created with 5 members"
    assert fields["member_count"] == 5
    # Privacy: member_userid must NOT be in fields
    assert "member_userid" not in fields, (
        f"sys.member_userid was exposed in fields: {fields}"
    )


def test_parse_system_event_update_room() -> None:
    fields, warnings = parse_system_event_message(
        {"subtype": "update_room", "member_count": 3}
    )
    assert fields["subtype"] == "update_room"
    assert fields["member_count"] == 3
    assert fields["display_text"] is None
    assert "member_userid" not in fields


def test_parse_system_event_switch_corp() -> None:
    fields, warnings = parse_system_event_message(
        {
            "subtype": "switch_corp",
            "corp_name": "New Corp",
            "display_text": "Switched to New Corp",
        }
    )
    assert fields["subtype"] == "switch_corp"
    assert fields["corp_name"] == "New Corp"
    assert fields["display_text"] == "Switched to New Corp"


def test_parse_system_event_conv_archive_auth() -> None:
    fields, warnings = parse_system_event_message(
        {"subtype": "conv_archive_auth", "display_text": "Archive authorized"}
    )
    assert fields["subtype"] == "conv_archive_auth"
    assert fields["display_text"] == "Archive authorized"


def test_parse_system_event_unknown_subtype_degrades_safely() -> None:
    """Unknown sys subtypes must not crash, and must preserve the subtype
    for possible display by the frontend fallback."""
    fields, warnings = parse_system_event_message(
        {"subtype": "future_action", "display_text": "Something happened"}
    )
    assert fields["subtype"] == "future_action"
    assert fields["display_text"] == "Something happened"


def test_parse_system_event_missing_subtype() -> None:
    fields, warnings = parse_system_event_message({})
    assert fields["subtype"] is None
    assert "missing_subtype" in warnings


# ============================================================================
# parse_structured_content dispatch — sys (CONTROL_SIGNAL)
# ============================================================================


def test_dispatch_sys_with_action() -> None:
    result = parse_structured_content(
        "sys",
        {
            "msgtype": "sys",
            "action": "create_room",
            "sys": {
                "member_userid": "user_xyz",
                "member_count": 42,
                "display_text": "Room created",
            },
        },
    )
    assert result is not None
    assert result["fields"]["subtype"] == "create_room"
    assert result["fields"]["member_count"] == 42
    assert result["fields"]["display_text"] == "Room created"
    # Privacy: member_userid must NOT be in dispatched fields
    assert "member_userid" not in result["fields"], (
        f"sys.member_userid was exposed via dispatch: {result['fields']}"
    )


def test_dispatch_sys_without_action_returns_missing_action_warning() -> None:
    result = parse_structured_content("sys", {"msgtype": "sys", "sys": {}})
    assert result is not None
    assert result["fields"] is None
    assert "missing_action" in result["parse_warnings"]


def test_dispatch_sys_without_sys_sub_payload() -> None:
    result = parse_structured_content("sys", {"msgtype": "sys", "action": "unknown_event"})
    assert result is not None
    assert result["fields"]["subtype"] == "unknown_event"


# ============================================================================
# Dispatch of interactive types through parse_structured_content()
# ============================================================================


def test_dispatch_vote_through_parse_structured_content() -> None:
    result = parse_structured_content(
        "vote",
        {
            "msgtype": "vote",
            "vote": {
                "votetitle": "Poll",
                "voteitem": [{"itemname": "Yes", "count": 10}],
            },
        },
    )
    assert result is not None
    assert result["fields"]["title"] == "Poll"
    assert len(result["fields"]["items"]) == 1


def test_dispatch_redpacket_through_parse_structured_content_privacy() -> None:
    """Verify the end-to-end dispatch path also strips monetary amount."""
    result = parse_structured_content(
        "redpacket",
        {
            "msgtype": "redpacket",
            "redpacket": {
                "type": "normal",
                "wishing": "Gong xi",
                "totalamount": 50000,
                "totalnum": 8,
            },
        },
    )
    assert result is not None
    assert "totalamount" not in result["fields"], (
        f"redpacket.totalamount leaked through dispatch: {result['fields']}"
    )


# ============================================================================
# Never-raises contract
# ============================================================================


@pytest.mark.parametrize(
    "msgtype,payload",
    [
        ("vote", {"votetitle": 123, "voteitem": None}),
        ("todo", {"title": None, "content": 42}),
        ("collect", {"title": 999, "details": "not-a-list"}),
        ("meeting", {"title": None, "meetingtime": "bad", "place": []}),
        ("schedule", {"title": None, "starttime": "bad", "endtime": {}}),
        ("redpacket", None),
        ("switch_corp", None),
    ],
)
def test_all_parsers_degrade_on_malformed_payload(msgtype, payload) -> None:
    """Every parser must return (fields, warnings) and never raise,
    even on wildly malformed input."""
    from app.structured_message_parser import _STRUCTURED_FIELD_PARSERS

    parser = _STRUCTURED_FIELD_PARSERS.get(msgtype)
    if parser is None:
        pytest.skip(f"no parser for {msgtype}")
    try:
        fields, warnings = parser(payload if payload is not None else {})
    except Exception as e:
        pytest.fail(f"Parser for {msgtype} raised unexpectedly: {e}")
    assert isinstance(fields, dict)
    assert isinstance(warnings, list)


def test_parse_structured_content_never_raises_for_any_rnd_198_type() -> None:
    for msgtype in ("vote", "todo", "collect", "meeting", "schedule", "redpacket", "switch_corp", "sys"):
        result = parse_structured_content(msgtype, {"msgtype": msgtype})
        if msgtype == "sys":
            assert result is not None  # sys always returns a dict
        elif result is not None:
            assert isinstance(result["fields"], dict)
            assert isinstance(result["parse_warnings"], list)