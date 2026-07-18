"""Structured message field extraction (RND-197, RND-198).

RND-196's message_type_registry catalogs *which* parsing approach each
msgtype needs (ParserStrategy) without implementing it. This module is
that implementation for the "basic structured message types" ticket
(RND-197): text/image/video/voice/file/audio_archive already have their
own extraction paths elsewhere (content_text, sdkfileid) and are untouched
here.

RND-198 extends the same pattern to interactive business message types
(vote, todo, collect, meeting, schedule, redpacket, switch_corp) and
system events (sys with action-subtype dispatch).

Coverage split, and why it isn't uniform:

  STRUCTURED_FIELDS (real field extraction) -- link, location, markdown,
  news, weapp (miniprogram). These have stable, well-documented WeCom
  archive-SDK payload shapes; parse_structured_content() dispatches to a
  dedicated parser per type below.

  RND-198 types: vote, todo, collect, meeting, schedule, redpacket,
  switch_corp also use STRUCTURED_FIELDS. Field names are the best-known
  WeCom Conversation Archive API payload shapes (no fixtures exist in this
  repo); all parsers degrade gracefully.

  RAW_PASSTHROUGH (raw preservation only, no field extraction) -- card
  (the ticket's "contact"), docmsg, audio_doc. No fixture, sample payload,
  or authoritative doc exists anywhere in this repository confirming
  these three types' actual field names (see RND-197 audit / dev report).
  Rather than fabricate a field mapping from the msgtype name alone (the
  ticket explicitly forbids this), these three are registered with
  support_status=PARTIAL and get only: the raw type-specific sub-payload
  preserved verbatim, and a generic structured fallback card on the
  frontend. Confirming their real schema against WeCom's official
  Finance/Conversation-Archive SDK docs is a recommended follow-up, not
  attempted here.

  CONTROL_SIGNAL (action-subtype dispatch) -- sys (system events). The
  action field is extracted from the decrypted payload envelope and the
  sys sub-payload is preserved raw.

  NESTED_MESSAGES (recursive extraction) -- mixed, chatrecord (RND-200).
  Each embeds a list of child messages under payload["item"]; every child
  is normalized to a common node shape (type/text/fields/media/sender/
  timestamp/children), recursively, so mixed-in-mixed / chatrecord-in-
  mixed nesting is preserved rather than flattened. No fixture, sample
  payload, or authoritative doc exists anywhere in this repository
  confirming the exact field names WeCom uses inside a nested item's
  content (or a chatrecord item's sender fields) -- see _parse_nested_item
  for the defensive multi-key-candidate approach this uses instead of
  assuming one, mirroring parse_miniprogram_message's icon field lookup
  and parse_news_message's image field lookup above. A child whose type
  this module does not recognize is still surfaced (type + best-effort
  text), never dropped -- ticket requirement. media_refs (a third element
  these two parsers return, alongside fields/warnings) is a flat,
  server-internal-only list of {"path","type","sdkfileid"} for every
  media-bearing nested item, consumed by the download pipeline
  (app.media_download) to register MediaFile rows via the existing RND-199
  pipeline; it is never included in the "fields" dict and callers must
  never serialize it to an API response (same privacy boundary as raw).

Every parser in this module is a pure function: it takes the
msgtype-specific sub-payload dict (i.e. decrypted.get(msgtype, {})) and
never raises -- malformed/partial/historical-dirty input degrades to
missing fields plus a parse_warnings entry, never an exception. This
mirrors message_type_registry.py's "never raises" contract so a single
bad row can never fail an entire timeline page.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlparse

from app.message_type_registry import ParserStrategy, resolve as resolve_message_type

# Schemes allowed through safe_url(). http/https only -- rejects
# javascript:/data:/vbscript: and anything else (including schemeless
# values, which are ambiguous rather than obviously safe).
_SAFE_URL_SCHEMES = frozenset({"http", "https"})

# Defensive upper bound on how many news articles a single message is
# allowed to contribute to the normalized contract -- a malformed or
# adversarial payload with an enormous item list must not blow up
# response size or render time. Excess items are dropped with a warning,
# never silently truncated without a trace (ticket requirement).
_NEWS_ITEM_CAP = 20

# Defensive upper bound on vote items — a malformed payload with an
# enormous item list must not blow up response size.
_VOTE_ITEM_CAP = 50

# Defensive upper bound on collect details — same rationale as above.
_COLLECT_DETAIL_CAP = 50

# Defensive upper bounds for mixed/chatrecord recursive extraction (RND-200)
# -- a malformed or adversarial payload must not be able to exhaust memory
# via an enormous flat item list, nor blow the Python recursion limit via
# extreme mixed-in-mixed nesting depth. _MIXED_ITEM_CAP counts every node
# visited across the *entire* tree (not per level), so a wide-but-shallow
# and a narrow-but-deep payload are both bounded by the same budget.
# Neither limit is reachable by any real WeCom payload this project has
# seen documented; both exist purely as a safety backstop (ticket
# requirement: "do not artificially limit nesting depth unless necessary
# for safety"). Excess nodes are dropped (never included, never partially
# parsed) with a parse_warnings entry, same truncation-with-a-trace pattern
# as _NEWS_ITEM_CAP/_VOTE_ITEM_CAP above.
_MIXED_ITEM_CAP = 200
_MIXED_MAX_DEPTH = 8

# WeCom child "type" values that are a bare/plain-text payload.
_NESTED_TEXT_TYPES = frozenset({"text"})

# Child types whose bytes are downloadable media (reuses the RND-199
# unified media pipeline's own msgtype vocabulary -- see
# app.media_download._SIGNATURE_CATEGORY_BY_MSGTYPE, which this set must
# stay a subset of so every media_refs entry's "type" is one
# download_one() already knows how to handle).
_NESTED_MEDIA_TYPES = frozenset({"image", "voice", "video", "file", "emotion"})

# Child types with their own STRUCTURED_FIELDS parser above -- reused
# as-is for a nested occurrence rather than re-implemented -- PLUS the
# three RAW_PASSTHROUGH-strategy types (card/docmsg/audio_doc), which have
# no field parser to call (_STRUCTURED_FIELD_PARSERS.get() returns None
# for them) but must still resolve to "supported=True, fields=None" here,
# matching their top-level PARTIAL/RAW_PASSTHROUGH treatment in
# parse_structured_content -- a nested card/docmsg/audio_doc child is a
# recognized type this project simply doesn't extract fields for, not an
# unknown one.
_NESTED_STRUCTURED_JSON_TYPES = frozenset(
    {
        "link",
        "location",
        "markdown",
        "news",
        "weapp",
        "vote",
        "todo",
        "collect",
        "meeting",
        "schedule",
        "redpacket",
        "switch_corp",
        "card",
        "docmsg",
        "audio_doc",
    }
)

# Child types that are themselves a nested composite -- recursion point.
_NESTED_COMPOSITE_TYPES = frozenset({"mixed", "chatrecord"})

_NESTED_KNOWN_TYPES = (
    _NESTED_TEXT_TYPES | _NESTED_MEDIA_TYPES | _NESTED_STRUCTURED_JSON_TYPES | _NESTED_COMPOSITE_TYPES
)


def safe_url(url: Any) -> Optional[str]:
    """Return url unchanged if it is a plain http(s) URL, else None.

    Deliberately conservative: only a string with an http/https scheme
    and a network location passes. Used for every URL surfaced from a
    structured message payload (link.url/image_url, news article url/
    image_url, weapp icon) so an unsafe scheme from raw WeCom payload
    data can never reach a frontend href/src attribute.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _SAFE_URL_SCHEMES or not parsed.netloc:
        return None
    return url


