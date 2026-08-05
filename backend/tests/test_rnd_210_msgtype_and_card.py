"""
Tests for RND-210 — fix official msgtype mismatch (meeting_voice_call /
meetingvoicecall / voip_doc_share / voipdocshare) and consistent
business-card (名片) rendering, plus the QA FAIL-remediation follow-ups.

Background: the real WeCom audio-archive msgtype is "meeting_voice_call"
(WITH underscores); a no-underscore spelling "meetingvoicecall" also appears
in some payloads. The audio-shared-doc msgtypes are "voip_doc_share" /
"voipdocshare". All of these were matching only the FALLBACK_DEFINITION
(support_status=UNKNOWN), so their media was unreachable, structured fields
were never persisted, and the frontend showed an unknown-type placeholder.
The business-card (card) message showed only a generic "unavailable" fallback
because no fields were extracted, and never resolved the contact's real name
from the tenant contacts registry.

NOTE (false-green guard): every assertion below uses the PRECISE official
wire value ("meeting_voice_call") as the primary input — the original test
used the no-underscore "meetingvoicecall" spelling only, which masked the
real regression where the underscore variant still hit UNKNOWN.

This file asserts the fixed behavior:
  - resolve(): official audio msgtypes (meeting_voice_call incl.) no longer
    UNKNOWN; card/audio_doc/audio_archive are promoted to STRUCTURED_FIELDS
    (field extraction) WITHOUT renaming raw_type/normalized_type.
  - classify_media(): meeting_voice_call / voip_doc_share land in the
    byte-bearing PARTIAL media bucket (not "unknown").
  - parse_structured_content(): persists voiceid/endtime/sdkfileid for the
    real meeting_voice_call payload (so the decrypt layer stores it).
  - build_frontend_registry_entries(): audio_archive is now a recognized
    "structured" type (was PLACEHOLDER -> excluded -> "unknown" in UI).
  - frontend: renderAudioArchiveMessage shows type + end time + explicit
    "not playable"; renderCardMessage surfaces company + resolved contact
    name (+ secondary userid) + "no avatar"; STRUCTURED_CARD_RENDERERS maps
    audio_archive/audio_doc/card to their dedicated renderers.
  - API: the timeline enriches a card's structured_content.fields with
    contact_name resolved from the tenant contacts registry.

Run (from backend/):
    pytest tests/test_rnd_210_msgtype_and_card.py -v
"""

from __future__ import annotations

import json
import re
import shutil

import pytest

from app.db.models import Contact
from app.media_classification import classify_media
from app.message_type_registry import (
    FALLBACK_DEFINITION,
    MessageSupportStatus,
    ParserStrategy,
    build_frontend_registry_entries,
    resolve,
)
from app.structured_message_parser import (
    parse_card_message,
    parse_structured_content,
)
from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_js_source
from tests.test_media_access_descriptor import _authed, client  # noqa: F401
from tests.test_reachability_audit import (  # noqa: F401
    _TENANT_A,
    _insert_message,
    _insert_recipient,
    db,
)

_REVIEW_CONSOLE_JS = review_console_js_source()

NODE = shutil.which("node")
pytestmark_node = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


# ============================================================================
# resolve() — official audio msgtypes must be recognized, not UNKNOWN
# ============================================================================


def test_meeting_voice_call_resolves_out_of_unknown() -> None:
    # Precise official wire value (WITH underscores) — the real regression.
    assert resolve("meeting_voice_call").support_status != MessageSupportStatus.UNKNOWN
    # Alias only — must resolve to the same definition as audio_archive.
    assert resolve("meeting_voice_call") is resolve("audio_archive")
    assert resolve("meeting_voice_call").parser_strategy == ParserStrategy.STRUCTURED_FIELDS


def test_meetingvoicecall_no_underscore_variant_also_resolves() -> None:
    # Defensive: the no-underscore spelling seen in some payloads still works.
    assert resolve("meetingvoicecall").support_status != MessageSupportStatus.UNKNOWN
    assert resolve("meetingvoicecall") is resolve("audio_archive")


def test_voip_doc_share_and_voipdocshare_resolve_out_of_unknown() -> None:
    assert resolve("voip_doc_share").support_status != MessageSupportStatus.UNKNOWN
    assert resolve("voipdocshare").support_status != MessageSupportStatus.UNKNOWN
    assert resolve("voip_doc_share") is resolve("audio_doc")
    assert resolve("voipdocshare") is resolve("audio_doc")
    assert resolve("voip_doc_share").parser_strategy == ParserStrategy.STRUCTURED_FIELDS


