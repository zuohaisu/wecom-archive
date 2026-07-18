"""
Message Type Registry (RND-196).

Single source of truth for "what do we know about a WeCom message
msgtype" — category, support status, i18n label, parser/renderer
*strategy*, media capability, fallback behavior, and aliases. Before this
module, that knowledge was split across three independently-maintained
places that could (and did) silently disagree:

  - app.media_classification.classify_media(): an if/elif chain that only
    distinguishes text/image/video/voice/file explicitly. Every other
    msgtype — whether a real WeCom type this project simply hasn't built
    support for (location, card, weapp, ...) or a msgtype nobody has ever
    seen — collapses into the same media_type="unsupported" bucket. There
    was no way to tell those two situations apart.
  - The embedded frontend `MessageTypeRegistry` JS in app.main
    (_REVIEW_CONSOLE_HTML, RND-173/177): a second, hand-maintained mapping
    of msgtype -> placeholder i18n key, keyed by "miniprogram" — which does
    not match the raw msgtype WeCom actually sends for mini-program
    messages ("weapp"; see decrypted_payload in
    scripts/decrypt_wecom_messages_once.py). See ALIASES below.
  - app.media_storage.MEDIA_TYPE_KEY_CATEGORIES / SUPPORTED_MIGRATION_
    MEDIA_TYPES: a third hardcoded set of "media types that have bytes."

This module does not replace any of those call sites (that would be the
large-scale parser/renderer refactor RND-196 explicitly rules out) — it
establishes the registry as the authoritative metadata source going
forward. New message-type support should mean: register an entry here,
write its parser, write its renderer, write its test — not another
if/elif branch in a fourth location.

Nothing here does DB access, network access, or WeCom API calls — this is
a pure, static, in-memory catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Tuple


class MessageCategory(str, Enum):
    TEXT = "text"
    MEDIA = "media"
    STRUCTURED = "structured"
    INTERACTIVE = "interactive"
    COMPOSITE = "composite"
    SYSTEM = "system"
    CONTROL = "control"
    UNKNOWN = "unknown"


class MessageSupportStatus(str, Enum):
    """Deliberately not a bool — "do we support this" is not a yes/no
    question. PARTIAL covers a type that is recognized and classified but
    not fully renderable (e.g. video is known and its download state is
    tracked, but there is no playback yet). UNKNOWN is reserved for a
    msgtype this registry has never heard of — distinct from UNSUPPORTED,
    which means "we know exactly what this WeCom type is; we haven't built
    it yet." Collapsing those two was the exact gap this ticket closes."""

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class MediaCapability(str, Enum):
    NONE = "none"
    SINGLE = "single"
    MULTIPLE = "multiple"
    NESTED = "nested"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class ParserStrategy(str, Enum):
    """Names a *strategy*, not a callable — RND-196 catalogs which
    approach each type needs; writing the structured parsers themselves
    (location/card/weapp/... field extraction) is RND-197+ scope."""

    TEXT_CONTENT = "text_content"
    MEDIA_REFERENCE = "media_reference"
    STRUCTURED_FIELDS = "structured_fields"
    NESTED_MESSAGES = "nested_messages"
    CONTROL_SIGNAL = "control_signal"
    RAW_PASSTHROUGH = "raw_passthrough"
    UNIMPLEMENTED = "unimplemented"


class RendererStrategy(str, Enum):
    TEXT_BODY = "text_body"
    MEDIA_PREVIEW = "media_preview"
    STRUCTURED_CARD = "structured_card"
    COMPOSITE_VIEW = "composite_view"
    PLACEHOLDER = "placeholder"
    UNSUPPORTED_PLACEHOLDER = "unsupported_placeholder"
    UNKNOWN_PLACEHOLDER = "unknown_placeholder"
    SYSTEM_CARD = "system_card"


