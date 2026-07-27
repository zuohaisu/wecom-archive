"""Unified media access service (RND-221) — moved verbatim out of
app.routers.conversations.

Consolidates what were previously two parallel implementations (top-level
message media vs. nested mixed/chatrecord media) of the same four
concerns into one:

  - authorization (resolve_authorized_media, merging the former
    _resolve_authorized_media / _resolve_authorized_nested_media)
  - storage backend/ref resolution (_resolve_servable_backend_and_ref)
  - byte response serving (serve_media_bytes)
  - access descriptor construction (build_access_descriptor)

No behavior changes from the pre-RND-221 per-route implementations: this
is a mechanical extraction, not a rewrite. The routes in
app.routers.media call these in the same order the original route bodies
did — authorization completes in full, for both the top-level and nested
case, *before* any storage provider is instantiated, any URL is
constructed, or any signing operation is attempted (RND-174/RND-187).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.conversation_membership import _fetch_conversation_messages, _is_valid_roomid
from app.db.models import ArchiveMessage, MediaFile
from app.media_download import NESTED_MEDIA_MSGTYPES, iter_nested_media_refs
from app.media_storage import (
    SERVABLE_MEDIA_MSGTYPES,
    MediaObjectNotFound,
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageUnavailable,
    compute_signed_url_deadline,
    detect_media_content_type_for_ref,
    get_media_storage_provider,
    object_key_tenant_prefix_matches,
    resolve_downloadable_media_file_state,
    resolve_effective_storage_reference,
    resolve_servable_downloadable_media_path,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Nested media path grammar (RND-200 QA fix / RND-201)
#
# Path grammar: a dot-separated chain of small non-negative integers,
# e.g. "0", "0.1", "3.0.2" — exactly the same value
# app.structured_message_parser._parse_nested_item already assigns each
# node as its "path" field (and the same value app.media_download's
# media_refs entries key on), reused verbatim as the URL identifier
# rather than inventing a second addressing scheme (e.g. "items.2.
# children.0") for the same, already-homogeneous tree: every level of a
# mixed/chatrecord tree is a list of the same node shape, so the
# "items"/"children" key-name segments the ticket's example grammar uses
# carry no additional addressing information over a bare index chain.
#
# No eval, no arbitrary attribute traversal, no filesystem mapping: this
# regex is the ENTIRE grammar, and resolution (_find_nested_media_ref)
# is a single exact-string-equality lookup against the flat, already-
# computed media_refs list — the path string is never used to index into
# a live Python structure, walk an object graph, or build a filesystem
# path.
#
# Segment count is capped at _MIXED_MAX_DEPTH + 1 (one segment per
# recursion level 0..depth, inclusive) and each segment at 6 digits
# (comfortably above _MIXED_ITEM_CAP=200, the largest index that could
# ever legitimately appear, while still bounding absurd input) — both
# constants are mirrored locally (not imported) to avoid a new
# conversations.py -> structured_message_parser dependency; a dedicated
# test (test_nested_media_path_segment_cap_matches_parser_max_depth)
# keeps the two in sync instead.
_NESTED_MEDIA_MAX_PATH_SEGMENTS = 9  # mirrors structured_message_parser._MIXED_MAX_DEPTH + 1
_NESTED_MEDIA_MAX_PATH_LENGTH = 64
_NESTED_MEDIA_PATH_RE = re.compile(
    r"^\d{1,6}(?:\.\d{1,6}){0,8}$"  # {0,8} = up to 8 additional segments -> 9 total
)


def _validate_nested_media_path(raw: str) -> Optional[str]:
    """Validate a caller-supplied nested-media item path (untrusted URL
    input) against the strict grammar above, returning the path unchanged
    (it is already canonical — no normalization needed) or None for
    anything malformed: wrong characters (rejects path traversal — "..",
    "/", null bytes, letters — since only digits and "." ever match),
    too many segments (recursion-depth violation), or too long overall
    (defense in depth on top of the regex's own implicit bound).

    This function alone decides "well-formed enough to attempt a lookup"
    — it does NOT confirm the path resolves to an actual node (that is
    _find_nested_media_ref's job, against THIS message's own data only).
    """
    if not isinstance(raw, str) or not raw:
        return None
    if len(raw) > _NESTED_MEDIA_MAX_PATH_LENGTH:
        return None
    if raw.count(".") > _NESTED_MEDIA_MAX_PATH_SEGMENTS - 1:
        return None
    if not _NESTED_MEDIA_PATH_RE.match(raw):
        return None
    return raw


def _find_nested_media_ref(structured_content, path: str) -> Optional[dict]:
    """Resolve a validated path to its media_refs entry for THIS message
    only — {"path", "type", "sdkfileid"} — or None if the path does not
    exist, is not media-bearing, or belongs to a different message
    entirely. iter_nested_media_refs (app.media_download) already
    filters to only well-formed entries whose type is a downloadable
    category, so a single exact-match lookup here simultaneously
    satisfies every one of the ticket's "reject: node not found /
    non-media node / path beyond this message's content" requirements —
    there is no separate tree-walk needed. Resolution is also
    structurally unique by construction: _parse_nested_item visits each
    (recursion level, sibling index) pair at most once, so no two
    media_refs entries for one message can ever share a path (verified by
    test, not merely assumed).

    structured_content.media_refs is server-internal only (see
    app.structured_message_parser's module docstring) — this function's
    return value must never be serialized into an API response as-is;
    only sdkfileid-derived, already-authorized lookups (MediaFile status/
    mime_type/size) may reach the client.
    """
    refs = iter_nested_media_refs(structured_content)
    for ref in refs:
        if ref["path"] == path:
            return ref
    return None


# ---------------------------------------------------------------------------
# Authorization (unified: top-level + nested)
# ---------------------------------------------------------------------------


@dataclass
class MediaAuthResult:
    """Result of resolve_authorized_media. ref is only populated for the
    nested case (item_path is not None) — it is the resolved
    {"path", "type", "sdkfileid"} media_refs entry from this message's
    own structured_content, server-internal only (never serialized as-is
    — see _find_nested_media_ref)."""

    msg: ArchiveMessage
    media_file: MediaFile
    ref: Optional[dict] = None


def resolve_authorized_media(
    db: Session,
    conversation_id: str,
    msgid: str,
    tenant_id: str,
    *,
    item_path: Optional[str] = None,
    mode: Optional[str] = None,
    entity_id: Optional[str] = None,
    conversation_type: Optional[str] = None,
) -> MediaAuthResult:
    """
    Shared authorization + lookup for all four media routes — the single
    merge of what were previously _resolve_authorized_media (top-level)
    and _resolve_authorized_nested_media (nested), which shared this
    exact authorization shape: conversation membership -> conversation_type
    consistency -> msgtype gate -> row lookup, all completed before any
    storage provider is touched.

    item_path is None -> top-level path: the MediaFile row is resolved by
    (tenant_id, archive_message_id).

    item_path is not None -> nested path: item_path is first validated
    (_validate_nested_media_path) and resolved against this message's own
    structured_content (_find_nested_media_ref) before the MediaFile row
    is resolved by (tenant_id, sdkfileid) — not archive_message_id. This
    is not a shortcut — it is the correct model for a nested reference:
    the same physical WeCom media object can be legitimately referenced
    by more than one logical message/node (e.g. the identical file
    re-forwarded into a second chatrecord digest), and this route answers
    "can this tenant read the physical object this logical node refers
    to", not "does this exact row happen to be owned by this exact
    message" (that ownership question only matters to the DOWNLOAD/write
    path — see app.media_download.get_or_reset_media_file — and is
    deliberately left unchanged here; see the RND-200 QA fix report's
    association-model section). Tenant isolation is unaffected: sdkfileid
    is never taken from the request — it is read server-side from THIS
    message's own already-tenant-scoped structured_content only.

    Authenticated (get_current_user, by every caller), tenant-scoped
    (tenant_id comes only from the session, never a request param), and
    conversation-scoped (the message must actually belong to
    conversation_id per _fetch_conversation_messages — the same
    membership rules the timeline route uses). The media_files lookup is
    additionally filtered by tenant_id directly (RND-156) — a second,
    independent check on top of message ownership, so a media row can
    never be served on the strength of archive_message_id/sdkfileid alone
    even if message/media tenant assignment were ever to diverge.

    mode/entity_id: optional entity context, forwarded verbatim into
    _fetch_conversation_messages — the exact same parameters and
    semantics the timeline route (get_conversation_messages) already
    uses. tenant_id still comes only from the session, never from these
    or any other request param, so passing entity context here never
    weakens tenant isolation. Passing this context through means a media
    request for a message that only exists on one side of a genuine
    direct/group collision resolves (and authorizes) against the same
    entity-scoped message set the timeline used to generate this URL —
    an entity that does not participate in that side gets a 404 here
    (the message simply is not in _fetch_conversation_messages's filtered
    result), same as it would for any other message it has no part in. A
    legacy ID-only request (mode/entity_id both None) on a genuinely
    ambiguous collision raises the same 400 _fetch_conversation_messages
    already raises for the ID-only timeline case — this function does
    not catch or convert it.

    conversation_type: optional consistency check, mirroring the exact
    policy get_conversation_messages already enforces. The caller has
    already validated this is exactly "direct"/"group" or None before
    reaching here (garbage input is a 400 independent of whether any
    message is found). Once a message is resolved below, this checks it
    against that ONE message's own actual type -- "group" if it carries
    a real roomid (_is_valid_roomid, the same truthiness rule
    _derive_conversation_membership and the timeline route use), "direct"
    otherwise.

    This authorization sequence completes in full *before* any storage
    provider, URL, or signing operation is touched (RND-174/RND-187) —
    all four media routes share this exact function so a security fix
    here automatically applies to all of them.
    """
    messages = _fetch_conversation_messages(
        db, conversation_id, tenant_id, mode=mode, entity_id=entity_id
    )
    msg = next((m for m in messages if m.msgid == msgid), None)
    if msg is None:
        raise HTTPException(status_code=404, detail="Not found")

    if conversation_type is not None:
        resolved_conversation_type = "group" if _is_valid_roomid(msg.roomid) else "direct"
        if conversation_type != resolved_conversation_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"conversation_type mismatch: requested '{conversation_type}' but "
                    f"this conversation resolved to '{resolved_conversation_type}'"
                ),
            )

    if item_path is None:
        if msg.msgtype not in SERVABLE_MEDIA_MSGTYPES:
            raise HTTPException(status_code=404, detail="Not found")

        media_file = (
            db.query(MediaFile)
            .filter(
                MediaFile.tenant_id == tenant_id,
                MediaFile.archive_message_id == msg.id,
            )
            .first()
        )
        if media_file is None or media_file.download_status != "downloaded":
            raise HTTPException(status_code=404, detail="Not found")

        return MediaAuthResult(msg=msg, media_file=media_file, ref=None)

    if msg.msgtype not in NESTED_MEDIA_MSGTYPES:
        raise HTTPException(status_code=404, detail="Not found")

    validated_path = _validate_nested_media_path(item_path)
    if validated_path is None:
        raise HTTPException(status_code=400, detail="Malformed nested media path")

    ref = _find_nested_media_ref(getattr(msg, "structured_content", None), validated_path)
    if ref is None:
        raise HTTPException(status_code=404, detail="Not found")

    media_file = (
        db.query(MediaFile)
        .filter(MediaFile.tenant_id == tenant_id, MediaFile.sdkfileid == ref["sdkfileid"])
        .first()
    )
    if media_file is None or media_file.download_status != "downloaded":
        raise HTTPException(status_code=404, detail="Not found")

    return MediaAuthResult(msg=msg, media_file=media_file, ref=ref)


# RND-207: private browser-cache lifetime for the authenticated byte-proxy
# routes (local media, and Qiniu bytes proxied server-side). These bytes are
# immutable per object key, and each request is independently session-
# authorized, so a per-user ("private") cache is safe and spares a re-download
# on reload. The primary Qiniu path is the client-direct signed URL (cached by
# the browser via its stable windowed URL), not this proxy.
_MEDIA_PROXY_CACHE_MAX_AGE = 3600


def _resolve_variant_serve_ref(
    media_file: MediaFile, variant: Optional[str], original_ref: str
) -> Tuple[str, bool, bool]:
    """Choose a requested thumbnail or browser-playable voice derivative.

    Returns ``(serve_ref, is_thumbnail, is_playback)``. A generated voice
    derivative is selected automatically for an ordinary descriptor request,
    or explicitly through ``variant=play``. It is only reached after the
    original media row has passed the normal tenant authorization flow.
    """
    if (
        variant == "thumb"
        and getattr(media_file, "thumbnail_status", None) == "generated"
        and getattr(media_file, "thumbnail_ref", None)
    ):
        return media_file.thumbnail_ref, True, False
    if (
        (variant == "play" or variant is None)
        and getattr(media_file, "file_type", None) in {"voice", "audio_archive"}
        and getattr(media_file, "playback_status", None) == "generated"
        and getattr(media_file, "playback_ref", None)
    ):
        return media_file.playback_ref, False, True
    return original_ref, False, False


def _resolve_servable_backend_and_ref(media_file: MediaFile, route_label: str) -> Tuple[str, str]:
    """
    Resolve (effective_backend, effective_ref) for an already-authorized
    media_file row (see resolve_authorized_media) and confirm it is
    actually servable, raising the shared 404/500/503 HTTPException
    taxonomy every media route uses.

    route_label only selects the log-line prefix (e.g. "media route" vs
    "media access route") — never included in the HTTP response.

    getattr(..., None): a MediaFile ORM row always has these columns, but
    duck-typed test doubles may not.
    """
    effective_backend, effective_ref = resolve_effective_storage_reference(
        getattr(media_file, "storage_backend", None),
        getattr(media_file, "storage_ref", None),
        getattr(media_file, "local_path", None),
    )
    if effective_backend is None:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        file_state = resolve_downloadable_media_file_state(effective_ref, effective_backend)
    except MediaStorageConfigurationError:
        logger.error(
            "%s: storage configuration error (backend=%s)", route_label, effective_backend
        )
        raise HTTPException(status_code=500, detail="Media storage is misconfigured")
    except MediaStorageUnavailable:
        logger.warning(
            "%s: storage provider unavailable (backend=%s)", route_label, effective_backend
        )
        raise HTTPException(status_code=503, detail="Media storage temporarily unavailable")

    if file_state != "servable":
        raise HTTPException(status_code=404, detail="Not found")

    return effective_backend, effective_ref


# ---------------------------------------------------------------------------
# Byte response (unified: top-level + nested)
# ---------------------------------------------------------------------------


def serve_media_bytes(effective_backend: str, serve_ref: str, *, route_label: str) -> Response:
    """
    Serve an already-authorized, already-resolved media object's bytes —
    the single merge of what were previously two byte-for-byte identical
    blocks in get_message_media and get_nested_message_media.

    provider resolution (RND-174 QA fix): the provider used to serve this
    row is resolved from the row's own storage_backend/storage_ref
    (via the caller's _resolve_servable_backend_and_ref call), never from
    the deployment-wide MEDIA_STORAGE_PROVIDER default — so this route
    keeps working correctly for both local- and Qiniu-backed rows in the
    same deployment, regardless of which provider is currently configured
    for new writes.

    Response codes:
      404  a confirmed-missing remote object, or no safe local path could
           be resolved for a local-backed row.
      503  the storage provider could not confirm the object's state
           (network timeout, auth failure, bucket error, SDK failure) —
           never reported as a plain 404 (RND-174 QA fix: an outage must
           not look like missing media).
      502  a storage operation otherwise failed against a provider that
           did respond.

    Never includes sdkfileid, local_path, storage_ref, oss_key, or any raw
    provider/SDK error detail in the response or in any log line.
    """
    provider = get_media_storage_provider(effective_backend)

    if provider.supports_local_path():
        safe_path = resolve_servable_downloadable_media_path(serve_ref, effective_backend)
        if safe_path is None:
            raise HTTPException(status_code=404, detail="Not found")
        content_type = detect_media_content_type_for_ref(str(safe_path))
        proxied = FileResponse(path=str(safe_path), media_type=content_type)
        proxied.headers["Cache-Control"] = f"private, max-age={_MEDIA_PROXY_CACHE_MAX_AGE}"
        return proxied

    # Cloud-backed media (RND-174): still a valid controlled access path —
    # fetch the bytes through the provider and proxy them back. The
    # response shape (raw bytes, same URL, same content-type behavior) is
    # unchanged. No Qiniu URL or credential ever reaches the client
    # through this route. detect_media_content_type_for_ref covers every
    # RND-199 supported media category (image/video/voice/file), not
    # image only.
    content_type = detect_media_content_type_for_ref(serve_ref)
    try:
        data = provider.read_bytes(serve_ref)
    except MediaObjectNotFound:
        raise HTTPException(status_code=404, detail="Not found")
    except MediaStorageUnavailable:
        logger.warning(
            "%s: storage provider unavailable during read (backend=%s)",
            route_label,
            effective_backend,
        )
        raise HTTPException(status_code=503, detail="Media storage temporarily unavailable")
    except MediaStorageOperationError:
        logger.error(
            "%s: storage operation failed during read (backend=%s)", route_label, effective_backend
        )
        raise HTTPException(status_code=502, detail="Media storage operation failed")
    proxied = Response(content=data, media_type=content_type)
    proxied.headers["Cache-Control"] = f"private, max-age={_MEDIA_PROXY_CACHE_MAX_AGE}"
    return proxied


# ---------------------------------------------------------------------------
# Access descriptor (unified: top-level + nested)
# ---------------------------------------------------------------------------


def build_access_descriptor(
    *,
    effective_backend: str,
    serve_ref: str,
    tenant_id: str,
    media_file: MediaFile,
    is_thumbnail: bool,
    is_playback: bool = False,
    size_bytes: Optional[int],
    proxy_url: str,
    route_label: str,
) -> dict:
    """
    Build a unified media access descriptor (RND-187) — the single merge
    of what were previously two byte-for-byte identical local/Qiniu
    branches in get_message_media_access and get_nested_message_media_access.

    Returns a plain dict with keys storage_backend/access_type/url/
    expires_at/content_type/size_bytes; the caller wraps it in the
    response_model appropriate to the route (MediaAccessOut for
    top-level, adding media_id; NestedMediaAccessOut for nested, renaming
    content_type -> mime_type and never adding media_id — RND-200 QA
    security fix, see app.schemas.media.NestedMediaAccessOut's docstring).

    access_type="proxy" (local-backed rows): url is proxy_url, exactly as
    the caller built it (get_message_media / get_nested_message_media,
    with any entity-context query string and/or variant=thumb already
    applied) — expires_at is None, the URL carries no time-boxed
    credential of its own; the session cookie authorizes each request to
    it.

    access_type="signed_url" (Qiniu-backed rows): url is a short-lived
    Qiniu signed URL whose absolute expiry is snapped to a fixed window
    (compute_signed_url_deadline; MEDIA_SIGNED_URL_WINDOW_SECONDS,
    defaulting to MEDIA_SIGNED_URL_TTL_SECONDS = 900, bounded [60, 3600])
    so the URL is byte-identical for every request in that window and the
    browser reuses its HTTP cache (RND-207). Minted only after the
    caller has already completed resolve_authorized_media /
    _resolve_servable_backend_and_ref *plus* an explicit check here that
    the object key's own "tenants/{tenant_id}/" prefix agrees with this
    row's authenticated tenant_id (object_key_tenant_prefix_matches) — so
    a corrupted/mistagged row can never mint a signed URL for a different
    tenant's object on the strength of the tenant_id column alone. The
    signed URL itself is never logged or included in any exception.

    Response codes: same 404/500/503 taxonomy and meaning as
    serve_media_bytes (wrong tenant/conversation, non-image, missing row,
    unservable file, misconfigured backend, provider outage, object-key
    tenant-prefix mismatch). 502 additionally covers a confirmed
    signed-URL generation failure against a provider that did respond.
    """
    content_type = detect_media_content_type_for_ref(serve_ref)

    if effective_backend == "local":
        return {
            "storage_backend": "local",
            "access_type": "proxy",
            "url": proxy_url,
            "expires_at": None,
            "content_type": content_type,
            "size_bytes": size_bytes,
        }

    if effective_backend == "qiniu_kodo":
        if not object_key_tenant_prefix_matches(serve_ref, tenant_id):
            logger.error(
                "%s: object key tenant prefix mismatch (media_id=%s)",
                route_label,
                media_file.id,
            )
            raise HTTPException(status_code=404, detail="Not found")

        # RND-207: fixed-window deadline so the signed URL is byte-identical
        # for every request in the same window -> the browser reuses its HTTP
        # cache instead of re-downloading on each fresh signature.
        try:
            now_epoch = int(datetime.now(timezone.utc).timestamp())
            deadline = compute_signed_url_deadline(now_epoch)
        except MediaStorageConfigurationError:
            logger.error("%s: invalid signed url TTL/window configuration", route_label)
            raise HTTPException(status_code=500, detail="Media storage is misconfigured")

        provider = get_media_storage_provider(effective_backend)
        try:
            signed_url = provider.get_download_url(serve_ref, deadline=deadline)
        except MediaObjectNotFound:
            raise HTTPException(status_code=404, detail="Not found")
        except MediaStorageConfigurationError:
            logger.error(
                "%s: signed url configuration error (media_id=%s)", route_label, media_file.id
            )
            raise HTTPException(status_code=500, detail="Media storage is misconfigured")
        except MediaStorageOperationError:
            logger.error(
                "%s: signed url generation failed (media_id=%s)", route_label, media_file.id
            )
            raise HTTPException(status_code=502, detail="Media storage operation failed")

        if not signed_url:
            logger.error(
                "%s: signed url generation returned empty (media_id=%s)",
                route_label,
                media_file.id,
            )
            raise HTTPException(status_code=502, detail="Media storage operation failed")

        expires_at = datetime.fromtimestamp(deadline, timezone.utc).isoformat()
        logger.info(
            "%s: signed url issued (media_id=%s, tenant_id=%s, "
            "backend=qiniu_kodo, is_thumbnail=%s, is_playback=%s)",
            route_label,
            media_file.id,
            tenant_id,
            is_thumbnail,
            is_playback,
        )
        return {
            "storage_backend": "qiniu_kodo",
            "access_type": "signed_url",
            "url": signed_url,
            "expires_at": expires_at,
            "content_type": content_type,
            "size_bytes": size_bytes,
        }

    logger.error(
        "%s: unsupported backend for access descriptor (backend=%s)",
        route_label,
        effective_backend,
    )
    raise HTTPException(status_code=500, detail="Media storage is misconfigured")