def _clean_str(value: Any) -> Optional[str]:
    """Coerce a payload value to a non-empty stripped string, or None.

    Tolerates the malformed-historical-data cases the ticket calls out
    explicitly: numbers where strings are expected, None, empty string.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


def _safe_int(value: Any) -> Optional[int]:
    """Coerce a payload value to int, or None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    """Coerce a payload value to float, or None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_item_list(raw_items: Any, item_cap: int) -> tuple[list[dict], list[str]]:
    """Parse an array of dict items, returning (parsed_items, warnings).

    Each item is independently degraded (not-a-dict → empty fields) so a
    single malformed entry cannot drop the entire list.
    """
    warnings: list[str] = []
    if not isinstance(raw_items, list):
        if raw_items is not None:
            warnings.append("malformed_item_list")
        raw_items = []
    truncated = len(raw_items) > item_cap
    if truncated:
        warnings.append(f"item_list_truncated_at_{item_cap}")
    return raw_items[:item_cap], warnings


def parse_link_message(payload: dict) -> tuple[dict, list[str]]:
    """WeCom link message: {"title", "description", "link_url", "image_url"}."""
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    title = _clean_str(payload.get("title"))
    description = _clean_str(payload.get("description"))

    url = safe_url(payload.get("link_url"))
    if payload.get("link_url") and url is None:
        warnings.append("unsafe_or_malformed_url")

    image_url = safe_url(payload.get("image_url"))
    if payload.get("image_url") and image_url is None:
        warnings.append("unsafe_or_malformed_image_url")

    if not title:
        warnings.append("missing_title")
    if not url:
        warnings.append("missing_url")

    fields = {
        "title": title,
        "description": description,
        "url": url,
        "image_url": image_url,
    }
    return fields, warnings


def parse_location_message(payload: dict) -> tuple[dict, list[str]]:
    """WeCom location message: {"latitude", "longitude", "address", "title", "zoom"}."""
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    try:
        raw_lat = payload.get("latitude")
        raw_lon = payload.get("longitude")
        if raw_lat is not None and raw_lon is not None:
            lat_f = float(raw_lat)
            lon_f = float(raw_lon)
            if -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0:
                latitude, longitude = lat_f, lon_f
            else:
                warnings.append("coordinates_out_of_range")
    except (TypeError, ValueError):
        warnings.append("malformed_coordinates")

    address = _clean_str(payload.get("address"))
    name = _clean_str(payload.get("title"))

    zoom: Optional[int] = None
    raw_zoom = payload.get("zoom")
    if raw_zoom is not None:
        try:
            zoom = int(raw_zoom)
        except (TypeError, ValueError):
            pass

    if not name and not address and latitude is None:
        warnings.append("missing_location_data")

    fields = {
        "name": name,
        "address": address,
        "latitude": latitude,
        "longitude": longitude,
        "zoom": zoom,
    }
    return fields, warnings


def parse_markdown_message(payload: dict) -> tuple[dict, list[str]]:
    """WeCom markdown message: {"content"}. Sanitization happens client-side
    on render, not here -- this only extracts the raw markdown string."""
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}
    content = _clean_str(payload.get("content"))
    if not content:
        warnings.append("missing_content")
    return {"content": content}, warnings


def _parse_news_item(item: Any) -> dict:
    if not isinstance(item, dict):
        return {"title": None, "description": None, "url": None, "image_url": None}
    url = safe_url(item.get("url"))
    # Field name for the article thumbnail is not perfectly uniform across
    # WeCom's news/图文消息 payload variants observed in the wild -- check
    # the plausible candidates defensively rather than assuming one.
    image_url = None
    for key in ("image_url", "pic_url", "picurl"):
        image_url = safe_url(item.get(key))
        if image_url:
            break
    return {
        "title": _clean_str(item.get("title")),
        "description": _clean_str(item.get("description")),
        "url": url,
        "image_url": image_url,
    }


def parse_news_message(payload: dict) -> tuple[dict, list[str]]:
    """WeCom news (图文) message: {"item": [{"title","description","url","pic_url"}, ...]}.

    Structurally multi-article -- normalized as a list, source order
    preserved, each article degraded independently rather than the whole
    message failing when one article is malformed.
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}
    raw_items = payload.get("item")
    if not isinstance(raw_items, list):
        raw_items = []
        if payload.get("item") is not None:
            warnings.append("malformed_item_list")

    if not raw_items:
        warnings.append("empty_article_list")

    truncated = len(raw_items) > _NEWS_ITEM_CAP
    if truncated:
        warnings.append(f"article_list_truncated_at_{_NEWS_ITEM_CAP}")

    articles = [_parse_news_item(item) for item in raw_items[:_NEWS_ITEM_CAP]]
    return {"articles": articles}, warnings