def test_card_promoted_to_structured_fields() -> None:
    assert resolve("card").parser_strategy == ParserStrategy.STRUCTURED_FIELDS
    assert resolve("card").support_status == MessageSupportStatus.PARTIAL


def test_aliases_do_not_pollute_fallback_definition() -> None:
    # The UNKNOWN fallback must remain untouched by the new aliases.
    assert resolve("future_message_type_not_registered") is FALLBACK_DEFINITION
    assert resolve("future_message_type_not_registered").support_status == (
        MessageSupportStatus.UNKNOWN
    )


# ============================================================================
# classify_media() — byte-bearing PARTIAL, not "unknown"
# ============================================================================


def test_meeting_voice_call_classified_as_byte_bearing_partial() -> None:
    cls = classify_media("meeting_voice_call", has_sdkfileid=True)
    assert cls.media_type != "unknown"
    assert cls.media_status == "not_downloaded"


def test_voip_doc_share_classified_as_byte_bearing_partial() -> None:
    cls = classify_media("voip_doc_share", has_sdkfileid=True)
    assert cls.media_type != "unknown"
    assert cls.media_status == "not_downloaded"


# ============================================================================
# parse_structured_content() — real field extraction (incl. persistence path)
# ============================================================================


def test_parse_meeting_voice_call_extracts_core_fields() -> None:
    # REAL official WeCom envelope: `voiceid` lives at the TOP LEVEL, while
    # endtime/sdkfileid/demofiledata/sharescreendata live inside the
    # `meeting_voice_call` sub-object. The parser only receives the sub-object,
    # so parse_structured_content must merge the top-level voiceid in. This is
    # exactly the path the decrypt layer calls.
    decrypted = {
        "msgtype": "meeting_voice_call",
        "voiceid": "v-123",
        "meeting_voice_call": {
            "endtime": 1700000000,
            "sdkfileid": "sdk-xyz",
            "demofiledata": [{"filename": "Q4.docx"}],
            "sharescreendata": [{"share": "staff_b"}],
        },
    }
    result = parse_structured_content("meeting_voice_call", decrypted)
    assert result is not None
    fields = result["fields"]
    # voiceid retained from the envelope top level (the round-4 blocker).
    assert fields["voiceid"] == "v-123"
    assert fields["endtime"] == 1700000000
    assert fields["sdkfileid"] == "sdk-xyz"
    # Demo-file / screen-share arrays retained internally (audit completeness).
    assert fields["demofiledata"] == [{"filename": "Q4.docx"}]
    assert fields["sharescreendata"] == [{"share": "staff_b"}]
    assert result["parse_warnings"] == []


def test_parse_meetingvoicecall_no_underscore_variant_extracts_fields() -> None:
    # No-underscore defensive alias uses the same top-level voiceid envelope.
    decrypted = {
        "msgtype": "meetingvoicecall",
        "voiceid": "v-9",
        "meetingvoicecall": {"endtime": 1700000999, "sdkfileid": "sdk-9"},
    }
    result = parse_structured_content("meetingvoicecall", decrypted)
    assert result["fields"]["voiceid"] == "v-9"
    assert result["fields"]["endtime"] == 1700000999


def test_parse_meeting_voice_call_handles_missing_fields_without_raising() -> None:
    result = parse_structured_content(
        "meeting_voice_call", {"msgtype": "meeting_voice_call", "meeting_voice_call": {}}
    )
    assert result["fields"]["voiceid"] is None
    assert "missing_voiceid" in result["parse_warnings"]


def test_parse_voip_doc_share_extracts_doc_metadata() -> None:
    decrypted = {
        "msgtype": "voip_doc_share",
        "voip_doc_share": {"title": "Q3 报告", "url": "https://doc.example.com/x", "docid": "d-9"},
    }
    result = parse_structured_content("voip_doc_share", decrypted)
    fields = result["fields"]
    assert fields["title"] == "Q3 报告"
    assert fields["url"] == "https://doc.example.com/x"
    assert fields["docid"] == "d-9"


def test_parse_card_extracts_corpname_and_userid() -> None:
    decrypted = {"msgtype": "card", "card": {"corpname": "Acme 有限公司", "Userid": "zhangsan"}}
    result = parse_structured_content("card", decrypted)
    fields = result["fields"]
    assert fields["corpname"] == "Acme 有限公司"
    # Official key is "Userid" (capital U); lowercase variant also accepted.
    assert fields["userid"] == "zhangsan"


