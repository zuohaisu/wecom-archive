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

Every parser in this module is a pure function: it takes the
msgtype-specific sub-payload dict (i.e. decrypted.get(msgtype, {})) and
never raises -- malformed/partial/historical-dirty input degrades to
missing fields plus a parse_warnings entry, never an exception. This
mirrors message_type_registry.py's "never raises" contract so a single
bad row can never fail an entire timeline page.
"""

from __future__ import annotations

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

    return None