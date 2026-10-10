"""
Tests for RND-202 — enterprise audio_archive (meeting_voice_call) support:
call metadata parsing, entry into the unified media download/storage/
access pipeline (RND-199), and real frontend playback (mirroring RND-206's
voice <audio> lazy-load path).

Scope boundary (RND-202 ticket, hard constraint): this file proves the
code + fixtures are implemented and behave correctly against SYNTHETIC
payloads modeled on the confirmed official meeting_voice_call envelope
(see app.structured_message_parser.parse_meetingvoicecall_message's
docstring and the RND-210 QA round 4 finding it references) and a real,
self-inflicted-signature "recording" byte string (not a real WeCom
enterprise call recording). It does NOT and CANNOT prove Production
Verified status — that requires a real enterprise-tier WeCom corp with
call-archive permission and a real recorded call, which this environment
does not have. See https://github.com/zuohaisu/wecom-archive/wiki/Audio-Archive-Verification for the
real-environment verification steps and this limitation's full write-up.

Run (from backend/):
    pytest tests/test_rnd_202_audio_archive.py -v
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.media_classification import classify_media
from app.media_download import (
    GENERIC_DOWNLOAD_MSGTYPES,
    _SIGNATURE_CATEGORY_BY_MSGTYPE,
    download_one,
    key_category_for_msgtype,
)
from app.media_storage import (
    MEDIA_TYPE_KEY_CATEGORIES,
    SERVABLE_MEDIA_MSGTYPES,
    SUPPORTED_MIGRATION_MEDIA_TYPES,
)
from app.structured_message_parser import parse_meetingvoicecall_message, parse_structured_content
from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_js_source

# ---------------------------------------------------------------------------
# §3.2.1 — meeting_voice_call fixtures (internal call + external WeCom
# customer call). Shaped per the CONFIRMED real envelope this codebase has
# already reverse-engineered (see parse_structured_content's RND-210 QA
# round 4 comment): `voiceid` sits at the top level of the decrypted
# payload, `endtime`/`sdkfileid`/(optional) `demofiledata`/`sharescreendata`
# live inside decrypted["meeting_voice_call"]. `starttime` is NOT part of
# the officially-confirmed schema -- one fixture includes it (defensive
# probe path), the other omits it (the common case) to prove neither path
# fabricates data.
# ---------------------------------------------------------------------------

INTERNAL_CALL_DECRYPTED = {
    "msgid": "call-internal-1",
    "action": "send",
    "from": "staff_alice",
    "tolist": ["staff_bob"],
    "roomid": "",
    "msgtime": 1751702400,
    "msgtype": "meeting_voice_call",
    "voiceid": "voice-internal-abc123",
    "meeting_voice_call": {
        "starttime": 1751702300,
        "endtime": 1751702400,
        "sdkfileid": "sdkfile-internal-abc123",
    },
}

# External "和微信客户通话" scenario: callee is an external WeCom contact
# (conventionally a "wm..." external-contact id in WeCom's own examples;
# nothing in this codebase asserts that prefix has semantic meaning, it is
# only used here to make the fixture readable as "not a staff_ id"). No
# starttime at all -- the common case per the ticket's own schema
# uncertainty note.
EXTERNAL_CALL_DECRYPTED = {
    "msgid": "call-external-1",
    "action": "send",
    "from": "staff_alice",
    "tolist": ["wmExternalContact001"],
    "roomid": "",
    "msgtime": 1751706000,
    "msgtype": "meeting_voice_call",
    "voiceid": "voice-external-xyz789",
    "meeting_voice_call": {
        "endtime": 1751706120,
        "sdkfileid": "sdkfile-external-xyz789",
    },
}


# ---------------------------------------------------------------------------
# Parser: metadata extraction for both scenarios
# ---------------------------------------------------------------------------


def test_internal_call_fixture_extracts_full_metadata_including_defensive_starttime():
    structured = parse_structured_content("meeting_voice_call", INTERNAL_CALL_DECRYPTED)
    fields = structured["fields"]
    assert fields["voiceid"] == "voice-internal-abc123"
    assert fields["sdkfileid"] == "sdkfile-internal-abc123"
    assert fields["starttime"] == 1751702300
    assert fields["endtime"] == 1751702400
    assert fields["duration_seconds"] == 100
    assert structured["parse_warnings"] == []


def test_external_call_fixture_extracts_metadata_without_fabricating_starttime():
    structured = parse_structured_content("meeting_voice_call", EXTERNAL_CALL_DECRYPTED)
    fields = structured["fields"]
    assert fields["voiceid"] == "voice-external-xyz789"
    assert fields["sdkfileid"] == "sdkfile-external-xyz789"
    assert fields["endtime"] == 1751706120
    # No starttime in the raw payload -- must be absent, never invented,
    # and duration must not be computed from any other proxy (e.g. msgtime).
    assert "starttime" not in fields
    assert "duration_seconds" not in fields


def test_both_scenarios_route_through_the_alias_and_the_no_underscore_variant_too():
    """meetingvoicecall (no underscore) and audio_archive are the same
    definition's raw_type/aliases (message_type_registry.py) -- confirms
    the parser dispatch (parse_structured_content), INCLUDING the
    top-level voiceid merge (RND-210 QA round 4 / RND-202 fix), is reached
    identically regardless of which of the three raw spellings a real
    payload uses. The sub-object is keyed by the SAME raw msgtype string
    WeCom actually sent (its own convention -- decrypted[msgtype]), so
    each spelling gets its own realistically-shaped payload rather than
    reusing "meeting_voice_call" as the sub-object key for every variant."""
    for msgtype in ("meeting_voice_call", "meetingvoicecall", "audio_archive"):
        payload = {
            key: value
            for key, value in INTERNAL_CALL_DECRYPTED.items()
            if key != "meeting_voice_call"
        }
        payload["msgtype"] = msgtype
        payload[msgtype] = INTERNAL_CALL_DECRYPTED["meeting_voice_call"]
        structured = parse_structured_content(msgtype, payload)
        assert structured["fields"]["voiceid"] == "voice-internal-abc123"
        assert structured["fields"]["sdkfileid"] == "sdkfile-internal-abc123"


def test_call_participants_are_the_generic_sender_tolist_fields_not_parser_specific():
    """RND-202 §3.2.2: caller/callee are NOT parsed by
    parse_meetingvoicecall_message -- they are the message envelope's own
    sender/tolist, already persisted generically for every message type.
    This documents (and guards) that design choice explicitly."""
    assert INTERNAL_CALL_DECRYPTED["from"] == "staff_alice"
    assert INTERNAL_CALL_DECRYPTED["tolist"] == ["staff_bob"]
    fields, warnings = parse_meetingvoicecall_message(INTERNAL_CALL_DECRYPTED["meeting_voice_call"])
    assert "from" not in fields and "tolist" not in fields and "sender" not in fields


def test_duration_never_computed_when_endtime_precedes_starttime():
    """Defensive: a clock-skew/malformed pair must never produce a
    negative or nonsensical duration -- degrade to omitting it, exactly
    like the missing-starttime case, never fabricate a value."""
    fields, _warnings = parse_meetingvoicecall_message(
        {"voiceid": "v1", "starttime": 500, "endtime": 400, "sdkfileid": "f1"}
    )
    assert fields["starttime"] == 500
    assert fields["endtime"] == 400
    assert "duration_seconds" not in fields


def test_missing_voiceid_and_sdkfileid_are_flagged_not_crashed():
    fields, warnings = parse_meetingvoicecall_message({})
    assert fields["voiceid"] is None
    assert fields["sdkfileid"] is None
    assert "missing_voiceid" in warnings
    assert "missing_sdkfileid" in warnings


# ---------------------------------------------------------------------------
# §3.2.3 — unified media pipeline entry (classification/storage/download)
# ---------------------------------------------------------------------------


def test_audio_archive_joins_the_migration_and_servable_sets():
    assert "audio_archive" in SUPPORTED_MIGRATION_MEDIA_TYPES
    assert "audio_archive" in SERVABLE_MEDIA_MSGTYPES
    assert "audio_archive" in GENERIC_DOWNLOAD_MSGTYPES
    assert MEDIA_TYPE_KEY_CATEGORIES["audio_archive"] == "call_recordings"
    assert key_category_for_msgtype("audio_archive") == "call_recordings"


def test_audio_archive_recordings_are_detected_as_voice_signature_category():
    """Call recordings are downloaded through the exact same byte-signature
    gate as regular voice messages -- WeCom's GetMediaData for a recording
    returns ordinary voice-format audio, never a distinct container."""
    assert _SIGNATURE_CATEGORY_BY_MSGTYPE["audio_archive"] == "voice"


def test_classify_media_reports_downloadable_when_sdkfileid_present():
    result = classify_media("audio_archive", has_sdkfileid=True)
    assert result.media_type == "audio_archive"
    assert result.media_status == "not_downloaded"


def test_classify_media_degrades_safely_without_enterprise_permission_or_sdkfileid():
    """§3.2.6: no enterprise call-archive permission (or the recording
    genuinely has no sdkfileid) must degrade the row, never crash the
    timeline and never fabricate an available recording."""
    result = classify_media("audio_archive", has_sdkfileid=False)
    assert result.media_type == "audio_archive"
    assert result.media_status == "unsupported"
    assert result.unsupported_reason == "audio_archive_not_implemented"


def test_download_one_accepts_real_voice_format_bytes_for_audio_archive(tmp_path, monkeypatch):
    """End-to-end proof (no real WeCom SDK needed -- same technique as
    tests/test_download_wecom_media_once.py's own download_one() tests):
    a recording's AMR-format bytes are downloaded and stored under the
    dedicated call_recordings key category, tenant-scoped, extension
    derived from the real byte signature (never trusted from sdkfileid).
    """
    import app.media_download as media_download
    from unittest.mock import MagicMock

    amr_bytes = b"#!AMR" + b"call-recording-body-bytes"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([amr_bytes])
    )
    outcome, detail, file_size = download_one(
        MagicMock(),
        MagicMock(),
        tmp_path,
        "tenant-a",
        555,
        "audio_archive",
        "sdk-call-recording-1",
        timeout=5,
    )

    assert outcome == "downloaded"
    final_path = Path(detail)
    assert final_path.suffix == ".amr"
    assert final_path.read_bytes() == amr_bytes
    assert file_size == len(amr_bytes)
    assert "tenants/tenant-a/call_recordings/555.amr" in detail
    # Never shares a directory with regular voice messages.
    assert "/voice/" not in detail


def test_download_one_rejects_non_audio_bytes_for_audio_archive(tmp_path, monkeypatch):
    """The byte-signature gate is content-driven, never trusted from
    msgtype/sdkfileid alone -- a corrupted/mismatched payload (here,
    PDF-looking bytes) for an audio_archive row must be rejected exactly
    like it would be for a regular voice message, and no .part file is
    left behind."""
    import app.media_download as media_download
    from unittest.mock import MagicMock

    not_audio_bytes = b"%PDF-1.4" + b"not actually a call recording"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([not_audio_bytes])
    )
    outcome, detail, _file_size = download_one(
        MagicMock(),
        MagicMock(),
        tmp_path,
        "tenant-a",
        556,
        "audio_archive",
        "sdk-call-recording-2",
        timeout=5,
    )

    assert outcome == "failed"
    assert detail == "unsupported_type"
    directory = tmp_path / "tenants" / "tenant-a" / "call_recordings"
    assert not any(directory.glob("556.*"))


def test_download_one_never_leaks_sdkfileid_in_failure_detail(tmp_path, monkeypatch):
    """Known-pitfalls / secrets discipline: a failure detail string must
    stay a short internal diagnostic tag, never the sdkfileid or any
    other identifier."""
    import app.media_download as media_download
    from unittest.mock import MagicMock

    monkeypatch.setattr(media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([b""]))
    outcome, detail, _file_size = download_one(
        MagicMock(),
        MagicMock(),
        tmp_path,
        "tenant-a",
        557,
        "audio_archive",
        "sdk-super-secret-fileid-should-never-leak",
        timeout=5,
    )

    assert outcome == "failed"
    assert detail is not None
    assert "sdk-super-secret-fileid-should-never-leak" not in detail


# ---------------------------------------------------------------------------
# §3.2.4 — frontend playback / graceful degradation (real Node execution,
# not source-string pattern matching -- same standard test_rnd_206_qa_fixes
# .py's module docstring holds this codebase to).
# ---------------------------------------------------------------------------

NODE = shutil.which("node")

_JS_SOURCE = review_console_js_source()


def _extract(pattern: str, label: str) -> str:
    import re

    match = re.search(pattern, _JS_SOURCE, re.S)
    assert match is not None, f"{label} not found in the review console JS bundle"
    return match.group(0)


def _audio_bundle() -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('en');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\}", "pad()"),
        _extract(r"var CARD_DOT_COLORS=\{.*?\};", "CARD_DOT_COLORS"),
        _extract(r"function structuredCardHeader\(labelKey,rawType,dotColor\)\{.*?\n\}", "structuredCardHeader()"),
        _extract(r"function renderStructuredFallback\(m\)\{.*?\n\}", "renderStructuredFallback()"),
        _extract(r"function richMediaPlaceholder\(kind,accessUrl,loadingKey,extraAttrs\)\{.*?\n\}", "richMediaPlaceholder()"),
        _extract(r"function renderAudioArchiveMessage\(m\)\{.*?\n\}", "renderAudioArchiveMessage()"),
        _extract(r"function fmtDurationSeconds\(totalSeconds\)\{.*?\n\}", "fmtDurationSeconds()"),
    ]
    return "\n".join(parts)


def _run(script_body: str) -> str:
    assert NODE, "node executable not found"
    harness = f"""
{_audio_bundle()}

(function() {{
{script_body}
}})();
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


def _msg(**overrides) -> dict:
    base = {
        "msgid": "call-1",
        "msgtype": "audio_archive",
        "media_status": None,
        "media_access_url": None,
        "recipient_display_names": [],
        "structured_content": {
            "fields": {
                "voiceid": "v1",
                "starttime": 1751702300000,
                "endtime": 1751702400000,
                "duration_seconds": 100,
                "sdkfileid": "f1",
            },
            "parse_warnings": [],
        },
        "display_label_key": "messageType.audioArchive",
    }
    base.update(overrides)
    return base


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_frontend_renders_real_audio_element_when_media_available():
    msg = _msg(media_status="available", media_access_url="/api/conv-1/messages/m1/media/access")
    out = _run(
        f"""
var html = renderAudioArchiveMessage({json.dumps(msg)});
process.stdout.write(html);
"""
    )
    assert 'data-rnd206-kind="voice"' in out
    assert "/api/conv-1/messages/m1/media/access" in out
    assert "Playback not supported" not in out


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_frontend_shows_graceful_placeholder_when_media_not_available():
    """§3.2.6: no enterprise permission / not downloaded yet -- must show
    the existing, explicit "not playable" status, never a broken player
    and never a fabricated access URL."""
    msg = _msg(media_status="unsupported", media_access_url=None)
    out = _run(
        f"""
var html = renderAudioArchiveMessage({json.dumps(msg)});
process.stdout.write(html);
"""
    )
    assert "data-rnd206-access-url" not in out
    assert "Playback not supported" in out


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_frontend_shows_metadata_lines_when_present():
    msg = _msg(media_status="unsupported", media_access_url=None)
    out = _run(
        f"""
var html = renderAudioArchiveMessage({json.dumps(msg)});
process.stdout.write(html);
"""
    )
    assert "Duration" in out  # audioArchive.duration label (en locale)
    assert "1:40" in out  # fmtDurationSeconds(100) == "1:40"


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_frontend_never_fabricates_starttime_or_duration_when_absent():
    msg = _msg(media_status="unsupported", media_access_url=None)
    msg["structured_content"]["fields"] = {
        "voiceid": "v2",
        "endtime": 1751706120000,
        "sdkfileid": "f2",
    }
    out = _run(
        f"""
var html = renderAudioArchiveMessage({json.dumps(msg)});
process.stdout.write(html);
"""
    )
    assert "Started at" not in out
    assert "Duration" not in out


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_frontend_shows_callee_when_recipient_names_present():
    msg = _msg(
        media_status="unsupported",
        media_access_url=None,
        recipient_display_names=["Bob"],
    )
    out = _run(
        f"""
var html = renderAudioArchiveMessage({json.dumps(msg)});
process.stdout.write(html);
"""
    )
    assert "Bob" in out


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_frontend_falls_back_to_structured_fallback_when_fields_missing():
    msg = _msg(structured_content=None)
    out = _run(
        f"""
var html = renderAudioArchiveMessage({json.dumps(msg)});
process.stdout.write(html);
"""
    )
    assert "structured-card-fallback" in out