@dataclass(frozen=True)
class MessageTypeDefinition:
    """One immutable, fully-specified record for a single WeCom msgtype.

    raw_type is the canonical key messages actually arrive with
    (ArchiveMessage.msgtype / decrypted_payload["msgtype"]) — never a
    display-only name. normalized_type is the stable internal name used
    for grouping/analytics when it differs from raw_type (e.g. "weapp"
    normalizes to "miniprogram" — see ALIASES for why both spellings need
    to resolve to this one definition).
    """

    raw_type: str
    normalized_type: str
    category: MessageCategory
    support_status: MessageSupportStatus
    display_label_key: str
    parser_strategy: ParserStrategy
    renderer_strategy: RendererStrategy
    media_capability: MediaCapability
    fallback_parser_strategy: ParserStrategy = ParserStrategy.RAW_PASSTHROUGH
    fallback_renderer_strategy: RendererStrategy = RendererStrategy.UNSUPPORTED_PLACEHOLDER
    is_composite: bool = False
    aliases: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Registry contents.
#
# Coverage note: text/image are SUPPORTED today (full parse + render
# pipeline). video/voice/file/audio_archive are PARTIAL — classify_media
# already tracks their download state, but there is no playback/render.
# Everything else below is a real, named WeCom message type this project
# has not built rendering for yet (UNSUPPORTED) — as opposed to a msgtype
# absent from this table entirely, which resolve() reports as UNKNOWN (see
# FALLBACK_DEFINITION).
# ---------------------------------------------------------------------------