def test_parse_card_lowercase_userid_variant() -> None:
    result = parse_structured_content(
        "card", {"msgtype": "card", "card": {"corpname": "Acme", "userid": "lisi"}}
    )
    assert result["fields"]["userid"] == "lisi"


def test_parse_card_does_not_fabricate_avatar_or_corpid() -> None:
    # Even if a malicious/garbled payload carries corpid/avatar, they must
    # never be surfaced into structured fields (privacy boundary).
    fields, _warnings = parse_card_message(
        {"corpname": "Acme", "Userid": "zhangsan", "corpid": "secret", "avatar": "http://x/y.png"}
    )
    assert "corpname" in fields
    assert "userid" in fields
    assert "corpid" not in fields
    assert "avatar" not in fields


def test_parse_card_missing_fields_degrades_without_crash() -> None:
    fields, warnings = parse_card_message({})
    assert fields["corpname"] is None
    assert fields["userid"] is None
    assert "missing_corpname" in warnings
    assert "missing_userid" in warnings


# ============================================================================
# Frontend registry — audio_archive must be a recognized structured type
# ============================================================================


def test_audio_archive_is_in_frontend_registry_as_structured() -> None:
    # QA probe: "实际前端 registry 包含 audio_archive" must PASS. Previously
    # audio_archive was PLACEHOLDER (excluded) -> UI showed "unknown".
    entries = build_frontend_registry_entries()
    assert "audio_archive" in entries
    assert entries["audio_archive"]["category"] == "structured"
    assert entries["audio_archive"]["normalizedType"] == "audio_archive"


# ============================================================================
# Frontend rendering (executes the real embedded JS under Node)
# ============================================================================


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_JS, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_JS"
    return match.group(0)


def _bundle() -> str:
    # STRUCTURED_CARD_RENDERERS references every renderer by name. We extract
    # the real ones we test (card/audio_archive/audio_doc/fallback) and stub
    # the rest so the map literal can be evaluated in isolation under Node.
    stub_renderers = [
        "renderLinkCard", "renderLocationCard", "renderMarkdownCard",
        "renderNewsCard", "renderMiniprogramCard", "renderVoteCard",
        "renderTodoCard", "renderCollectCard", "renderMeetingCard",
        "renderScheduleCard", "renderRedpacketCard", "renderSwitchCorpCard",
    ]
    stub = "\n".join(f"function {n}(m){{return '';}}" for n in stub_renderers)
    # isSafeUrl is used by renderAudioDocMessage; provide a minimal real impl.
    stub += "\nfunction isSafeUrl(u){return typeof u==='string'&&/^https?:/i.test(u);}\n"
    parts = [
        stub,
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('en');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\n\}", "pad()"),
        _extract(r"function renderStructuredFallback\(m\)\{.*?\n\}", "renderStructuredFallback()"),
        _extract(r"function renderCardMessage\(m\)\{.*?\n\}", "renderCardMessage()"),
        # Archive Console v2 (design import): audio_archive/audio_doc now
        # share the one structuredCardHeader (dot + label + raw msgtype)
        # every other business card uses.
        _extract(
            r"var CARD_DOT_COLORS=\{.*?\nfunction structuredCardHeader\(labelKey,rawType,dotColor\)\{.*?\n\}",
            "structuredCardHeader",
        ),
        _extract(r"function sphfeedTypeLabel\(feedType\)\{.*?\n\}", "sphfeedTypeLabel()"),
        _extract(r"function renderSphfeedCard\(m\)\{.*?\n\}", "renderSphfeedCard()"),
        _extract(r"function renderAudioArchiveMessage\(m\)\{.*?\n\}", "renderAudioArchiveMessage()"),
        _extract(r"function renderAudioDocMessage\(m\)\{.*?\n\}", "renderAudioDocMessage()"),
        _extract(r"var STRUCTURED_CARD_RENDERERS=\{.*?\n\};", "STRUCTURED_CARD_RENDERERS"),
    ]
    return "\n".join(parts)