def parse_miniprogram_message(payload: dict) -> tuple[dict, list[str]]:
    """WeCom weapp (小程序) message.

    title/appid/username/pagepath are high-confidence, stable field
    names. The icon/cover field name is checked defensively across a
    couple of plausible candidates -- lower confidence, flagged in the
    RND-197 dev report; pagepath is an internal mini-program route, never
    treated as an external browser URL (ticket requirement -- enforced on
    render, not here).
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    title = _clean_str(payload.get("title"))
    appid = _clean_str(payload.get("appid"))
    username = _clean_str(payload.get("username"))
    pagepath = _clean_str(payload.get("pagepath"))
    display_name = _clean_str(payload.get("displayname"))

    icon_url = None
    for key in ("cover_url", "thumb_url", "icon_url"):
        icon_url = safe_url(payload.get(key))
        if icon_url:
            break

    if not title:
        warnings.append("missing_title")
    if not appid:
        warnings.append("missing_appid")

    fields = {
        "title": title,
        "display_name": display_name,
        "appid": appid,
        "username": username,
        "pagepath": pagepath,
        "icon_url": icon_url,
    }
    return fields, warnings


def parse_vote_message(payload: dict) -> tuple[dict, list[str]]:
    """RND-198: WeCom vote/poll message.

    Expected fields: votetitle, voteitem (list of {itemname, count}),
    votetype, votestatus. No voter userids are extracted — only aggregate
    item names and counts.
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    title = _clean_str(payload.get("votetitle"))
    if not title:
        warnings.append("missing_title")

    raw_items = payload.get("voteitem")
    items, item_warnings = _parse_item_list(raw_items, _VOTE_ITEM_CAP)
    warnings.extend(item_warnings)

    parsed_items = []
    for raw_item in items:
        if isinstance(raw_item, dict):
            parsed_items.append({
                "name": _clean_str(raw_item.get("itemname")),
                "count": _safe_int(raw_item.get("count")),
            })
        else:
            parsed_items.append({"name": None, "count": None})

    vote_type = _clean_str(payload.get("votetype"))
    status = _clean_str(payload.get("votestatus"))

    fields = {
        "title": title,
        "items": parsed_items,
        "type": vote_type,
        "status": status,
    }
    return fields, warnings


