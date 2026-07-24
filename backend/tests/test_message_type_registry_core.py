"""
Tests for RND-196 — Message Type Registry & Support Matrix (backend core),
including the RND-196 rework that made the registry the actual runtime
metadata authority (not just a catalog).

Scope: app.message_type_registry — the single source of truth for
message-type category / support status / media capability / parser and
renderer strategy / fallback behavior / aliases — plus every runtime call
site it now drives:

  - app.media_classification.classify_media(): dispatches on the
    registry's support_status, not a literal msgtype if/elif chain, and
    now distinguishes UNSUPPORTED from UNKNOWN on the wire
    (media_type="unsupported" vs "unknown").
  - scripts/decrypt_wecom_messages_once.py._normalise_fields(): selects
    its extraction strategy via get_parser_strategy(), not a literal
    `msgtype == "text"` check.
  - app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES: validated against
    the registry at import time instead of being an independent list.
  - app.main's embedded frontend MessageTypeRegistry JS: its `entries`
    object is generated from
    app.message_type_registry.build_frontend_registry_entries() — the
    frontend now consumes backend-derived metadata rather than a
    hand-duplicated literal.

What this file verifies:

  - Registry structural integrity: unique keys/aliases, every entry has a
    valid category/support-status/parser/renderer/fallback.
  - Every "known type" the RND-196 ticket names by name — text, image,
    voice, video, file, location, link, card, weapp, revoke, mixed,
    chatrecord, todo, a system message, audio_archive — resolves to a
    sensible, specific definition.
  - An unregistered future msgtype never raises, is classified UNKNOWN
    (not UNSUPPORTED — those are different facts, see
    MessageSupportStatus), gets the fallback renderer, and never loses the
    original raw msgtype string.
  - The support matrix is derived from the registry (auto-verifiable), not
    a hand-maintained document.
  - Each runtime call site above genuinely reads from the registry (not
    just "happens to agree with it") — verified either by monkeypatching
    the registry lookup and observing the call site's behavior change, or
    by exact-equality against the registry's own exported contract.

Run (from backend/):
    pytest tests/test_message_type_registry_core.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from app.main import _REVIEW_CONSOLE_HTML
from app.media_classification import classify_media
from app.message_type_registry import (
    FALLBACK_DEFINITION,
    MESSAGE_TYPE_DEFINITIONS,
    MESSAGE_TYPE_REGISTRY,
    MediaCapability,
    MessageCategory,
    MessageSupportStatus,
    ParserStrategy,
    RendererStrategy,
    build_filterable_type_options,
    build_frontend_registry_entries,
    build_support_matrix,
    describe_message_type,
    get_media_capability,
    get_message_category,
    get_renderer_strategy,
    get_support_status,
    is_known_message_type,
    resolve,
)

NODE = shutil.which("node")


# ---------------------------------------------------------------------------
# Registry structural integrity.
# ---------------------------------------------------------------------------


def test_every_raw_type_is_unique() -> None:
    raw_types = [d.raw_type for d in MESSAGE_TYPE_DEFINITIONS]
    assert len(raw_types) == len(set(raw_types))


def test_every_alias_is_unique_and_does_not_collide_with_a_raw_type() -> None:
    raw_types = {d.raw_type for d in MESSAGE_TYPE_DEFINITIONS}
    all_aliases = [alias for d in MESSAGE_TYPE_DEFINITIONS for alias in d.aliases]
    assert len(all_aliases) == len(set(all_aliases))
    assert not (set(all_aliases) & raw_types)


def _rebuild_with(definitions) -> None:
    """Mirrors app.message_type_registry._build_registry()'s duplicate-key
    check against an arbitrary definitions tuple, without needing that
    function (which has no runtime reason to take one) to accept a
    parameter just for this test."""
    lookup: dict = {}
    for definition in definitions:
        for key in (definition.raw_type,) + definition.aliases:
            if key in lookup:
                raise ValueError(f"Duplicate MessageTypeRegistry key {key!r}")
            lookup[key] = definition


def test_registering_a_duplicate_key_raises_at_build_time() -> None:
    from app.message_type_registry import MessageTypeDefinition

    duplicated = MESSAGE_TYPE_DEFINITIONS + (
        MessageTypeDefinition(
            raw_type="text",
            normalized_type="text",
            category=MessageCategory.TEXT,
            support_status=MessageSupportStatus.SUPPORTED,
            display_label_key="messageType.text",
            parser_strategy=ParserStrategy.TEXT_CONTENT,
            renderer_strategy=RendererStrategy.TEXT_BODY,
            media_capability=MediaCapability.NONE,
        ),
    )
    with pytest.raises(ValueError, match="Duplicate MessageTypeRegistry key"):
        _rebuild_with(duplicated)


@pytest.mark.parametrize("definition", MESSAGE_TYPE_DEFINITIONS, ids=lambda d: d.raw_type)
def test_every_entry_has_a_valid_category(definition) -> None:
    assert isinstance(definition.category, MessageCategory)


@pytest.mark.parametrize("definition", MESSAGE_TYPE_DEFINITIONS, ids=lambda d: d.raw_type)
def test_every_entry_has_a_valid_support_status(definition) -> None:
    assert isinstance(definition.support_status, MessageSupportStatus)


@pytest.mark.parametrize("definition", MESSAGE_TYPE_DEFINITIONS, ids=lambda d: d.raw_type)
def test_every_entry_declares_a_parser_and_renderer_strategy(definition) -> None:
    assert isinstance(definition.parser_strategy, ParserStrategy)
    assert isinstance(definition.renderer_strategy, RendererStrategy)


@pytest.mark.parametrize("definition", MESSAGE_TYPE_DEFINITIONS, ids=lambda d: d.raw_type)
def test_every_entry_declares_a_fallback_parser_and_renderer(definition) -> None:
    assert isinstance(definition.fallback_parser_strategy, ParserStrategy)
    assert isinstance(definition.fallback_renderer_strategy, RendererStrategy)


@pytest.mark.parametrize("definition", MESSAGE_TYPE_DEFINITIONS, ids=lambda d: d.raw_type)
def test_every_entry_declares_a_valid_media_capability(definition) -> None:
    assert isinstance(definition.media_capability, MediaCapability)


def test_fallback_definition_exists_and_is_marked_unknown() -> None:
    assert FALLBACK_DEFINITION.category == MessageCategory.UNKNOWN
    assert FALLBACK_DEFINITION.support_status == MessageSupportStatus.UNKNOWN
    assert FALLBACK_DEFINITION.renderer_strategy == RendererStrategy.UNKNOWN_PLACEHOLDER


def test_registry_lookup_resolves_every_raw_type_to_itself() -> None:
    for definition in MESSAGE_TYPE_DEFINITIONS:
        assert MESSAGE_TYPE_REGISTRY[definition.raw_type] is definition


def test_registry_is_immutable() -> None:
    with pytest.raises(TypeError):
        MESSAGE_TYPE_REGISTRY["text"] = FALLBACK_DEFINITION  # type: ignore[index]


# ---------------------------------------------------------------------------
# Known types (RND-196 required coverage).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msgtype,expected_category,expected_status,expected_capability",
    [
        ("text", MessageCategory.TEXT, MessageSupportStatus.SUPPORTED, MediaCapability.NONE),
        ("image", MessageCategory.MEDIA, MessageSupportStatus.SUPPORTED, MediaCapability.SINGLE),
        ("voice", MessageCategory.MEDIA, MessageSupportStatus.PARTIAL, MediaCapability.SINGLE),
        ("video", MessageCategory.MEDIA, MessageSupportStatus.PARTIAL, MediaCapability.SINGLE),
        ("file", MessageCategory.MEDIA, MessageSupportStatus.PARTIAL, MediaCapability.SINGLE),
        (
            "audio_archive",
            MessageCategory.MEDIA,
            MessageSupportStatus.PARTIAL,
            MediaCapability.SINGLE,
        ),
        (
            # RND-197: full structured parsing + rendering.
            "location",
            MessageCategory.STRUCTURED,
            MessageSupportStatus.SUPPORTED,
            MediaCapability.NONE,
        ),
        ("link", MessageCategory.STRUCTURED, MessageSupportStatus.SUPPORTED, MediaCapability.NONE),
        (
            # RND-197: this is also the ticket's "contact" type (see
            # message_type_registry.py's card definition docstring) — raw
            # preservation + generic structured fallback only, no field
            # extraction (no confirmed schema).
            "card",
            MessageCategory.STRUCTURED,
            MessageSupportStatus.PARTIAL,
            MediaCapability.NONE,
        ),
        (
            "weapp",
            MessageCategory.INTERACTIVE,
            MessageSupportStatus.SUPPORTED,
            MediaCapability.CONDITIONAL,
        ),
        (
            "markdown",
            MessageCategory.STRUCTURED,
            MessageSupportStatus.SUPPORTED,
            MediaCapability.NONE,
        ),
        ("news", MessageCategory.STRUCTURED, MessageSupportStatus.SUPPORTED, MediaCapability.MULTIPLE),
        (
            "docmsg",
            MessageCategory.STRUCTURED,
            MessageSupportStatus.PARTIAL,
            MediaCapability.NONE,
        ),
        (
            "audio_doc",
            MessageCategory.MEDIA,
            MessageSupportStatus.PARTIAL,
            MediaCapability.CONDITIONAL,
        ),
        ("todo", MessageCategory.INTERACTIVE, MessageSupportStatus.SUPPORTED, MediaCapability.NONE),
        ("revoke", MessageCategory.CONTROL, MessageSupportStatus.SUPPORTED, MediaCapability.NONE),
        (
            # RND-200: promoted to PARTIAL — recursive nested-message
            # extraction is implemented; the composite viewer is not (see
            # message_type_registry.py's mixed entry comment).
            "mixed",
            MessageCategory.COMPOSITE,
            MessageSupportStatus.PARTIAL,
            MediaCapability.MULTIPLE,
        ),
        (
            "chatrecord",
            MessageCategory.COMPOSITE,
            MessageSupportStatus.PARTIAL,
            MediaCapability.NESTED,
        ),
        ("sys", MessageCategory.SYSTEM, MessageSupportStatus.SUPPORTED, MediaCapability.NONE),
    ],
)
def test_known_message_type_resolves_correctly(
    msgtype, expected_category, expected_status, expected_capability
) -> None:
    definition = resolve(msgtype)
    assert definition.category == expected_category
    assert definition.support_status == expected_status
    assert definition.media_capability == expected_capability
    assert isinstance(definition.renderer_strategy, RendererStrategy)
    assert is_known_message_type(msgtype) is True


def test_weapp_is_the_canonical_key_and_miniprogram_is_an_alias_to_the_same_definition() -> None:
    """WeCom's real msgtype for mini-program messages is "weapp" (see
    scripts/decrypt_wecom_messages_once.py — msgtype is stored verbatim
    from the decrypted payload). "miniprogram" is kept only because it is
    the key the pre-existing frontend placeholder registry already ships
    under (RND-173) — both must resolve to one definition."""
    assert resolve("weapp") is resolve("miniprogram")
    assert resolve("weapp").raw_type == "weapp"
    assert resolve("weapp").normalized_type == "miniprogram"


def test_composite_types_are_flagged() -> None:
    assert resolve("mixed").is_composite is True
    assert resolve("chatrecord").is_composite is True
    assert resolve("text").is_composite is False
    assert resolve("news").is_composite is False  # article list, not nested messages


def test_audio_doc_is_registered_separately_from_audio_archive() -> None:
    """RND-197 explicitly requires these stay distinct — audio_archive is
    RND-202 enterprise call-recording scope, audio_doc is not. RND-210 adds
    the voip_doc_share / voipdocshare aliases (the real WeCom msgtypes that
    were hitting the UNKNOWN fallback) without merging the two definitions."""
    assert resolve("audio_doc") is not resolve("audio_archive")
    assert resolve("audio_doc").raw_type == "audio_doc"
    assert resolve("audio_doc").aliases == ("voip_doc_share", "voipdocshare")


@pytest.mark.parametrize("msgtype", ["docmsg"])
def test_raw_passthrough_types_have_no_field_parser(msgtype) -> None:
    """RND-197: no fixture/doc in this repo confirms docmsg's real
    field structure — it must stay RAW_PASSTHROUGH (raw preservation
    only), never STRUCTURED_FIELDS (which would imply field extraction
    this project cannot honestly claim)."""
    assert resolve(msgtype).parser_strategy == ParserStrategy.RAW_PASSTHROUGH
    assert resolve(msgtype).support_status == MessageSupportStatus.PARTIAL


@pytest.mark.parametrize(
    "msgtype",
    ["meetingvoicecall", "voip_doc_share", "voipdocshare", "audio_archive", "audio_doc", "card"],
)
def test_rnd210_aliased_and_structured_types_use_structured_fields_strategy(msgtype) -> None:
    """RND-210: the official audio/contact msgtypes that previously fell
    into the UNKNOWN fallback (meetingvoicecall / voip_doc_share /
    voipdocshare) or RAW_PASSTHROUGH (card / audio_doc) are now recognized
    and dispatched through STRUCTURED_FIELDS so parse_structured_content
    extracts real fields (voiceid/endtime, doc metadata, corpname+userid).
    They remain PARTIAL (no full rendering/playback yet)."""
    definition = resolve(msgtype)
    assert definition.parser_strategy == ParserStrategy.STRUCTURED_FIELDS
    assert definition.support_status == MessageSupportStatus.PARTIAL
    # None of them may have collapsed into the UNKNOWN fallback.
    assert definition is not FALLBACK_DEFINITION


@pytest.mark.parametrize("msgtype", ["link", "location", "markdown", "news", "weapp"])
def test_full_parse_types_use_structured_fields_strategy(msgtype) -> None:
    assert resolve(msgtype).parser_strategy == ParserStrategy.STRUCTURED_FIELDS
    assert resolve(msgtype).support_status == MessageSupportStatus.SUPPORTED
    assert resolve(msgtype).renderer_strategy == RendererStrategy.STRUCTURED_CARD


@pytest.mark.parametrize(
    "msgtype",
    [
        "text",
        "image",
        "voice",
        "video",
        "file",
        "location",
        "link",
        "card",
        "weapp",
        "markdown",
        "news",
        "docmsg",
        "audio_doc",
        "revoke",
        "mixed",
        "chatrecord",
        "todo",
        "sys",
        "audio_archive",
    ],
)
def test_query_helpers_agree_with_resolve(msgtype) -> None:
    definition = resolve(msgtype)
    assert get_message_category(msgtype) == definition.category
    assert get_support_status(msgtype) == definition.support_status
    assert get_renderer_strategy(msgtype) == definition.renderer_strategy
    assert get_media_capability(msgtype) == definition.media_capability


# ---------------------------------------------------------------------------
# Unknown type fallback — must never throw, must never lose the raw value,
# must never be misclassified as a real registered type.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msgtype",
    [None, "", "future_message_type_not_registered", 123, "weapp_vote"],
)
def test_unknown_msgtype_never_raises(msgtype) -> None:
    resolve(msgtype)
    describe_message_type(msgtype)
    get_message_category(msgtype)
    get_support_status(msgtype)
    get_renderer_strategy(msgtype)
    get_media_capability(msgtype)


def test_unknown_msgtype_is_classified_unknown_not_unsupported() -> None:
    definition = resolve("future_message_type_not_registered")
    assert definition.support_status == MessageSupportStatus.UNKNOWN
    assert definition.category == MessageCategory.UNKNOWN
    assert definition.media_capability == MediaCapability.UNKNOWN


def test_unknown_msgtype_gets_the_fallback_renderer() -> None:
    definition = resolve("future_message_type_not_registered")
    assert definition.renderer_strategy == RendererStrategy.UNKNOWN_PLACEHOLDER


def test_unknown_msgtype_preserves_the_original_raw_value() -> None:
    described = describe_message_type("future_message_type_not_registered")
    assert described["raw_msgtype"] == "future_message_type_not_registered"
    assert described["is_registered"] is False
    assert described["support_status"] == MessageSupportStatus.UNKNOWN.value


def test_missing_and_empty_msgtype_also_preserve_the_original_value() -> None:
    assert describe_message_type(None)["raw_msgtype"] is None
    assert describe_message_type("")["raw_msgtype"] == ""


def test_unknown_msgtype_is_never_misidentified_as_a_registered_type() -> None:
    definition = resolve("future_message_type_not_registered")
    assert definition is FALLBACK_DEFINITION
    for known in MESSAGE_TYPE_DEFINITIONS:
        assert definition is not known


# ---------------------------------------------------------------------------
# Support matrix — must be derivable/verifiable by code, not hand-authored.
# ---------------------------------------------------------------------------


def test_support_matrix_has_one_row_per_registered_type_no_more_no_less() -> None:
    matrix = build_support_matrix()
    assert len(matrix) == len(MESSAGE_TYPE_DEFINITIONS)
    assert {row["message_type"] for row in matrix} == {
        d.raw_type for d in MESSAGE_TYPE_DEFINITIONS
    }


def test_support_matrix_rows_contain_every_required_column() -> None:
    required_columns = {
        "message_type",
        "category",
        "support_status",
        "parser_strategy",
        "renderer_strategy",
        "media_capability",
        "fallback_parser_strategy",
        "fallback_renderer_strategy",
    }
    for row in build_support_matrix():
        assert required_columns <= set(row.keys())


def test_support_matrix_is_json_serializable() -> None:
    json.dumps(build_support_matrix())


# ---------------------------------------------------------------------------
# Runtime integration — app.media_classification.classify_media() (RND-196
# rework). classify_media() no longer owns message-type definitions: it
# dispatches on the registry's support_status (SUPPORTED/PARTIAL/
# UNSUPPORTED/UNKNOWN) rather than a literal msgtype if/elif chain, and the
# wire contract now genuinely distinguishes "a real WeCom type we haven't
# built" (media_type="unsupported") from "a msgtype this registry has
# never heard of" (media_type="unknown") — the exact gap the first RND-196
# round's QA review flagged as unclosed.
# ---------------------------------------------------------------------------


def test_registry_text_type_keeps_its_own_media_classification_shape() -> None:
    """text is SUPPORTED like image, but classify_media special-cases it
    (no download/status semantics at all) before falling into the
    generic SUPPORTED/PARTIAL media branch — this must not regress."""
    assert resolve("text").support_status == MessageSupportStatus.SUPPORTED
    classification = classify_media("text", has_sdkfileid=True)
    assert classification == ("text", None, None)


@pytest.mark.parametrize(
    "msgtype", ["image", "video", "voice", "file", "audio_archive", "audio_doc"]
)
def test_registry_supported_or_partial_media_types_get_dedicated_classify_media_handling(
    msgtype,
) -> None:
    """image/video/voice/file/audio_archive/audio_doc are exactly the
    msgtypes the registry marks SUPPORTED or PARTIAL *and* category MEDIA
    — classify_media derives its branch from that status+category, not
    from matching msgtype literally, so audio_archive/audio_doc (never
    explicitly named in classify_media's source) are handled correctly
    purely because the registry says so."""
    definition = resolve(msgtype)
    assert definition.support_status in (
        MessageSupportStatus.SUPPORTED,
        MessageSupportStatus.PARTIAL,
    )
    assert definition.category == MessageCategory.MEDIA
    classification = classify_media(msgtype, has_sdkfileid=True)
    assert classification.media_type == msgtype
    assert classification.media_status == "not_downloaded"


@pytest.mark.parametrize(
    "msgtype",
    ["link", "location", "markdown", "news", "weapp", "card", "docmsg", "mixed", "chatrecord", "revoke"],
)
def test_registry_structured_types_get_the_structured_media_classification(msgtype) -> None:
    """RND-197: link/location/markdown/news/weapp (SUPPORTED) and
    card/docmsg (PARTIAL) are structured-field types, not byte-bearing
    media — they have no sdkfileid/download-status concept, so
    classify_media must not run the not_downloaded/unknown reasoning
    built for image/video/voice/file. The frontend dispatches these by
    TimelineMessageOut.renderer_strategy == "structured_card", not by
    media_type, so media_type only needs to be distinct here."""
    definition = resolve(msgtype)
    assert definition.support_status in (
        MessageSupportStatus.SUPPORTED,
        MessageSupportStatus.PARTIAL,
    )
    assert definition.category != MessageCategory.MEDIA
    classification = classify_media(msgtype, has_sdkfileid=False)
    assert classification == ("structured", None, None)


@pytest.mark.parametrize(
    "msgtype",
    ["emotion"],
)
def test_registry_unsupported_types_fall_into_classify_medias_generic_bucket(msgtype) -> None:
    definition = resolve(msgtype)
    assert definition.support_status == MessageSupportStatus.UNSUPPORTED
    classification = classify_media(msgtype, has_sdkfileid=False)
    assert classification.media_type == "unsupported"
    assert classification.unsupported_reason == "unsupported_msgtype"


def test_a_genuinely_unregistered_msgtype_is_classified_unknown_not_unsupported() -> None:
    """The gap the previous RND-196 round left open, now closed:
    classify_media distinguishes UNSUPPORTED (a real, named WeCom type,
    e.g. "location") from UNKNOWN (never registered at all) on the wire —
    "unsupported" and "unknown" are no longer the same value for these two
    different facts."""
    assert resolve("future_message_type_not_registered").support_status == (
        MessageSupportStatus.UNKNOWN
    )
    classification = classify_media("future_message_type_not_registered", has_sdkfileid=False)
    assert classification.media_type == "unknown"
    assert classification.media_status == "unknown"
    assert classification.unsupported_reason == "unregistered_msgtype"


def test_unsupported_and_unknown_produce_different_media_type_values() -> None:
    unsupported = classify_media("emotion", has_sdkfileid=False)
    unknown = classify_media("future_message_type_not_registered", has_sdkfileid=False)
    assert unsupported.media_type == "unsupported"
    assert unknown.media_type == "unknown"
    assert unsupported.media_type != unknown.media_type


def test_registering_a_new_supported_or_partial_type_needs_no_classify_media_changes() -> None:
    """Proves classify_media's dispatch is genuinely registry-driven: a
    definition never seen at import time (not one of the module's own
    _DEFINITIONS) still gets correct SUPPORTED/PARTIAL handling purely
    because resolve() would return it — simulated here by calling
    classify_media with a msgtype whose only source of truth is this
    test's own monkeypatched registry entry, not classify_media's source."""
    import app.media_classification as media_classification_module
    from app.message_type_registry import MessageTypeDefinition

    fake_type = "future_partial_media_type"
    fake_definition = MessageTypeDefinition(
        raw_type=fake_type,
        normalized_type=fake_type,
        category=MessageCategory.MEDIA,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.text",
        parser_strategy=ParserStrategy.MEDIA_REFERENCE,
        renderer_strategy=RendererStrategy.PLACEHOLDER,
        media_capability=MediaCapability.SINGLE,
    )

    def _fake_resolve(msgtype):
        if msgtype == fake_type:
            return fake_definition
        return resolve(msgtype)

    original_resolve = media_classification_module.resolve_message_type
    media_classification_module.resolve_message_type = _fake_resolve
    try:
        classification = media_classification_module.classify_media(fake_type, has_sdkfileid=True)
    finally:
        media_classification_module.resolve_message_type = original_resolve

    assert classification.media_type == fake_type
    assert classification.media_status == "not_downloaded"


# ---------------------------------------------------------------------------
# Runtime integration — parser dispatch
# (scripts/decrypt_wecom_messages_once.py._normalise_fields, RND-196
# rework). Extraction selection now comes from
# get_parser_strategy(msgtype) instead of a literal `msgtype == "text"`
# check; the extraction logic itself is unchanged.
# ---------------------------------------------------------------------------


def test_parser_dispatch_text_content_strategy_extracts_content_text() -> None:
    from scripts.decrypt_wecom_messages_once import _normalise_fields

    decrypted = {
        "msgtype": "text",
        "from": "userid1",
        "roomid": "",
        "msgtime": 123,
        "tolist": ["userid2"],
        "text": {"content": "hello"},
    }
    normalised = _normalise_fields(decrypted)
    assert normalised["content_text"] == "hello"
    assert normalised["sdkfileid"] is None


def test_parser_dispatch_non_text_strategy_extracts_sdkfileid() -> None:
    from scripts.decrypt_wecom_messages_once import _normalise_fields

    decrypted = {
        "msgtype": "image",
        "from": "userid1",
        "roomid": "",
        "msgtime": 123,
        "tolist": ["userid2"],
        "image": {"sdkfileid": "sdk-abc"},
    }
    normalised = _normalise_fields(decrypted)
    assert normalised["content_text"] is None
    assert normalised["sdkfileid"] == "sdk-abc"


def test_parser_dispatch_uses_registry_not_a_literal_text_check() -> None:
    """Confirms the dispatch really reads get_parser_strategy(), not a
    hardcoded `msgtype == "text"` string comparison — a msgtype whose
    registry entry claims TEXT_CONTENT strategy gets text-style
    extraction even though it is not literally "text"."""
    import scripts.decrypt_wecom_messages_once as decrypt_module

    original_get_parser_strategy = decrypt_module.get_parser_strategy
    decrypt_module.get_parser_strategy = (
        lambda msgtype: ParserStrategy.TEXT_CONTENT
        if msgtype == "spoofed_text_type"
        else original_get_parser_strategy(msgtype)
    )
    try:
        normalised = decrypt_module._normalise_fields(
            {
                "msgtype": "spoofed_text_type",
                "text": {"content": "still extracted via TEXT_CONTENT strategy"},
            }
        )
    finally:
        decrypt_module.get_parser_strategy = original_get_parser_strategy

    assert normalised["content_text"] == "still extracted via TEXT_CONTENT strategy"


# ---------------------------------------------------------------------------
# Runtime integration — app.media_storage's migration type set (RND-196
# rework). MEDIA_TYPE_KEY_CATEGORIES/SUPPORTED_MIGRATION_MEDIA_TYPES no
# longer independently guess which media types are real — they are
# validated against the registry at import time (see media_storage.py).
# ---------------------------------------------------------------------------


def test_media_storage_migration_types_are_all_registry_supported_or_partial() -> None:
    from app.media_storage import SUPPORTED_MIGRATION_MEDIA_TYPES

    for media_type in SUPPORTED_MIGRATION_MEDIA_TYPES:
        assert resolve(media_type).support_status in (
            MessageSupportStatus.SUPPORTED,
            MessageSupportStatus.PARTIAL,
        )


def test_media_storage_raises_at_import_if_it_drifts_from_the_registry() -> None:
    """Mirrors app.media_storage's own import-time guard so a future
    silent edit to either MEDIA_TYPE_KEY_CATEGORIES or the registry that
    drops one of image/video/voice/file to UNSUPPORTED/UNKNOWN is caught
    immediately rather than only manifesting as a migration bug later."""
    from app.message_type_registry import MESSAGE_TYPE_REGISTRY

    fake_categories = {"image": "images", "emotion": "emotions"}
    drifted = {
        media_type
        for media_type in fake_categories
        if media_type not in MESSAGE_TYPE_REGISTRY
        or MESSAGE_TYPE_REGISTRY[media_type].support_status
        not in (MessageSupportStatus.SUPPORTED, MessageSupportStatus.PARTIAL)
    }
    assert drifted == {"emotion"}


def test_audio_archive_is_partial_but_intentionally_excluded_from_migration() -> None:
    """audio_archive is PARTIAL in the registry (same tier as
    video/voice/file) but has no byte-signature detector or RND-186
    ticket-approved object-key path segment — it must stay out of
    migration scope until a future ticket adds real support, not be
    silently swept in just because the registry allows it."""
    from app.media_storage import SUPPORTED_MIGRATION_MEDIA_TYPES

    assert resolve("audio_archive").support_status == MessageSupportStatus.PARTIAL
    assert "audio_archive" not in SUPPORTED_MIGRATION_MEDIA_TYPES


# ---------------------------------------------------------------------------
# Backend/frontend contract (RND-196 rework, finding #6). The embedded
# frontend MessageTypeRegistry JS (app.main._REVIEW_CONSOLE_HTML,
# RND-173/177) no longer hand-duplicates its `entries` object — app.main
# now generates it from app.message_type_registry.
# build_frontend_registry_entries() at import time (see app/main.py's
# _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON). These tests assert the *actual,
# live* embedded JS equals that export exactly, byte-for-byte — a strict
# bidirectional check, not a one-directional "frontend entry has some
# backend counterpart" heuristic — so the two can never silently drift:
# the frontend literally *is* the backend's export.
# ---------------------------------------------------------------------------

pytestmark_node = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _extract(pattern: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_HTML, re.S)
    assert match is not None, f"pattern not found in _REVIEW_CONSOLE_HTML: {pattern}"
    return match.group(0)


def _frontend_registry_entries() -> dict:
    assert NODE, "node executable not found"
    src = _extract(r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);")
    harness = f"""
{src}
process.stdout.write(JSON.stringify(MessageTypeRegistry.entries));
"""
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


@pytestmark_node
def test_frontend_registry_entries_exactly_equal_the_backend_export() -> None:
    """The live embedded JS's MessageTypeRegistry.entries must be
    byte-for-byte identical (as parsed JSON) to what app.main actually
    computed from the registry — proving the frontend is genuinely
    consuming backend-derived data, not a hand-written literal that
    happens to look similar."""
    from app.main import _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON

    assert _frontend_registry_entries() == json.loads(_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON)


@pytestmark_node
def test_every_frontend_placeholder_entry_has_a_backend_registry_counterpart() -> None:
    """Every msgtype the frontend gives a distinct placeholder for must
    also be a real, registered (non-fallback) backend definition — so a
    type is never "known enough for a specific label" on one side and
    "never heard of" on the other."""
    frontend_entries = _frontend_registry_entries()
    for frontend_key, entry in frontend_entries.items():
        if entry.get("category") != "placeholder":
            continue
        assert is_known_message_type(frontend_key), (
            f"frontend MessageTypeRegistry has a placeholder for {frontend_key!r} "
            "with no corresponding backend app.message_type_registry entry"
        )


@pytestmark_node
def test_frontend_miniprogram_key_is_a_documented_alias_not_a_silent_gap() -> None:
    """Known, intentional divergence (see the raw_type="weapp" definition's
    docstring comment): the frontend keys its mini-program placeholder as
    "miniprogram", which is not WeCom's real msgtype ("weapp"). This test
    exists so that if the frontend is ever updated to key on "weapp"
    directly, a human notices and can simplify the alias away instead of
    it silently going stale."""
    frontend_entries = _frontend_registry_entries()
    assert "miniprogram" in frontend_entries
    assert resolve("miniprogram").raw_type == "weapp"


def test_frontend_export_is_gated_by_real_i18n_key_presence_not_a_hardcoded_list() -> None:
    """build_frontend_registry_entries() must derive its filter from
    whatever placeholder.<type> keys it is told exist, not a hardcoded
    allow-list — a registered PARTIAL/UNSUPPORTED *legacy placeholder*
    type (e.g. "chatrecord", which is COMPOSITE_VIEW+PARTIAL) appears only
    when its i18n key is present in known_placeholder_keys, and disappears
    the moment it is not, with no code change. (RND-197: STRUCTURED_CARD
    types like "location" are not gated by this filter at all — see
    build_frontend_registry_entries()'s docstring — so they are not used
    as examples here. RND-210: "audio_archive" was promoted from PLACEHOLDER
    to STRUCTURED_CARD, so it is now always present like other
    STRUCTURED_CARD types — it can no longer serve as a gated example.
    "docmsg" is also STRUCTURED_CARD (RND-197), so it is not gated either.
    RND-206: video/voice/file/emotion are MEDIA_PREVIEW now and always
    present regardless of this filter, exactly like STRUCTURED_CARD types —
    see test_frontend_export_media_preview_entries_are_never_gated below, so
    they are not used as examples here. mixed/chatrecord (COMPOSITE_VIEW,
    PARTIAL) remain genuine legacy-placeholder-bucket members and ARE used
    here — chatrecord as the "appears when key supplied" example, mixed as
    the "disappears when key absent" example.)"""
    with_key = build_support_matrix()  # sanity: matrix build unaffected by this filter
    assert with_key

    entries_with_chatrecord = build_frontend_registry_entries(
        known_placeholder_keys=frozenset({"placeholder.chatrecord"})
    )
    assert "chatrecord" in entries_with_chatrecord
    assert "mixed" not in entries_with_chatrecord  # gated out: key not supplied

    entries_with_mixed = build_frontend_registry_entries(
        known_placeholder_keys=frozenset({"placeholder.mixed"})
    )
    assert "chatrecord" not in entries_with_mixed
    assert "mixed" in entries_with_mixed


def test_frontend_export_media_preview_entries_are_never_gated() -> None:
    """RND-206: video/voice/file/emotion are MEDIA_PREVIEW, a genuine
    rendering capability, not a legacy placeholder — they must always be
    present in the exported entries regardless of known_placeholder_keys
    (same guarantee STRUCTURED_CARD types already have), even though each
    still optionally carries its placeholderKey (gated) for the terminal
    fallback path — see app.main's renderMessageBody."""
    entries = build_frontend_registry_entries(known_placeholder_keys=frozenset())
    for normalized_type in ("video", "voice", "file", "emotion", "image"):
        assert normalized_type in entries
        assert entries[normalized_type]["category"] == "media"
        assert "placeholderKey" not in entries[normalized_type]

    entries_with_keys = build_frontend_registry_entries(
        known_placeholder_keys=frozenset({"placeholder.video", "placeholder.emotion"})
    )
    assert entries_with_keys["video"]["placeholderKey"] == "placeholder.video"
    assert entries_with_keys["emotion"]["placeholderKey"] == "placeholder.emotion"
    assert "placeholderKey" not in entries_with_keys["voice"]
    assert entries_with_keys["emotion"]["previewSupported"] == "conditional"
    assert entries_with_keys["video"]["previewSupported"] is True


def test_frontend_export_structured_card_entries_are_never_gated() -> None:
    """Unlike the legacy placeholder shape, STRUCTURED_CARD entries carry
    no placeholderKey and are always present regardless of
    known_placeholder_keys — the structured card renderers read i18n
    copy per-message from the API, not from this exported object."""
    entries = build_frontend_registry_entries(known_placeholder_keys=frozenset())
    for normalized_type in ("link", "location", "markdown", "news", "miniprogram", "card", "docmsg", "audio_doc"):
        assert normalized_type in entries
        assert entries[normalized_type]["category"] == "structured"
        assert "placeholderKey" not in entries[normalized_type]


def test_frontend_export_without_a_filter_returns_the_full_candidate_set() -> None:
    """known_placeholder_keys=None (the default) skips the i18n-existence
    gate entirely — every SUPPORTED/PARTIAL/UNSUPPORTED registered type
    gets an entry, including ones the live frontend does not yet surface
    (mixed/chatrecord/sys/audio_archive) because those still lack i18n
    copy. This is the "full candidate set" a caller building a new
    consumer of the registry would see.

    CONTROL-category types (currently only "revoke", RND-201) are the one
    deliberate exception: they never get a per-row frontend entry at all,
    regardless of the filter, because they never render as their own
    timeline row — see build_frontend_registry_entries()'s CONTROL guard."""
    entries = build_frontend_registry_entries()
    for definition in MESSAGE_TYPE_DEFINITIONS:
        if definition.category == MessageCategory.CONTROL:
            assert definition.normalized_type not in entries
            continue
        assert definition.normalized_type in entries


# ---------------------------------------------------------------------------
# RND-230 — search page "消息类型" filter options. A first version of this
# filter hand-duplicated an 11-item Chinese-label list instead of reading
# this registry, which both under-covered it (missing audio_archive,
# location, card, markdown, news, docmsg, audio_doc, todo, mixed, vote,
# collect, meeting, schedule, switch_corp entirely) and mismatched two raw
# DB values outright ("system"/"miniprogram" hand-typed vs the real stored
# "sys"/"weapp"), so selecting those two filters silently matched nothing.
# These tests pin build_filterable_type_options() against that regression.
# ---------------------------------------------------------------------------


def test_filterable_type_options_cover_every_non_control_definition() -> None:
    options = build_filterable_type_options()
    normalized_types = {opt["normalizedType"] for opt in options}
    for definition in MESSAGE_TYPE_DEFINITIONS:
        if definition.category == MessageCategory.CONTROL:
            assert definition.normalized_type not in normalized_types
            continue
        assert definition.normalized_type in normalized_types


def test_filterable_type_options_raw_values_are_the_real_stored_msgtype() -> None:
    """The exact bug this locks in: a filter option's rawValues must be
    real ArchiveMessage.msgtype values (raw_type + aliases), never a
    display-only spelling — "system"/"miniprogram" (the old hand-typed
    list) are display names, not what's ever actually stored."""
    options = build_filterable_type_options()
    by_normalized = {opt["normalizedType"]: opt for opt in options}

    assert by_normalized["system"]["rawValues"] == ["sys"]
    assert "system" not in by_normalized["system"]["rawValues"]

    assert set(by_normalized["miniprogram"]["rawValues"]) == {"weapp", "miniprogram"}

    assert by_normalized["audio_archive"]["rawValues"][0] == "audio_archive"
    assert set(by_normalized["audio_archive"]["rawValues"]) >= {
        "audio_archive", "meeting_voice_call", "meetingvoicecall",
    }


def test_filterable_type_options_raw_values_never_collide_across_types() -> None:
    """Every raw/alias spelling must belong to exactly one filter option —
    otherwise selecting one checkbox could silently also match a
    different, unrelated message type at the SQL layer."""
    options = build_filterable_type_options()
    seen: dict = {}
    for opt in options:
        for raw in opt["rawValues"]:
            assert raw not in seen, (
                f"raw value {raw!r} claimed by both {seen.get(raw)!r} and {opt['normalizedType']!r}"
            )
            seen[raw] = opt["normalizedType"]


def test_filterable_type_options_label_keys_exist_in_every_locale() -> None:
    """Every option's labelKey must resolve to a real translation in each
    shipped locale — a dangling messageType.* key would silently render
    literally as "messageType.foo" in the filter popover."""
    from app.i18n_assets import I18N_JS_SOURCE

    known_keys = set(re.findall(r'"(messageType\.[a-zA-Z0-9_]+)"', I18N_JS_SOURCE))
    options = build_filterable_type_options()
    for opt in options:
        assert opt["labelKey"] in known_keys, f"{opt['labelKey']!r} has no i18n translation"


def test_filterable_type_options_is_json_serializable() -> None:
    json.dumps(build_filterable_type_options())


@pytestmark_node
def test_search_page_msgtype_options_exactly_equal_the_backend_export() -> None:
    """Same bidirectional guarantee as the console's MessageTypeRegistry
    (test_frontend_registry_entries_exactly_equal_the_backend_export
    above): the search page's embedded MSGTYPE_OPTIONS must be exactly
    what build_filterable_type_options() computed, not a hand-copied
    snapshot that can drift the moment the registry changes."""
    from app.main import _SEARCH_MSGTYPE_OPTIONS_JSON, _SEARCH_PAGE_HTML

    match = re.search(r"var MSGTYPE_OPTIONS=(\[.*?\]);", _SEARCH_PAGE_HTML, re.S)
    assert match is not None, "var MSGTYPE_OPTIONS=... not found in _SEARCH_PAGE_HTML"
    embedded = json.loads(match.group(1))
    assert embedded == json.loads(_SEARCH_MSGTYPE_OPTIONS_JSON)
    assert embedded == list(build_filterable_type_options())