_DEFINITIONS: Tuple[MessageTypeDefinition, ...] = (
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
    MessageTypeDefinition(
        raw_type="image",
        normalized_type="image",
        category=MessageCategory.MEDIA,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.image",
        parser_strategy=ParserStrategy.MEDIA_REFERENCE,
        renderer_strategy=RendererStrategy.MEDIA_PREVIEW,
        media_capability=MediaCapability.SINGLE,
    ),
    MessageTypeDefinition(
        raw_type="video",
        normalized_type="video",
        category=MessageCategory.MEDIA,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.video",
        parser_strategy=ParserStrategy.MEDIA_REFERENCE,
        renderer_strategy=RendererStrategy.PLACEHOLDER,
        media_capability=MediaCapability.SINGLE,
    ),
    MessageTypeDefinition(
        raw_type="voice",
        normalized_type="voice",
        category=MessageCategory.MEDIA,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.voice",
        parser_strategy=ParserStrategy.MEDIA_REFERENCE,
        renderer_strategy=RendererStrategy.PLACEHOLDER,
        media_capability=MediaCapability.SINGLE,
    ),
    MessageTypeDefinition(
        raw_type="file",
        normalized_type="file",
        category=MessageCategory.MEDIA,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.file",
        parser_strategy=ParserStrategy.MEDIA_REFERENCE,
        renderer_strategy=RendererStrategy.PLACEHOLDER,
        media_capability=MediaCapability.SINGLE,
    ),
    MessageTypeDefinition(
        raw_type="audio_archive",
        normalized_type="audio_archive",
        category=MessageCategory.MEDIA,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.audioArchive",
        parser_strategy=ParserStrategy.MEDIA_REFERENCE,
        renderer_strategy=RendererStrategy.PLACEHOLDER,
        media_capability=MediaCapability.SINGLE,
    ),
    MessageTypeDefinition(
        raw_type="location",
        normalized_type="location",
        category=MessageCategory.STRUCTURED,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.location",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        raw_type="link",
        normalized_type="link",
        category=MessageCategory.STRUCTURED,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.link",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # RND-197: this is also the ticket's "contact" message type — WeCom's
        # 名片/business-card message (corpname + the referenced user) is the
        # closest confirmed match to the ticket's contact-card requirements;
        # no separately-confirmed "contact" raw msgtype exists in this repo's
        # fixtures/docs, so it is not registered as a second, unconfirmed
        # entry (see structured_message_parser.py module docstring). Field
        # extraction is not attempted (RAW_PASSTHROUGH) — no fixture/doc in
        # this repo confirms the card payload's field names beyond
        # "corpname", so only raw preservation + a generic structured
        # fallback card are implemented; see RND-197 dev report.
        raw_type="card",
        normalized_type="card",
        category=MessageCategory.STRUCTURED,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.card",
        parser_strategy=ParserStrategy.RAW_PASSTHROUGH,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        raw_type="emotion",
        normalized_type="emotion",
        category=MessageCategory.MEDIA,
        support_status=MessageSupportStatus.UNSUPPORTED,
        display_label_key="messageType.emotion",
        parser_strategy=ParserStrategy.MEDIA_REFERENCE,
        renderer_strategy=RendererStrategy.UNSUPPORTED_PLACEHOLDER,
        media_capability=MediaCapability.SINGLE,
    ),
    MessageTypeDefinition(
        # raw_type is the real WeCom protocol value (see module docstring);
        # "miniprogram" is kept as an alias because it is the key the
        # pre-existing frontend placeholder registry (RND-173) already
        # shipped under — see test_message_type_registry_core.py's
        # consistency check against that JS registry, and Remaining Risks
        # in the RND-196 report for why the frontend itself is not being
        # changed here.
        raw_type="weapp",
        normalized_type="miniprogram",
        category=MessageCategory.INTERACTIVE,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.miniprogram",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.CONDITIONAL,
        aliases=("miniprogram",),
    ),
    MessageTypeDefinition(
        raw_type="markdown",
        normalized_type="markdown",
        category=MessageCategory.STRUCTURED,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.markdown",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # media_capability=MULTIPLE reflects the article list; not
        # is_composite — that flag is reserved for true nested-*message*
        # types (mixed/chatrecord), which recursively embed other full
        # messages. news's "item" list holds article stubs, not messages,
        # and recursive rendering is explicitly RND-200 scope.
        raw_type="news",
        normalized_type="news",
        category=MessageCategory.STRUCTURED,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.news",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.MULTIPLE,
    ),
    MessageTypeDefinition(
        # RND-197: no fixture/doc in this repo confirms docmsg's real field
        # structure — RAW_PASSTHROUGH only (raw preserved, no field
        # extraction); see structured_message_parser.py and the RND-197 dev
        # report's blocked-fields callout.
        raw_type="docmsg",
        normalized_type="docmsg",
        category=MessageCategory.STRUCTURED,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.docmsg",
        parser_strategy=ParserStrategy.RAW_PASSTHROUGH,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # RND-197: distinct from the existing "audio_archive" entry — do not
        # merge (audio_archive is RND-202 enterprise call-recording scope).
        # No fixture/doc in this repo confirms audio_doc's real field
        # structure — RAW_PASSTHROUGH only, same rationale as docmsg above.
        raw_type="audio_doc",
        normalized_type="audio_doc",
        category=MessageCategory.MEDIA,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.audioDoc",
        parser_strategy=ParserStrategy.RAW_PASSTHROUGH,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.CONDITIONAL,
    ),
    MessageTypeDefinition(
        # RND-198: promoted to SUPPORTED with structured field extraction
        # and structured-card rendering.
        raw_type="todo",
        normalized_type="todo",
        category=MessageCategory.INTERACTIVE,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.todo",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        raw_type="revoke",
        normalized_type="revoke",
        category=MessageCategory.CONTROL,
        support_status=MessageSupportStatus.UNSUPPORTED,
        display_label_key="messageType.revoke",
        parser_strategy=ParserStrategy.CONTROL_SIGNAL,
        renderer_strategy=RendererStrategy.UNSUPPORTED_PLACEHOLDER,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # RND-200: promoted from UNSUPPORTED to PARTIAL — recursive nested-
        # message extraction is now implemented (see
        # app.structured_message_parser.parse_mixed_message), matching
        # PARTIAL's definition exactly: recognized, classified, and now
        # parsed/structured, but not yet renderable (the composite viewer
        # is RND-206 scope). parser_strategy/renderer_strategy/
        # media_capability/is_composite are unchanged.
        #
        # This DOES have one API-visible side effect worth calling out
        # explicitly: app.media_classification.classify_media() branches
        # on (support_status, category), so TimelineMessageOut.media_type
        # for every mixed/chatrecord message changes from "unsupported"
        # (media_status="unsupported", unsupported_reason=
        # "unsupported_msgtype") to "structured" (media_status=None,
        # unsupported_reason=None) — the same bucket link/location/card
        # already report. The embedded frontend (app.main) dispatches on
        # renderer_strategy / normalized_type, never on this media_type
        # value for these two types, so today's rendering is unaffected;
        # any other consumer of this API keying off media_type=="unsupported"
        # for mixed/chatrecord specifically will see "structured" instead
        # after this change ships.
        raw_type="mixed",
        normalized_type="mixed",
        category=MessageCategory.COMPOSITE,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.mixed",
        parser_strategy=ParserStrategy.NESTED_MESSAGES,
        renderer_strategy=RendererStrategy.COMPOSITE_VIEW,
        media_capability=MediaCapability.MULTIPLE,
        fallback_renderer_strategy=RendererStrategy.UNSUPPORTED_PLACEHOLDER,
        is_composite=True,
    ),
    MessageTypeDefinition(
        # RND-200: promoted from UNSUPPORTED to PARTIAL — see the mixed
        # entry's comment above; same rationale applies to chatrecord's
        # recursive extraction (parse_chatrecord_message).
        raw_type="chatrecord",
        normalized_type="chatrecord",
        category=MessageCategory.COMPOSITE,
        support_status=MessageSupportStatus.PARTIAL,
        display_label_key="messageType.chatrecord",
        parser_strategy=ParserStrategy.NESTED_MESSAGES,
        renderer_strategy=RendererStrategy.COMPOSITE_VIEW,
        media_capability=MediaCapability.NESTED,
        fallback_renderer_strategy=RendererStrategy.UNSUPPORTED_PLACEHOLDER,
        is_composite=True,
    ),
    MessageTypeDefinition(
        # RND-198: promoted to SUPPORTED with action-subtype extraction
        # and system-card rendering.
        raw_type="sys",
        normalized_type="system",
        category=MessageCategory.SYSTEM,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.system",
        parser_strategy=ParserStrategy.CONTROL_SIGNAL,
        renderer_strategy=RendererStrategy.SYSTEM_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # RND-198: interactive poll message with structured field extraction.
        raw_type="vote",
        normalized_type="vote",
        category=MessageCategory.INTERACTIVE,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.vote",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # RND-198: interactive collect/form message with structured field extraction.
        raw_type="collect",
        normalized_type="collect",
        category=MessageCategory.INTERACTIVE,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.collect",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # RND-198: interactive meeting invitation with structured field extraction.
        raw_type="meeting",
        normalized_type="meeting",
        category=MessageCategory.INTERACTIVE,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.meeting",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # RND-198: interactive schedule message with structured field extraction.
        raw_type="schedule",
        normalized_type="schedule",
        category=MessageCategory.INTERACTIVE,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.schedule",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # RND-198: red packet (hongbao) message — monetary amount is NEVER
        # extracted into fields (security), only raw-payload preserved.
        raw_type="redpacket",
        normalized_type="redpacket",
        category=MessageCategory.INTERACTIVE,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.redpacket",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
    MessageTypeDefinition(
        # RND-198: use this interactive business type msgtype for "user has
        # been switched to a different corp" — distinct from the sys action
        # subtype of the same name (see sys above). Field extraction
        # preserves corp_name only; corpid is preserved in raw only.
        raw_type="switch_corp",
        normalized_type="switch_corp",
        category=MessageCategory.INTERACTIVE,
        support_status=MessageSupportStatus.SUPPORTED,
        display_label_key="messageType.switchCorp",
        parser_strategy=ParserStrategy.STRUCTURED_FIELDS,
        renderer_strategy=RendererStrategy.STRUCTURED_CARD,
        media_capability=MediaCapability.NONE,
    ),
)

# Fallback used by resolve() for any msgtype not present above (including
# None/empty). category/support_status are UNKNOWN — never UNSUPPORTED —
# because "not in this table" is a strictly different fact from "a WeCom
# type we deliberately haven't built": see MessageSupportStatus docstring.
FALLBACK_DEFINITION = MessageTypeDefinition(
    raw_type="__unknown__",
    normalized_type="unknown",
    category=MessageCategory.UNKNOWN,
    support_status=MessageSupportStatus.UNKNOWN,
    display_label_key="messageType.unknown",
    parser_strategy=ParserStrategy.RAW_PASSTHROUGH,
    renderer_strategy=RendererStrategy.UNKNOWN_PLACEHOLDER,
    media_capability=MediaCapability.UNKNOWN,
    fallback_parser_strategy=ParserStrategy.RAW_PASSTHROUGH,
    fallback_renderer_strategy=RendererStrategy.UNKNOWN_PLACEHOLDER,
)


def _build_registry() -> Mapping[str, MessageTypeDefinition]:
    """Build the raw_type/alias -> definition lookup once at import time.

    Raises at import time (fail fast, never at request time) if two
    definitions claim the same raw_type or alias — that is a registry
    authoring bug, not a runtime condition to recover from.
    """
    lookup: dict = {}
    for definition in _DEFINITIONS:
        keys = (definition.raw_type,) + definition.aliases
        for key in keys:
            if key in lookup:
                raise ValueError(
                    f"Duplicate MessageTypeRegistry key {key!r} claimed by both "
                    f"{lookup[key].raw_type!r} and {definition.raw_type!r}"
                )
            lookup[key] = definition
    return MappingProxyType(lookup)


MESSAGE_TYPE_REGISTRY: Mapping[str, MessageTypeDefinition] = _build_registry()

# Keyed by canonical raw_type only (no aliases) — this is the "one row per
# real message type" view the support matrix and its tests iterate over.
MESSAGE_TYPE_DEFINITIONS: Tuple[MessageTypeDefinition, ...] = _DEFINITIONS


def resolve(msgtype: Optional[str]) -> MessageTypeDefinition:
    """Resolve any msgtype — registered, aliased, missing, empty, or a
    brand-new WeCom type this registry has never heard of — to a
    MessageTypeDefinition. Never raises, never returns None: callers that
    need the raw, unrecognized value (e.g. to preserve it in a payload)
    read msgtype themselves, not through this function.
    """
    if not msgtype:
        return FALLBACK_DEFINITION
    return MESSAGE_TYPE_REGISTRY.get(msgtype, FALLBACK_DEFINITION)


def get_message_type_definition(msgtype: Optional[str]) -> MessageTypeDefinition:
    return resolve(msgtype)


def get_message_category(msgtype: Optional[str]) -> MessageCategory:
    return resolve(msgtype).category


def get_support_status(msgtype: Optional[str]) -> MessageSupportStatus:
    return resolve(msgtype).support_status


def get_renderer_strategy(msgtype: Optional[str]) -> RendererStrategy:
    return resolve(msgtype).renderer_strategy


def get_parser_strategy(msgtype: Optional[str]) -> ParserStrategy:
    return resolve(msgtype).parser_strategy


def get_media_capability(msgtype: Optional[str]) -> MediaCapability:
    return resolve(msgtype).media_capability


def is_known_message_type(msgtype: Optional[str]) -> bool:
    return bool(msgtype) and msgtype in MESSAGE_TYPE_REGISTRY


def describe_message_type(msgtype: Optional[str]) -> dict:
    """Resolve msgtype to its full metadata while explicitly preserving the
    original, unmodified value the caller passed in — including when it is
    missing, empty, or not registered at all. This is the shape a caller
    building an API/UI payload for an unknown message should read from:
    "raw_msgtype" is always the exact input (never the fallback's
    placeholder raw_type "__unknown__"), so an unrecognized message never
    loses its identity even though it is classified as unknown. Never
    raises for any input.
    """
    definition = resolve(msgtype)
    return {
        "raw_msgtype": msgtype,
        "normalized_type": definition.normalized_type,
        "category": definition.category.value,
        "support_status": definition.support_status.value,
        "display_label_key": definition.display_label_key,
        "parser_strategy": definition.parser_strategy.value,
        "renderer_strategy": definition.renderer_strategy.value,
        "media_capability": definition.media_capability.value,
        "fallback_parser_strategy": definition.fallback_parser_strategy.value,
        "fallback_renderer_strategy": definition.fallback_renderer_strategy.value,
        "is_composite": definition.is_composite,
        "is_registered": is_known_message_type(msgtype),
    }


def build_support_matrix() -> Tuple[dict, ...]:
    """Machine-checkable support matrix (RND-196 requirement: the matrix
    must be verifiable by code/tests, not a hand-maintained markdown
    table). One row per canonical registered type, in registry order."""
    return tuple(
        {
            "message_type": d.raw_type,
            "normalized_type": d.normalized_type,
            "category": d.category.value,
            "support_status": d.support_status.value,
            "parser_strategy": d.parser_strategy.value,
            "renderer_strategy": d.renderer_strategy.value,
            "media_capability": d.media_capability.value,
            "fallback_parser_strategy": d.fallback_parser_strategy.value,
            "fallback_renderer_strategy": d.fallback_renderer_strategy.value,
            "is_composite": d.is_composite,
            "aliases": list(d.aliases),
        }
        for d in _DEFINITIONS
    )


def build_frontend_registry_entries(
    known_placeholder_keys: Optional[frozenset] = None,
) -> "Mapping[str, dict]":
    """The candidate {msgtype: {...}} shape the frontend's embedded
    MessageTypeRegistry (app.main._REVIEW_CONSOLE_HTML) needs, derived
    from this registry instead of hand-duplicated (RND-196 rework,
    finding #6 — "Backend Registry becomes the runtime authority;
    Frontend consumes metadata returned by Backend").

    Keyed by normalized_type (so "weapp" is exposed under its frontend
    key "miniprogram" — see the weapp definition's aliases). Every entry
    mirrors one of four shapes:
      - category "text": {"category": "text"}
      - RND-197: renderer_strategy STRUCTURED_CARD (any support_status):
        {"category": "structured", "normalizedType", "supportStatus"} —
        checked before the SUPPORTED/PARTIAL branches below so a
        SUPPORTED structured type (e.g. link, location) is never
        miscategorized as a byte-bearing "media" entry. Unlike the two
        placeholder shapes below, this carries no placeholderKey: the
        structured-card renderers (app.main's renderStructuredCard() and
        friends) read i18n copy per-message from TimelineMessageOut's
        display_label_key / structured_content directly, not from this
        exported entries object — always present regardless of
        known_placeholder_keys, since there is no dangling-key risk to
        gate against.
      - SUPPORTED (byte-bearing media): {"category": "media", "mediaType", "previewSupported": true}
      - PARTIAL/UNSUPPORTED (media-shaped placeholder): {"category":
        "placeholder", "mediaType", "previewSupported": false,
        "placeholderKey": "placeholder.<normalized_type>"}

    known_placeholder_keys, when given, restricts the legacy placeholder
    shape above to types whose "placeholder.<normalized_type>" i18n key
    actually exists — callers (app.main) pass the set of keys defined in
    app.i18n_assets.I18N_JS_SOURCE so this never emits a dangling
    placeholderKey with no translation (that would silently render
    untranslated text, exactly the "disconnected metadata" this ticket's
    QA flagged). Passing None skips that filter — used by tests that want
    the full, unfiltered candidate set.
    """
    entries: dict = {}
    for d in _DEFINITIONS:
        if d.category == MessageCategory.TEXT:
            entries[d.normalized_type] = {"category": "text"}
            continue
        if d.renderer_strategy == RendererStrategy.STRUCTURED_CARD:
            entries[d.normalized_type] = {
                "category": "structured",
                "normalizedType": d.normalized_type,
                "supportStatus": d.support_status.value,
            }
            continue
        if d.renderer_strategy == RendererStrategy.SYSTEM_CARD:
            entries[d.normalized_type] = {
                "category": "system",
                "normalizedType": d.normalized_type,
                "supportStatus": d.support_status.value,
            }
            continue
        if d.support_status == MessageSupportStatus.SUPPORTED:
            entries[d.normalized_type] = {
                "category": "media",
                "mediaType": d.normalized_type,
                "previewSupported": True,
            }
            continue
        if d.support_status in (MessageSupportStatus.PARTIAL, MessageSupportStatus.UNSUPPORTED):
            placeholder_key = f"placeholder.{d.normalized_type}"
            if known_placeholder_keys is not None and placeholder_key not in known_placeholder_keys:
                continue
            entries[d.normalized_type] = {
                "category": "placeholder",
                "mediaType": d.normalized_type,
                "previewSupported": False,
                "placeholderKey": placeholder_key,
            }
    return entries