def parse_todo_message(payload: dict) -> tuple[dict, list[str]]:
    """RND-198: WeCom todo message.

    Expected fields: title, content.
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    title = _clean_str(payload.get("title"))
    content = _clean_str(payload.get("content"))
    if not title:
        warnings.append("missing_title")

    fields = {
        "title": title,
        "content": content,
    }
    return fields, warnings


def parse_collect_message(payload: dict) -> tuple[dict, list[str]]:
    """RND-198: WeCom collect (form/collection) message.

    Expected fields: title, details (list of items), type.
    Participant identifiers in details are intentionally not extracted.
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    title = _clean_str(payload.get("title"))
    if not title:
        warnings.append("missing_title")

    raw_details = payload.get("details")
    details, detail_warnings = _parse_item_list(raw_details, _COLLECT_DETAIL_CAP)
    warnings.extend(detail_warnings)

    parsed_details = []
    for raw_item in details:
        # Privacy: collect details[].id is a participant/entry identifier
        # and must not be extracted into fields (exposed via API).
        # Only the display value is extracted.
        if isinstance(raw_item, dict):
            parsed_details.append({
                "value": _clean_str(raw_item.get("value")),
            })
        else:
            parsed_details.append({"value": None})

    collect_type = _clean_str(payload.get("type"))

    fields = {
        "title": title,
        "details": parsed_details,
        "type": collect_type,
    }
    return fields, warnings


def parse_meeting_message(payload: dict) -> tuple[dict, list[str]]:
    """RND-198: WeCom meeting invitation message.

    Expected fields: title, meetingtime, place, agenda.
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    title = _clean_str(payload.get("title"))
    if not title:
        warnings.append("missing_title")

    meeting_time = _safe_int(payload.get("meetingtime"))
    place = _clean_str(payload.get("place"))
    agenda = _clean_str(payload.get("agenda"))

    fields = {
        "title": title,
        "time": meeting_time,
        "place": place,
        "agenda": agenda,
    }
    return fields, warnings


def parse_schedule_message(payload: dict) -> tuple[dict, list[str]]:
    """RND-198: WeCom schedule message.

    Expected fields: title, starttime, endtime, place, description.
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    title = _clean_str(payload.get("title"))
    if not title:
        warnings.append("missing_title")

    starttime = _safe_int(payload.get("starttime"))
    endtime = _safe_int(payload.get("endtime"))
    place = _clean_str(payload.get("place"))
    description = _clean_str(payload.get("description"))

    fields = {
        "title": title,
        "starttime": starttime,
        "endtime": endtime,
        "place": place,
        "description": description,
    }
    return fields, warnings


def parse_redpacket_message(payload: dict) -> tuple[dict, list[str]]:
    """RND-198: WeCom red packet (hongbao) message.

    Expected fields: type (hbtype), wishing, totalnum.
    Monetary amount (totalamount) is INTENTIONALLY NOT EXTRACTED into
    fields for security reasons — preserved only in the raw sub-payload.
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    red_type = _clean_str(payload.get("type"))
    wishing = _clean_str(payload.get("wishing"))
    totalnum = _safe_int(payload.get("totalnum"))

    fields = {
        "type": red_type,
        "wishing": wishing,
        "totalnum": totalnum,
    }
    return fields, warnings


def parse_switch_corp_message(payload: dict) -> tuple[dict, list[str]]:
    """RND-198: WeCom switch_corp message.

    Expected fields: corpid, corp_name.
    corpid is INTENTIONALLY NOT EXTRACTED into fields for security reasons
    — preserved only in the raw sub-payload. Only corp_name is surfaced.
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    corp_name = _clean_str(payload.get("corp_name"))
    if not corp_name:
        warnings.append("missing_corp_name")

    fields = {
        "corp_name": corp_name,
    }
    return fields, warnings


def parse_system_event_message(payload: dict) -> tuple[dict, list[str]]:
    """RND-198: WeCom sys (system event) message.

    The action subtype is extracted from the decrypted payload envelope's
    "action" field. The sys sub-payload itself typically contains
    structured fields about the event (e.g. member userids for room
    changes, corp info for switch_corp).

    Expected action subtypes (from WeCom ChatData action field):
      - "switch_corp": user switched to a different corp
      - "create_room" / "update_room": group member join/leave/removal
      - "conv_archive_auth": conversation archive authorization event
      - unknown: any other action value is treated as unknown subtype

    Participant userids in sys events are NOT extracted into fields —
    only display-text context is surfaced.
    """
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}

    subtype = _clean_str(payload.get("subtype"))
    if not subtype:
        warnings.append("missing_subtype")

    if subtype in ("create_room", "update_room"):
        # Privacy: member_userid is a raw identifier and must not be
        # extracted into fields (exposed via API).
        member_count = _safe_int(payload.get("member_count"))
        display_text = payload.get("display_text")

        fields = {
            "subtype": subtype,
            "display_text": display_text if isinstance(display_text, str) else None,
            "member_count": member_count,
        }
    elif subtype == "switch_corp":
        corp_name = _clean_str(payload.get("corp_name"))
        display_text = payload.get("display_text")
        fields = {
            "subtype": subtype,
            "display_text": display_text if isinstance(display_text, str) else None,
            "corp_name": corp_name,
        }
    elif subtype == "conv_archive_auth":
        display_text = payload.get("display_text")
        fields = {
            "subtype": subtype,
            "display_text": display_text if isinstance(display_text, str) else None,
        }
    else:
        display_text = payload.get("display_text")
        fields = {
            "subtype": subtype,
            "display_text": display_text if isinstance(display_text, str) else None,
        }

    return fields, warnings