def _render_card(fields) -> str:
    bundle = _bundle()
    harness = f"""
{bundle}
var msg = {{
  display_label_key: 'messageType.card',
  structured_content: {json.dumps({"fields": fields})}
}};
process.stdout.write(renderCardMessage(msg));
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


def _render_audio_archive(fields) -> str:
    bundle = _bundle()
    harness = f"""
{bundle}
var msg = {{
  display_label_key: 'messageType.audioArchive',
  structured_content: {json.dumps({"fields": fields})}
}};
process.stdout.write(renderAudioArchiveMessage(msg));
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


@pytestmark_node
def test_render_card_message_shows_corpname_and_contact() -> None:
    html = _render_card({"corpname": "Acme Inc.", "userid": "zhangsan"})
    assert "Acme Inc." in html
    assert "zhangsan" in html
    # Protocol never provides an avatar — state that explicitly, do not fake one.
    assert "avatar not provided by protocol" in html
    # It must NOT be the generic fallback copy.
    assert "Full content unavailable" not in html


@pytestmark_node
def test_render_card_message_shows_resolved_contact_name() -> None:
    # When the backend resolved contact_name from the contacts registry, the
    # timeline shows the real person name (张三) as the primary label and the
    # raw userid as a secondary "Contact ID:" line.
    html = _render_card(
        {"corpname": "Acme Inc.", "userid": "contact_zhangsan", "contact_name": "张三"}
    )
    assert "张三" in html
    assert "Contact ID:" in html
    assert "contact_zhangsan" in html  # shown as the secondary id line
    assert "avatar not provided by protocol" in html


@pytestmark_node
def test_render_card_message_degrades_when_fields_absent() -> None:
    html = _render_card(None)
    # With no fields, renderCardMessage defers to renderStructuredFallback.
    assert "Full content unavailable" in html


@pytestmark_node
def test_render_audio_archive_message_shows_type_endtime_not_playable() -> None:
    # endtime 1700000000 (epoch-seconds) = 2023-11-14 22:13:20 UTC
    # = 2023-11-15 06:13:20 Beijing (UTC+8). fmtTime() renders Beijing time
    # from the UTC epoch, so the displayed value is 2023-11-15 06:13:20.
    html = _render_audio_archive({"endtime": 1700000000, "voiceid": "v-1"})
    assert "Audio archive message" in html
    assert "Playback not supported" in html
    # Formatted Beijing time must appear (NOT "unknown message type").
    assert "2023-11-15 06:13:20" in html
    assert "Unknown message type" not in html


@pytestmark_node
def test_render_audio_archive_message_degrades_without_endtime() -> None:
    html = _render_audio_archive({})
    assert "Audio archive message" in html
    assert "Playback not supported" in html


