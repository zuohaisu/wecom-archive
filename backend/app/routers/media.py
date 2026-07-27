"""
Media access APIs for the 365 WeCom Archive review console (RND-221 — moved
out of app.routers.conversations; no behavior, path, or schema changes).

All routes are protected by get_current_user (RND-110).
All archive queries are scoped by session tenant_id.
tenant_id is NEVER accepted from user-supplied request params.

Authorization, storage-backend/provider resolution, byte-response
serving, and access-descriptor construction are unified across the
top-level and nested (mixed/chatrecord) media routes in
app.services.media_access — see that module for the shared
implementation. This module only validates/converts HTTP input and
dispatches to it.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.auth import get_current_user
from app.db.models import AdminUser
from app.db.session import get_db
from app.schemas.media import MediaAccessOut, NestedMediaAccessOut
from app.services.media_access import (
    _resolve_servable_backend_and_ref,
    _resolve_variant_serve_ref,
    build_access_descriptor,
    resolve_authorized_media,
    serve_media_bytes,
)
from app.services.timeline_service import (
    _entity_context_query_string,
    _resolve_entity_context,
    _with_variant_play,
    _with_variant_thumb,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Cache-Control: no-store for the media access descriptor endpoint
#
# get_message_media_access() sets response.headers["Cache-Control"] on its
# own success path, but that mutation only reaches the client when the route
# body actually returns a value. Every error path — an HTTPException raised
# inside the route body (404/500/502/503), *and* a 401 raised by
# get_current_user() while resolving dependencies, which runs before the
# route body and therefore never touches that Response object at all — goes
# through FastAPI/Starlette's own exception-to-Response conversion instead,
# which builds a brand-new Response from scratch and knows nothing about
# headers set earlier on the request-scoped Response object.
#
# A try/except around the route body would still miss the get_current_user
# 401 case (it fails before the body starts). This middleware instead
# inspects the fully-built outgoing Response for every request — after
# Starlette's ExceptionMiddleware has already converted any exception
# (dependency-resolution or route-body) into a concrete status code — and
# adds/overwrites the header there. It is scoped by an exact path-pattern
# match so no other endpoint's caching behavior changes.
# ---------------------------------------------------------------------------

_MEDIA_ACCESS_PATH_RE = re.compile(
    r"^/api/conversations/[^/]+/messages/[^/]+/(media|nested-media/[^/]+)/access$"
)


class MediaAccessNoStoreMiddleware(BaseHTTPMiddleware):
    """Ensures Cache-Control: no-store on every response — success or error,
    any status code — for GET .../media/access and (RND-200/201) GET
    .../nested-media/{item_path}/access, which shares the exact same
    no-time-boxed-credential-must-never-be-cached rationale (RND-187). See
    the module comment above for why this can't be done from inside the
    route alone."""

    async def dispatch(self, request: Request, call_next: Callable):
        response = await call_next(request)
        if _MEDIA_ACCESS_PATH_RE.match(request.url.path):
            response.headers["Cache-Control"] = "no-store"
        return response


@router.get("/api/conversations/{conversation_id}/messages/{msgid}/media")
def get_message_media(
    conversation_id: str,
    msgid: str,
    mode: Optional[str] = Query(
        None,
        description=(
            "Optional entity context: 'staff' or 'contact'. Same semantics "
            "as GET .../messages -- required to unambiguously authorize a "
            "message that only exists on one side of a direct/group "
            "collision. Without it, a genuinely ambiguous conversation_id "
            "returns 400, same as the timeline route."
        ),
    ),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    conversation_type: Optional[str] = Query(
        None,
        description=(
            "Optional consistency check, 'direct' or 'group' -- same "
            "validation as GET .../messages. Never used to select or "
            "filter the message itself."
        ),
    ),
    variant: Optional[str] = Query(
        None,
        description=(
            "RND-207/RND-258: 'thumb' serves a generated list thumbnail; "
            "'play' serves a generated browser-playable voice derivative. "
            "Omitted voice requests use a generated playback variant when "
            "available; otherwise the original is served."
        ),
    ),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Serve an already-downloaded image message's file content (RND-144).

    mode/staff_id/contact_id/conversation_type (RND-158 Phase 2 QA round
    7, blocker 3): optional entity context, identical params/validation to
    GET .../messages, forwarded into resolve_authorized_media so a media
    request generated from an entity-scoped timeline resolves against the
    same entity-scoped message set the timeline used -- see
    resolve_authorized_media's docstring for the collision/authorization
    contract this establishes.

    Kept unchanged by RND-187 for backward compatibility: local-backed rows
    still only serve this way, and this is still a valid (if no longer the
    primary) access path for Qiniu-backed rows too — see
    get_message_media_access for the RND-187 signed-URL descriptor route
    the frontend now calls first for image messages.

    See resolve_authorized_media's docstring for the full authorization-
    boundary rationale (RND-174/RND-156): the sequence completes in full
    *before* any storage provider is selected or called — the provider is
    never part of the authorization boundary.

    The provider used to serve this row is resolved from the row's own
    storage_backend/storage_ref (RND-174 QA fix), never from the
    deployment-wide MEDIA_STORAGE_PROVIDER default — so this route keeps
    working correctly for both local- and Qiniu-backed rows in the same
    deployment, regardless of which provider is currently configured for
    new writes.

    Response codes:
      404  wrong tenant, wrong conversation, non-image message,
           missing/pending/failed media_files row, unsafe/missing storage
           reference, disallowed file extension, or a confirmed-missing
           remote object.
      503  the storage provider could not confirm the object's state
           (network timeout, auth failure, bucket error, SDK failure) —
           never reported as a plain 404 (RND-174 QA fix: an outage must
           not look like missing media).
      502  a storage operation otherwise failed against a provider that
           did respond.
      500  the row names a storage backend that is unset/unsupported/
           misconfigured.

    Never includes sdkfileid, local_path, storage_ref, oss_key, or any raw
    provider/SDK error detail in the response or in any log line.
    """
    _, tenant_id = auth
    if conversation_type is not None and conversation_type not in ("direct", "group"):
        raise HTTPException(
            status_code=400, detail="conversation_type must be 'direct' or 'group'"
        )
    entity_id = _resolve_entity_context(mode, staff_id, contact_id)

    auth_result = resolve_authorized_media(
        db,
        conversation_id,
        msgid,
        tenant_id,
        mode=mode,
        entity_id=entity_id,
        conversation_type=conversation_type,
    )
    effective_backend, effective_ref = _resolve_servable_backend_and_ref(
        auth_result.media_file, "media route"
    )
    # RND-207: serve the generated thumbnail when variant=thumb is requested
    # and one exists; the thumbnail is co-located in the same backend and
    # under the same tenant prefix as the original (already authorized above).
    serve_ref, _is_thumbnail, _is_playback = _resolve_variant_serve_ref(
        auth_result.media_file, variant, effective_ref
    )
    return serve_media_bytes(effective_backend, serve_ref, route_label="media route")