_STRUCTURED_FIELD_PARSERS = {
    "link": parse_link_message,
    "location": parse_location_message,
    "markdown": parse_markdown_message,
    "news": parse_news_message,
    "weapp": parse_miniprogram_message,
    # RND-198 interactive business types
    "vote": parse_vote_message,
    "todo": parse_todo_message,
    "collect": parse_collect_message,
    "meeting": parse_meeting_message,
    "schedule": parse_schedule_message,
    "redpacket": parse_redpacket_message,
    "switch_corp": parse_switch_corp_message,
}


# ---------------------------------------------------------------------------
# mixed / chatrecord — recursive nested-message extraction (RND-200).
# ---------------------------------------------------------------------------


def _extract_nested_text(raw_content: Any) -> Optional[str]:
    """Extract display text from a "text"-type nested item's content field.

    Two plausible WeCom encodings exist and neither is confirmed by any
    fixture in this repo (see module docstring): a bare string
    ("hello world"), or the same JSON-wrapped-string-with-a-"content"-key
    shape every other nested type uses ('{"content":"hello world"}',
    mirroring the top-level decrypted["text"]["content"] shape). Both are
    tried defensively rather than assuming one; a string that happens to
    start with "{" but is not valid JSON (or decodes to something other
    than a dict with a "content" key) falls back to being treated as the
    literal text itself, never dropped.
    """
    if isinstance(raw_content, dict):
        return _clean_str(raw_content.get("content"))
    if not isinstance(raw_content, str):
        return _clean_str(raw_content) if raw_content is not None else None
    stripped = raw_content.strip()
    if stripped.startswith("{"):
        try:
            candidate = json.loads(stripped)
        except (ValueError, TypeError, RecursionError):
            # RecursionError (e.g. json.loads("[" * 2000)) is a
            # RuntimeError subclass, NOT a ValueError/TypeError -- a
            # pathologically deep-nested content string must degrade the
            # same as any other malformed JSON, never propagate past this
            # function (see _decode_nested_content's identical guard).
            candidate = None
        if isinstance(candidate, dict) and "content" in candidate:
            return _clean_str(candidate.get("content"))
    return _clean_str(raw_content)


def _decode_nested_content(raw_content: Any) -> tuple[Any, bool]:
    """Best-effort decode of one nested item's "content" field.

    Every non-text WeCom nested-item shape carries content as a
    JSON-encoded *string* that needs a second json.loads to reach the same
    dict shape that type's top-level parser (e.g. parse_link_message)
    already expects -- unconfirmed against any fixture in this repo (see
    module docstring), so any decode failure degrades to returning the
    original raw value with malformed=True rather than raising. A content
    value that already arrived as a dict (a plausible alternate encoding)
    is accepted as-is without requiring the JSON-string step. text items
    never reach this function -- see _extract_nested_text.
    """
    if isinstance(raw_content, dict):
        return raw_content, False
    if isinstance(raw_content, str):
        text = raw_content.strip()
        if not text:
            return {}, False
        try:
            decoded = json.loads(text)
        except (ValueError, TypeError, RecursionError):
            # RecursionError is deliberately caught alongside
            # ValueError/TypeError: json.loads on a string with thousands
            # of nested brackets/braces raises RecursionError, not
            # ValueError, and (being a RuntimeError subclass) would
            # otherwise only be caught by parse_structured_content's
            # outer `except Exception`, which discards the ENTIRE
            # message's parse result (all sibling items, all media_refs)
            # instead of degrading just this one item -- exactly the
            # "malformed payload must not break parsing" failure mode
            # this module's per-item degradation exists to prevent.
            return raw_content, True
        if isinstance(decoded, dict):
            return decoded, False
        return raw_content, True
    return raw_content, raw_content is not None