@pytestmark_node
def test_structured_card_renderers_maps_audio_types_and_card() -> None:
    bundle = _bundle()
    harness = f"""
{bundle}
process.stdout.write(JSON.stringify({{
  card: STRUCTURED_CARD_RENDERERS.card && STRUCTURED_CARD_RENDERERS.card.name,
  audio_archive: STRUCTURED_CARD_RENDERERS.audio_archive && STRUCTURED_CARD_RENDERERS.audio_archive.name,
  audio_doc: STRUCTURED_CARD_RENDERERS.audio_doc && STRUCTURED_CARD_RENDERERS.audio_doc.name
}}));
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    mapping = json.loads(result.stdout)
    assert mapping["card"] == "renderCardMessage"
    # audio_archive now has a dedicated renderer (was PLACEHOLDER -> "unknown").
    assert mapping["audio_archive"] == "renderAudioArchiveMessage"
    # audio_doc shows the shared-doc title + not-playable copy.
    assert mapping["audio_doc"] == "renderAudioDocMessage"


# ============================================================================
# API — card contact_name resolution from the tenant contacts registry
# ============================================================================


def test_card_timeline_resolves_contact_name_from_contacts_registry(client, db) -> None:
    from app.main import app

    # Tenant-scoped contact: userid -> display name.
    db.add(Contact(wecom_userid="contact_zhangsan", name="张三", tenant_id=_TENANT_A))
    db.commit()

    msg = _insert_message(
        db,
        msgid="card-1",
        msgtype="card",
        sender="staff_yingzi",
        roomid=None,
        structured_content={
            "fields": {"corpname": "Acme QA Ltd.", "userid": "contact_zhangsan"},
            "parse_warnings": [],
        },
        msgtime=1600,
    )
    _insert_recipient(db, msg.id, "contact_zhangsan")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            "/api/conversations/direct__contact_zhangsan___staff_yingzi/messages"
            "?mode=staff&staff_id=staff_yingzi"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    msgs = data["messages"]
    assert len(msgs) == 1
    fields = msgs[0]["structured_content"]["fields"]
    # Resolved name present; raw userid + corpname preserved.
    assert fields["contact_name"] == "张三"
    assert fields["userid"] == "contact_zhangsan"
    assert fields["corpname"] == "Acme QA Ltd."


def test_card_timeline_falls_back_to_userid_when_no_contact(client, db) -> None:
    from app.main import app

    # No Contact row for this userid -> contact_name must be absent (no
    # fabrication); raw userid preserved.
    msg = _insert_message(
        db,
        msgid="card-2",
        msgtype="card",
        sender="staff_yingzi",
        roomid=None,
        structured_content={
            "fields": {"corpname": "Acme QA Ltd.", "userid": "contact_unknown"},
            "parse_warnings": [],
        },
        msgtime=1601,
    )
    _insert_recipient(db, msg.id, "contact_unknown")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            "/api/conversations/direct__contact_unknown___staff_yingzi/messages"
            "?mode=staff&staff_id=staff_yingzi"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    fields = resp.json()["messages"][0]["structured_content"]["fields"]
    assert "contact_name" not in fields
    assert fields["userid"] == "contact_unknown"


# ============================================================================
# RND-210 QA FAIL round 2 — public API must NOT leak internal sdkfileid
# ============================================================================


def _find_key_recursive(obj: object, key: str) -> bool:
    """Return True if `key` appears anywhere in the JSON-like structure."""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_find_key_recursive(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_find_key_recursive(v, key) for v in obj)
    return False


def _find_value_recursive(obj: object, needle: object) -> bool:
    """Return True if `needle` (a value) appears anywhere in the structure.

    Used by the QA FAIL round-3 gate: a secret must not leak even if it is
    renamed to a different key (e.g. `sdkfileid` value surfaced as `docid`).
    """
    if obj == needle:
        return True
    if isinstance(obj, dict):
        return any(_find_value_recursive(v, needle) for v in obj.values())
    if isinstance(obj, list):
        return any(_find_value_recursive(v, needle) for v in obj)
    return False


def test_project_public_structured_fields_strips_sdkfileid() -> None:
    from app.routers.conversations import _project_public_structured_fields

    fields = {"voiceid": "v-1", "endtime": 1700000000, "sdkfileid": "secret"}
    out = _project_public_structured_fields("audio_archive", fields)
    assert "sdkfileid" not in out
    assert out["voiceid"] == "v-1"
    assert out["endtime"] == 1700000000


def test_project_public_structured_fields_audio_archive_whitelist() -> None:
    from app.routers.conversations import _project_public_structured_fields

    # Only documented, non-sensitive fields survive for audio_archive; the
    # internal sdkfileid AND any unexpected key are dropped.
    fields = {
        "voiceid": "v-1",
        "endtime": 1700000000,
        "sdkfileid": "secret",
        "shared_doc": {"title": "Q3", "url": "https://x", "docid": "d-1"},
        "unexpected": "leak",
    }
    out = _project_public_structured_fields("audio_archive", fields)
    assert set(out.keys()) == {"voiceid", "endtime", "shared_doc"}
    assert "sdkfileid" not in out
    assert "unexpected" not in out


def test_project_public_structured_fields_sphfeed_whitelist() -> None:
    from app.routers.conversations import _project_public_structured_fields

    fields = {
        "feed_type": 4,
        "sph_name": "Video Channels",
        "feed_desc": "A post",
        "sdkfileid": "must-not-leak",
        "unexpected": "must-not-leak",
    }
    assert _project_public_structured_fields("sphfeed", fields) == {
        "feed_type": 4,
        "sph_name": "Video Channels",
        "feed_desc": "A post",
    }


def test_audio_archive_timeline_does_not_leak_sdkfileid(client, db) -> None:
    from app.db.models import ArchiveMessage
    from app.main import app

    # Persist a meeting_voice_call message WITH sdkfileid — the server must
    # keep it internally (media-download path) but never expose it publicly.
    msg = _insert_message(
        db,
        msgid="audio-leak-1",
        msgtype="meeting_voice_call",
        sender="staff_yingzi",
        roomid=None,
        structured_content={
            "fields": {
                "voiceid": "voice-qa",
                "endtime": 1700000000,
                "sdkfileid": "sdk-secret",
            },
            "parse_warnings": [],
        },
        msgtime=1602,
    )
    _insert_recipient(db, msg.id, "contact_zhangsan")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            "/api/conversations/direct__contact_zhangsan___staff_yingzi/messages"
            "?mode=staff&staff_id=staff_yingzi"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Recursive safety assertion (QA gate): sdkfileid must NEVER appear in the
    # public timeline JSON, at any nesting depth.
    assert not _find_key_recursive(data, "sdkfileid")
    fields = data["messages"][0]["structured_content"]["fields"]
    # Documented, non-sensitive fields are still present and intact.
    assert fields["voiceid"] == "voice-qa"
    assert fields["endtime"] == 1700000000
    # Server-side persistence is preserved (media-download path still works).
    persisted = db.query(ArchiveMessage).filter_by(msgid="audio-leak-1").one()
    assert persisted.structured_content["fields"]["sdkfileid"] == "sdk-secret"


def test_parse_voip_doc_share_never_uses_sdkfileid_as_docid() -> None:
    # QA FAIL round 3 blocker: the internal sdkfileid value must NOT be renamed
    # to the public `docid`. Only a genuine protocol-level docid/fileid is used.
    from app.structured_message_parser import parse_voip_doc_share_message

    fields, _ = parse_voip_doc_share_message({"sdkfileid": "sdk-doc-secret"})
    assert fields["docid"] is None  # NOT "sdk-doc-secret"
    assert "sdk-doc-secret" not in (fields.get("docid") or "")

    # A genuine docid/fileid is still surfaced (not over-stripped).
    fields, _ = parse_voip_doc_share_message(
        {"title": "Q3", "docid": "real-doc-123"}
    )
    assert fields["docid"] == "real-doc-123"
    fields, _ = parse_voip_doc_share_message({"fileid": "real-file-9"})
    assert fields["docid"] == "real-file-9"


def test_voip_doc_share_timeline_does_not_leak_sdkfileid_value(client, db) -> None:
    from app.db.models import ArchiveMessage
    from app.main import app

    # QA FAIL round 3 probe: voip_doc_share carrying ONLY an internal
    # sdkfileid (no real docid/fileid). The secret value "sdk-doc-secret"
    # must not appear anywhere in the public JSON, even renamed to `docid`.
    msg = _insert_message(
        db,
        msgid="doc-leak-1",
        msgtype="voip_doc_share",
        sender="staff_yingzi",
        roomid=None,
        structured_content={
            "fields": {
                "title": "Q3",
                "url": None,
                "sdkfileid": "sdk-doc-secret",
            },
            "parse_warnings": [],
        },
        msgtime=1603,
    )
    _insert_recipient(db, msg.id, "contact_zhangsan")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            "/api/conversations/direct__contact_zhangsan___staff_yingzi/messages"
            "?mode=staff&staff_id=staff_yingzi"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Value-based safety assertion (QA gate): the secret value itself must
    # NEVER appear in the public timeline JSON, regardless of the key name.
    assert not _find_value_recursive(data, "sdk-doc-secret")
    assert not _find_key_recursive(data, "sdkfileid")
    fields = data["messages"][0]["structured_content"]["fields"]
    # The renamed leak path is closed: docid is not the secret.
    assert fields.get("docid") != "sdk-doc-secret"
    assert fields.get("title") == "Q3"
    # Server-side persistence keeps sdkfileid for the media-download path.
    persisted = db.query(ArchiveMessage).filter_by(msgid="doc-leak-1").one()
    assert persisted.structured_content["fields"]["sdkfileid"] == "sdk-doc-secret"


def test_voip_doc_share_timeline_exposes_genuine_docid(client, db) -> None:
    from app.main import app

    # Positive control: a genuine protocol-level docid/fileid IS surfaced
    # (confirm we did not over-strip), while no internal sdkfileid leaks.
    msg = _insert_message(
        db,
        msgid="doc-ok-1",
        msgtype="voip_doc_share",
        sender="staff_yingzi",
        roomid=None,
        structured_content={
            "fields": {
                "title": "Q3",
                "url": "https://docs.example.com/q3",
                "docid": "real-doc-123",
            },
            "parse_warnings": [],
        },
        msgtime=1604,
    )
    _insert_recipient(db, msg.id, "contact_zhangsan")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            "/api/conversations/direct__contact_zhangsan___staff_yingzi/messages"
            "?mode=staff&staff_id=staff_yingzi"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = data["messages"][0]["structured_content"]["fields"]
    assert fields.get("docid") == "real-doc-123"
    assert not _find_key_recursive(data, "sdkfileid")


# ============================================================================
# RND-210 QA FAIL round 4 — official envelope: TOP-LEVEL voiceid must survive
# normalisation + DB persistence and reach the public timeline.
# ============================================================================


def test_meeting_voice_call_official_top_level_voiceid_normalised() -> None:
    # Exact official WeCom payload hierarchy: voiceid at the envelope top
    # level; endtime/sdkfileid/demofiledata/sharescreendata inside the
    # meeting_voice_call sub-object. This is the precise shape the decrypt
    # layer passes to parse_structured_content.
    official = {
        "msgtype": "meeting_voice_call",
        "voiceid": "voice-official-top-level",
        "meeting_voice_call": {
            "endtime": 1700000000,
            "sdkfileid": "sdk-call-secret",
            "demofiledata": [{"filename": "Q4.docx"}],
            "sharescreendata": [{"share": "staff_b"}],
        },
    }
    parsed = parse_structured_content("meeting_voice_call", official)
    assert parsed is not None
    fields = parsed["fields"]
    # The round-4 blocker: top-level voiceid must be retained (was null).
    assert fields["voiceid"] == "voice-official-top-level"
    assert fields["endtime"] == 1700000000
    # Internal sdkfileid is still extracted (server-internal media ref).
    assert fields["sdkfileid"] == "sdk-call-secret"
    # Demo-file / screen-share arrays retained for audit completeness.
    assert fields["demofiledata"] == [{"filename": "Q4.docx"}]
    assert fields["sharescreendata"] == [{"share": "staff_b"}]
    assert "missing_voiceid" not in parsed["parse_warnings"]


def test_meeting_voice_call_timeline_persists_top_level_voiceid(client, db) -> None:
    from app.db.models import ArchiveMessage
    from app.main import app

    # Full chain with the exact official envelope: normalise -> persist ->
    # serve. Confirms voiceid survives normalisation + DB persistence and the
    # public timeline exposes it, while internal sdkfileid and the internal
    # demo/screen-share arrays stay out of the public JSON.
    official = {
        "msgtype": "meeting_voice_call",
        "voiceid": "voice-official-top-level",
        "meeting_voice_call": {
            "endtime": 1700000000,
            "sdkfileid": "sdk-call-secret",
            "demofiledata": [{"filename": "Q4.docx"}],
            "sharescreendata": [{"share": "staff_b"}],
        },
    }
    parsed = parse_structured_content("meeting_voice_call", official)
    assert parsed["fields"]["voiceid"] == "voice-official-top-level"

    msg = _insert_message(
        db,
        msgid="voiceid-official-1",
        msgtype="meeting_voice_call",
        sender="staff_yingzi",
        roomid=None,
        structured_content={
            "fields": parsed["fields"],
            "raw": parsed["raw"],
            "parse_warnings": parsed["parse_warnings"],
        },
        msgtime=1605,
    )
    _insert_recipient(db, msg.id, "contact_zhangsan")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            "/api/conversations/direct__contact_zhangsan___staff_yingzi/messages"
            "?mode=staff&staff_id=staff_yingzi"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = data["messages"][0]["structured_content"]["fields"]
    # voiceid reaches the public timeline (the round-4 blocker's core contract).
    assert fields["voiceid"] == "voice-official-top-level"
    assert fields["endtime"] == 1700000000
    # Internal sdkfileid: never in public JSON (key OR value).
    assert not _find_key_recursive(data, "sdkfileid")
    assert not _find_value_recursive(data, "sdk-call-secret")
    # Demo-file / screen-share retained internally but scrubbed from public by
    # the audio_archive whitelist.
    assert not _find_key_recursive(data, "demofiledata")
    assert not _find_key_recursive(data, "sharescreendata")
    # DB persistence confirms voiceid + internal arrays survive end-to-end.
    persisted = db.query(ArchiveMessage).filter_by(msgid="voiceid-official-1").one()
    assert persisted.structured_content["fields"]["voiceid"] == "voice-official-top-level"
    assert persisted.structured_content["fields"]["sdkfileid"] == "sdk-call-secret"
    assert persisted.structured_content["fields"]["demofiledata"] == [
        {"filename": "Q4.docx"}
    ]
    assert persisted.structured_content["fields"]["sharescreendata"] == [
        {"share": "staff_b"}
    ]