@router.get(
    "/api/conversations/{conversation_id}/messages/{msgid}/media/access",
    response_model=MediaAccessOut,
)
def get_message_media_access(
    conversation_id: str,
    msgid: str,
    response: Response,
    mode: Optional[str] = Query(
        None,
        description=(
            "Optional entity context: 'staff' or 'contact'. Same semantics "
            "as GET .../messages -- required to unambiguously authorize a "
            "message that only exists on one side of a direct/group "
            "collision. Without it, a genuinely ambiguous conversation_id "
            "returns 400, same as the timeline route."
        ),
    ),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    conversation_type: Optional[str] = Query(
        None,
        description=(
            "Optional consistency check, 'direct' or 'group' -- same "
            "validation as GET .../messages. Never used to select or "
            "filter the message itself."
        ),
    ),
    variant: Optional[str] = Query(
        None,
        description=(
            "RND-207/RND-258: 'thumb' returns a list-thumbnail descriptor; "
            "'play' returns a generated browser-playable voice descriptor. "
            "Omitted voice requests use playback when available; otherwise "
            "the original is returned."
        ),
    ),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Return a unified media access descriptor (RND-187) instead of proxying
    image bytes. The frontend calls this first for every image message,
    then loads the actual image from descriptor.url — it never needs to
    know which storage_backend served it.

    mode/staff_id/contact_id/conversation_type (RND-158 Phase 2 QA round
    7, blocker 3): optional entity context, identical params/validation to
    GET .../messages, forwarded into resolve_authorized_media. Also
    echoed onto the returned proxy url (access_type="proxy" / local
    backend) so a client following it preserves the same context.

    access_type="proxy" (local-backed rows): url is the existing
    get_message_media route, unchanged. expires_at is None — the URL
    carries no time-boxed credential of its own; the session cookie
    authorizes each request to it, same as before RND-187.

    access_type="signed_url" (Qiniu-backed rows): url is a short-lived
    Qiniu signed URL whose absolute expiry is snapped to a fixed window
    (compute_signed_url_deadline; MEDIA_SIGNED_URL_WINDOW_SECONDS, defaulting
    to MEDIA_SIGNED_URL_TTL_SECONDS = 900, bounded [60, 3600]) so the URL is
    byte-identical for every request in that window and the browser reuses
    its HTTP cache (RND-207). Minted only after this route completes the
    exact same authorization sequence
    as get_message_media (resolve_authorized_media /
    _resolve_servable_backend_and_ref) *plus* an explicit check that the
    object key's own "tenants/{tenant_id}/" prefix agrees with this row's
    authenticated tenant_id (object_key_tenant_prefix_matches) — so a
    corrupted/mistagged row can never mint a signed URL for a different
    tenant's object on the strength of the tenant_id column alone. The
    signed URL itself is never logged or included in any exception.

    Cache-Control: no-store on every response this endpoint can produce —
    success or error, any status code, including a 401 raised by
    get_current_user() before this function body even runs. The header set
    below covers the success path (FastAPI copies headers mutated on an
    injected Response parameter onto the final response for a returned
    Pydantic model); MediaAccessNoStoreMiddleware (registered on the app in
    main.py) independently guarantees the same header on every error path,
    since an exception's Response is built fresh by FastAPI/Starlette and
    never sees this function's local `response` mutation. Both mechanisms
    target the same header value, so keeping this line is redundant but
    harmless on the success path, not dead code.

    Response codes: same 404/500/503 taxonomy and meaning as
    get_message_media (wrong tenant/conversation, non-image, missing row,
    unservable file, misconfigured backend, provider outage, object-key
    tenant-prefix mismatch). 502 additionally covers a confirmed signed-URL
    generation failure against a provider that did respond.
    """
    response.headers["Cache-Control"] = "no-store"
    _, tenant_id = auth
    if conversation_type is not None and conversation_type not in ("direct", "group"):
        raise HTTPException(
            status_code=400, detail="conversation_type must be 'direct' or 'group'"
        )
    entity_id = _resolve_entity_context(mode, staff_id, contact_id)

    auth_result = resolve_authorized_media(
        db,
        conversation_id,
        msgid,
        tenant_id,
        mode=mode,
        entity_id=entity_id,
        conversation_type=conversation_type,
    )
    media_file = auth_result.media_file
    effective_backend, effective_ref = _resolve_servable_backend_and_ref(
        media_file, "media access route"
    )
    # RND-207: variant=thumb resolves to the generated thumbnail (co-located
    # in the same backend / tenant prefix); anything else keeps the original.
    serve_ref, is_thumbnail, is_playback = _resolve_variant_serve_ref(
        media_file, variant, effective_ref
    )
    # size_bytes tracks only the original object; a thumbnail's byte size is
    # not persisted, so report None rather than the misleading original size.
    size_bytes = None if (is_thumbnail or is_playback) else media_file.file_size
    proxy_context_qs = _entity_context_query_string(mode, staff_id, contact_id, conversation_type)

    proxy_url = f"/api/conversations/{conversation_id}/messages/{msgid}/media{proxy_context_qs}"
    if is_thumbnail:
        proxy_url = _with_variant_thumb(proxy_url)
    elif is_playback:
        proxy_url = _with_variant_play(proxy_url)

    descriptor = build_access_descriptor(
        effective_backend=effective_backend,
        serve_ref=serve_ref,
        tenant_id=tenant_id,
        media_file=media_file,
        is_thumbnail=is_thumbnail,
        is_playback=is_playback,
        size_bytes=size_bytes,
        proxy_url=proxy_url,
        route_label="media access route",
    )
    return MediaAccessOut(media_id=media_file.id, **descriptor)


@router.get("/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}")
def get_nested_message_media(
    conversation_id: str,
    msgid: str,
    item_path: str,
    mode: Optional[str] = Query(
        None,
        description=(
            "RND-226: optional entity context: 'staff' or 'contact'. Same "
            "semantics as GET .../messages -- required to unambiguously "
            "authorize a nested media item on a message that only exists on "
            "one side of a direct/group conversation-id collision. Without "
            "it, a genuinely ambiguous conversation_id returns 400, same as "
            "the timeline route."
        ),
    ),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    conversation_type: Optional[str] = Query(
        None,
        description=(
            "RND-226: optional consistency check, 'direct' or 'group' -- same "
            "validation as GET .../messages. Never used to select or filter "
            "the message itself."
        ),
    ),
    variant: Optional[str] = Query(
        None,
        description=(
            "RND-207/RND-258: 'thumb' serves a generated thumbnail; 'play' "
            "serves a generated browser-playable voice derivative. Omitted "
            "voice requests use playback when available."
        ),
    ),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Serve an already-downloaded nested mixed/chatrecord media item's file
    content (RND-200 QA fix) — the nested-item analogue of
    get_message_media, reusing every storage/content-type primitive it
    uses. Not a second implementation: only resolve_authorized_media is
    called with item_path set (which additionally validates item_path and
    resolves via sdkfileid instead of archive_message_id — see its
    docstring); every step after that is identical to get_message_media,
    including the exact response-code taxonomy and the "never log/return
    sdkfileid, local_path, storage_ref, oss_key" guarantee.

    item_path is untrusted URL input, strictly validated against a fixed
    grammar (see app.services.media_access._validate_nested_media_path)
    before ever being used — never eval'd, never used as a filesystem
    path, never used to index an arbitrary object graph. A malformed path
    is rejected with 400; a well-formed path that does not resolve to a
    media-bearing node on THIS message is rejected with 404 — the same
    404 as "wrong conversation" or "wrong tenant", so a caller cannot
    distinguish "path doesn't exist" from "you can't see this message" by
    response shape alone.
    """
    _, tenant_id = auth
    if conversation_type is not None and conversation_type not in ("direct", "group"):
        raise HTTPException(
            status_code=400, detail="conversation_type must be 'direct' or 'group'"
        )
    entity_id = _resolve_entity_context(mode, staff_id, contact_id)

    auth_result = resolve_authorized_media(
        db,
        conversation_id,
        msgid,
        tenant_id,
        item_path=item_path,
        mode=mode,
        entity_id=entity_id,
        conversation_type=conversation_type,
    )
    effective_backend, effective_ref = _resolve_servable_backend_and_ref(
        auth_result.media_file, "nested media route"
    )
    serve_ref, _is_thumbnail, _is_playback = _resolve_variant_serve_ref(
        auth_result.media_file, variant, effective_ref
    )
    return serve_media_bytes(effective_backend, serve_ref, route_label="nested media route")


@router.get(
    "/api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}/access",
    response_model=NestedMediaAccessOut,
)
def get_nested_message_media_access(
    conversation_id: str,
    msgid: str,
    item_path: str,
    response: Response,
    mode: Optional[str] = Query(
        None,
        description=(
            "RND-226: optional entity context: 'staff' or 'contact'. Same "
            "semantics as GET .../messages -- required to unambiguously "
            "authorize a nested media item on a message that only exists on "
            "one side of a direct/group conversation-id collision. Without "
            "it, a genuinely ambiguous conversation_id returns 400, same as "
            "the timeline route."
        ),
    ),
    staff_id: Optional[str] = Query(None, description="Required when mode=staff"),
    contact_id: Optional[str] = Query(None, description="Required when mode=contact"),
    conversation_type: Optional[str] = Query(
        None,
        description=(
            "RND-226: optional consistency check, 'direct' or 'group' -- same "
            "validation as GET .../messages. Never used to select or filter "
            "the message itself."
        ),
    ),
    variant: Optional[str] = Query(
        None,
        description=(
            "RND-207/RND-258: 'thumb' returns a generated-thumbnail "
            "descriptor; 'play' returns a browser-playable voice descriptor. "
            "Omitted voice requests use playback when available."
        ),
    ),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    """
    Return a public access descriptor for one nested mixed/chatrecord
    media item (RND-200 QA fix) — the nested-item analogue of
    get_message_media_access, reusing the exact same signed-URL/proxy
    branching, TTL config, and object-key tenant-prefix check. Not a
    second storage-access implementation: only the authorization/lookup
    step differs (resolve_authorized_media called with item_path set,
    instead of None) — see its docstring for why resolution is keyed by
    sdkfileid rather than archive_message_id.

    Returns NestedMediaAccessOut, NOT MediaAccessOut (RND-200 QA
    security fix, second round): MediaAccessOut.media_id exposes the
    internal MediaFile primary key, which independent QA correctly
    rejected as a leak for the nested contract — the approved public
    handle for nested media is (conversation_id, msgid, item_path) only,
    never a database id. media_file.id is still used locally in this
    function (log lines only, never returned to a caller) for
    operator-facing diagnostics, exactly as before.

    access_type="proxy": url is get_nested_message_media (this same
    conversation_id/msgid/item_path), not the top-level get_message_media
    route — a nested item's bytes are never served through the single-
    media parent route, since a mixed/chatrecord message can hold more
    than one media item and the parent route has no way to disambiguate
    which one is meant.

    Cache-Control: no-store on every response — see
    MediaAccessNoStoreMiddleware, whose path pattern covers this route
    too.
    """
    response.headers["Cache-Control"] = "no-store"
    _, tenant_id = auth
    if conversation_type is not None and conversation_type not in ("direct", "group"):
        raise HTTPException(
            status_code=400, detail="conversation_type must be 'direct' or 'group'"
        )
    entity_id = _resolve_entity_context(mode, staff_id, contact_id)

    auth_result = resolve_authorized_media(
        db,
        conversation_id,
        msgid,
        tenant_id,
        item_path=item_path,
        mode=mode,
        entity_id=entity_id,
        conversation_type=conversation_type,
    )
    media_file = auth_result.media_file
    effective_backend, effective_ref = _resolve_servable_backend_and_ref(
        media_file, "nested media access route"
    )
    serve_ref, is_thumbnail, is_playback = _resolve_variant_serve_ref(
        media_file, variant, effective_ref
    )
    size_bytes = None if (is_thumbnail or is_playback) else media_file.file_size
    proxy_context_qs = _entity_context_query_string(mode, staff_id, contact_id, conversation_type)

    proxy_url = (
        f"/api/conversations/{conversation_id}/messages/{msgid}"
        f"/nested-media/{item_path}"
        f"{proxy_context_qs}"
    )
    if is_thumbnail:
        proxy_url = _with_variant_thumb(proxy_url)
    elif is_playback:
        proxy_url = _with_variant_play(proxy_url)

    descriptor = build_access_descriptor(
        effective_backend=effective_backend,
        serve_ref=serve_ref,
        tenant_id=tenant_id,
        media_file=media_file,
        is_thumbnail=is_thumbnail,
        is_playback=is_playback,
        size_bytes=size_bytes,
        proxy_url=proxy_url,
        route_label="nested media access route",
    )
    content_type = descriptor.pop("content_type")
    return NestedMediaAccessOut(mime_type=content_type, filename=None, **descriptor)