def _parse_nested_item(
    raw_item: Any, path: str, depth: int, budget: dict
) -> Optional[dict]:
    """Recursively normalize one mixed/chatrecord child item.

    Returns a dict shaped {"path", "type", "supported", "text", "fields",
    "media", "sender", "sender_name", "timestamp", "children"} (plus a
    "malformed": True marker when content decoding failed) -- the shared
    node shape structured_content.fields["items"] is built from, preserving
    ordering (path is a dot-separated index chain, e.g. "0.2", matching
    source list order) and hierarchy (children is the same shape,
    recursively) per the ticket's requirements.

    Returns None once the safety budget (total node count across the
    whole tree, or recursion depth) is exhausted -- the item is dropped,
    never partially parsed; callers must check budget["truncated"] /
    budget["depth_exceeded"] and surface a parse_warnings entry. This is
    the only case a node is silently dropped -- every other malformed
    shape (not a dict, unrecognized type, undecodable content) still
    produces a visible node instead (ticket requirement: unknown/malformed
    embedded types must remain visible, never discarded).

    A malformed/cyclic *value* cannot literally cycle (json.loads never
    produces a reference cycle), so the depth+count budget alone is what
    protects against a pathological payload (extreme nesting depth or an
    enormous flat item list) -- see _MIXED_ITEM_CAP/_MIXED_MAX_DEPTH.
    """
    budget["count"] += 1
    if budget["count"] > _MIXED_ITEM_CAP:
        budget["truncated"] = True
        return None
    if depth > _MIXED_MAX_DEPTH:
        budget["depth_exceeded"] = True
        return None

    node: dict = {
        "path": path,
        "type": None,
        "supported": False,
        "text": None,
        "fields": None,
        "media": None,
        "sender": None,
        "sender_name": None,
        "timestamp": None,
        "children": None,
    }

    if not isinstance(raw_item, dict):
        return node

    item_type = _clean_str(raw_item.get("type"))
    node["type"] = item_type
    node["supported"] = item_type in _NESTED_KNOWN_TYPES

    # Sender/timestamp field names are chatrecord-specific and unconfirmed
    # (see module docstring) -- checked defensively across plausible
    # candidates, same pattern as parse_news_message's image field lookup.
    for key in ("from", "fromusername", "sender", "fromuser"):
        sender = _clean_str(raw_item.get(key))
        if sender:
            node["sender"] = sender
            break
    for key in ("fromname", "sendername", "display_name", "nickname"):
        sender_name = _clean_str(raw_item.get(key))
        if sender_name:
            node["sender_name"] = sender_name
            break
    for key in ("msgtime", "time", "timestamp"):
        timestamp = _safe_int(raw_item.get(key))
        if timestamp is not None:
            node["timestamp"] = timestamp
            break

    if item_type in _NESTED_TEXT_TYPES:
        node["text"] = _extract_nested_text(raw_item.get("content"))
        return node

    if item_type not in _NESTED_KNOWN_TYPES:
        # Unknown child type -- there is no expected payload shape to
        # decode against, so a plain string is never "malformed" here
        # (unlike the known-type branches below); it is simply surfaced
        # as best-effort literal text via the same dict/str/JSON-wrapped
        # extraction _extract_nested_text already implements for "text"
        # items, kept visible rather than discarded (ticket requirement)
        # without a false malformed marker.
        node["text"] = _extract_nested_text(raw_item.get("content"))
        return node

    decoded_content, malformed = _decode_nested_content(raw_item.get("content"))
    if malformed:
        node["malformed"] = True
        return node

    if item_type in _NESTED_MEDIA_TYPES:
        payload = decoded_content if isinstance(decoded_content, dict) else {}
        sdkfileid = _clean_str(payload.get("sdkfileid"))
        node["media"] = {"has_reference": sdkfileid is not None}
        if sdkfileid:
            # budget["media_refs"] is always present -- every caller
            # constructs budget via _parse_nested_item_list's top-level
            # entry point, which initializes it unconditionally.
            budget["media_refs"].append({"path": path, "type": item_type, "sdkfileid": sdkfileid})

    elif item_type in _NESTED_STRUCTURED_JSON_TYPES:
        payload = decoded_content if isinstance(decoded_content, dict) else {}
        field_parser = _STRUCTURED_FIELD_PARSERS.get(item_type)
        if field_parser is not None:
            try:
                fields, _warnings = field_parser(payload)
                node["fields"] = fields
            except Exception:
                node["malformed"] = True
        # else: a nested RAW_PASSTHROUGH-strategy type (card/docmsg/
        # audio_doc) -- no field extraction, fields stays None, matching
        # the top-level dispatcher's treatment of the same types.

    elif item_type in _NESTED_COMPOSITE_TYPES:
        payload = decoded_content if isinstance(decoded_content, dict) else {}
        if item_type == "chatrecord":
            node["fields"] = {"title": _clean_str(payload.get("title"))}
        # Reuses _parse_nested_item_list (the same walk the top-level
        # entry points use) instead of a hand-copied loop, so a nested
        # composite's own item list gets identical malformed/empty/
        # truncation/unsupported-type diagnostics as the outermost list --
        # an earlier revision's inline duplicate silently dropped these
        # warnings whenever the problem occurred inside a nested
        # composite's own "item" list (RND-200 QA fix).
        children, child_warnings = _parse_nested_item_list(payload.get("item"), path, depth + 1, budget)
        node["children"] = children
        if child_warnings:
            budget["nested_warnings"].extend(f"{path}:{w}" for w in child_warnings)

    return node


def _parse_nested_item_list(
    raw_items: Any, path_prefix: str, depth: int, budget: dict
) -> tuple[list[dict], list[str]]:
    """Shared item-list walk, used both for the top-level entry point
    (parse_mixed_message/parse_chatrecord_message, path_prefix="", depth=0)
    and recursively for a nested mixed/chatrecord child's own "item" list
    (from _parse_nested_item's composite branch, path_prefix=that child's
    own path, depth=depth+1) -- the same function at every nesting level,
    so warnings are computed identically regardless of depth.

    path_prefix is the parent's own dotted path ("" at the top level); each
    item's path is `path_prefix + "." + idx`, or bare str(idx) when
    path_prefix is empty. budget is the single shared accumulator for the
    whole tree — count/truncated/depth_exceeded/media_refs/
    nested_warnings — created once by the top-level caller and threaded
    through every recursive call, never re-created partway down.

    Returns (items, warnings) -- items preserves source order (ticket
    requirement); warnings are local to *this* list only (top-level
    callers use them directly; _parse_nested_item's composite branch
    folds them into budget["nested_warnings"], tagged with its own path,
    since a single node dict has no separate slot to return warnings
    through).
    """
    warnings: list[str] = []
    if not isinstance(raw_items, list):
        if raw_items is not None:
            warnings.append("malformed_item_list")
        raw_items = []
    if not raw_items:
        warnings.append("empty_item_list")

    items = []
    for idx, raw_item in enumerate(raw_items):
        if budget["truncated"] or budget["depth_exceeded"]:
            # Safety budget already exhausted elsewhere in the tree --
            # stop iterating this list instead of calling
            # _parse_nested_item (which would immediately return None
            # anyway) for every remaining entry of a possibly enormous
            # list. Bounds CPU work, not just output size, against a wide
            # (not just deep) adversarial payload.
            break
        path = str(idx) if not path_prefix else f"{path_prefix}.{idx}"
        node = _parse_nested_item(raw_item, path, depth, budget)
        if node is not None:
            items.append(node)

    if any(item.get("malformed") for item in items):
        warnings.append("some_items_malformed")
    if any(not item.get("supported", True) for item in items):
        warnings.append("some_items_unsupported_type")

    return items, warnings


def _new_nested_parse_budget() -> dict:
    return {
        "count": 0,
        "truncated": False,
        "depth_exceeded": False,
        "media_refs": [],
        "nested_warnings": [],
    }


def _finalize_nested_warnings(warnings: list[str], budget: dict) -> list[str]:
    """Merge the top-level list's own warnings with the safety-budget
    flags and every nested-level warning collected anywhere in the tree
    (see _parse_nested_item_list/_parse_nested_item's composite branch)."""
    if budget["truncated"]:
        warnings.append(f"item_tree_truncated_at_{_MIXED_ITEM_CAP}")
    if budget["depth_exceeded"]:
        warnings.append(f"nesting_depth_truncated_at_{_MIXED_MAX_DEPTH}")
    warnings.extend(budget["nested_warnings"])
    return warnings


def parse_mixed_message(payload: dict) -> tuple[dict, list[str], list[dict]]:
    """WeCom mixed (图文混排/composite) message: {"item": [{"type","content"}, ...]}.

    Each item is independently degraded (see _parse_nested_item) so one
    malformed or unsupported child never drops the rest. Returns (fields,
    warnings, media_refs) -- a three-tuple, unlike the two-tuple contract
    every STRUCTURED_FIELDS parser above uses -- see module docstring for
    why (media_refs is server-internal, feeds app.media_download only).
    """
    payload = payload if isinstance(payload, dict) else {}
    budget = _new_nested_parse_budget()
    items, warnings = _parse_nested_item_list(payload.get("item"), "", 0, budget)
    warnings = _finalize_nested_warnings(warnings, budget)
    fields = {"items": items, "item_count": len(items)}
    return fields, warnings, budget["media_refs"]


def parse_chatrecord_message(payload: dict) -> tuple[dict, list[str], list[dict]]:
    """WeCom chatrecord (聊天记录/forwarded chat history) message:
    {"title": "...", "item": [{"type","content",...}, ...]}.

    Same recursive item handling as parse_mixed_message; additionally
    surfaces the digest title when present.
    """
    payload = payload if isinstance(payload, dict) else {}
    title = _clean_str(payload.get("title"))
    warnings_title = [] if title else ["missing_title"]
    budget = _new_nested_parse_budget()
    items, warnings = _parse_nested_item_list(payload.get("item"), "", 0, budget)
    warnings = _finalize_nested_warnings(warnings_title + warnings, budget)
    fields = {"title": title, "items": items, "item_count": len(items)}
    return fields, warnings, budget["media_refs"]


_NESTED_MESSAGE_PARSERS = {
    "mixed": parse_mixed_message,
    "chatrecord": parse_chatrecord_message,
}


def parse_structured_content(msgtype: Optional[str], decrypted: dict) -> Optional[dict]:
    """Dispatch a decrypted WeCom payload to the right structured parser.

    Flow: raw msgtype -> resolve_message_type() -> parser_strategy ->
    message-specific extraction (or raw-only preservation), matching the
    Message Type Registry's parser_strategy exactly -- this is the only
    msgtype -> parser dispatch in the codebase; no second competing
    mapping.

    Returns None for message types this ticket does not add structured
    persistence for (text uses content_text; image/video/voice/file/
    audio_archive use sdkfileid; control/composite/unknown types are out
    of RND-197 scope) -- callers should not write a structured_content
    value in that case.

    For in-scope types, always returns a dict shaped
    {"fields": {...} | None, "raw": <type-specific sub-payload>,
    "parse_warnings": [...]}, even when the sub-payload is missing or the
    parser itself raises -- never propagates an exception, so a single
    malformed historical message can never fail an entire decrypt run or
    timeline page.

    For mixed/chatrecord (NESTED_MESSAGES, RND-200) the returned dict
    carries one additional key, "media_refs" -- a flat, server-internal-
    only list of {"path","type","sdkfileid"} for every media-bearing
    nested item, consumed by app.media_download to register MediaFile rows
    via the existing RND-199 pipeline. Every other in-scope type omits
    this key entirely; callers must never copy it into an API response
    (same privacy boundary as "raw" -- see TimelineMessageOut's
    structured_content serialization, which only ever reads "fields" and
    "parse_warnings").
    """
    definition = resolve_message_type(msgtype)
    sub_payload = decrypted.get(msgtype, {}) if isinstance(decrypted, dict) else {}
    sub_payload = sub_payload if isinstance(sub_payload, dict) else {}

    if definition.parser_strategy == ParserStrategy.STRUCTURED_FIELDS:
        parser = _STRUCTURED_FIELD_PARSERS.get(msgtype)
        if parser is None:
            return {"fields": None, "raw": sub_payload, "parse_warnings": ["parser_not_implemented"]}
        try:
            fields, warnings = parser(sub_payload)
        except Exception:
            return {"fields": None, "raw": sub_payload, "parse_warnings": ["parse_failed"]}
        return {"fields": fields, "raw": sub_payload, "parse_warnings": warnings}

    if definition.parser_strategy == ParserStrategy.RAW_PASSTHROUGH and msgtype in (
        "card",
        "docmsg",
        "audio_doc",
    ):
        return {"fields": None, "raw": sub_payload, "parse_warnings": ["unconfirmed_schema"]}

    if definition.parser_strategy == ParserStrategy.CONTROL_SIGNAL and msgtype == "sys":
        # Extract the action field from the decrypted payload envelope.
        # The sys sub-payload typically contains structured event data
        # under decrypted["sys"].
        action = _clean_str(decrypted.get("action") if isinstance(decrypted, dict) else None)
        display_text = _clean_str(sub_payload.get("display_text"))

        # Build a synthetic payload for the system event parser
        sys_payload: dict = {"subtype": action, "display_text": display_text}

        # Extract known sub-payload fields based on common WeCom sys patterns
        member_userid = _clean_str(sub_payload.get("member_userid"))
        if member_userid:
            sys_payload["member_userid"] = member_userid

        member_count = _safe_int(sub_payload.get("member_count"))
        if member_count is not None:
            sys_payload["member_count"] = member_count

        corp_name = _clean_str(sub_payload.get("corp_name"))
        if corp_name:
            sys_payload["corp_name"] = corp_name

        if not action:
            return {"fields": None, "raw": sub_payload, "parse_warnings": ["missing_action"]}

        try:
            fields, warnings = parse_system_event_message(sys_payload)
        except Exception:
            return {"fields": None, "raw": sub_payload, "parse_warnings": ["parse_failed"]}
        return {"fields": fields, "raw": sub_payload, "parse_warnings": warnings}

    if definition.parser_strategy == ParserStrategy.NESTED_MESSAGES and msgtype in _NESTED_MESSAGE_PARSERS:
        nested_parser = _NESTED_MESSAGE_PARSERS[msgtype]
        try:
            fields, warnings, media_refs = nested_parser(sub_payload)
        except Exception:
            return {
                "fields": None,
                "raw": sub_payload,
                "parse_warnings": ["parse_failed"],
                "media_refs": [],
            }
        return {
            "fields": fields,
            "raw": sub_payload,
            "parse_warnings": warnings,
            "media_refs": media_refs,
        }

    return None