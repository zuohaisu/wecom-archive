"""Response schemas for the media/nested-media access descriptor endpoints
(RND-221 — moved verbatim out of app.routers.conversations, no field/type/
order changes).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class MediaAccessOut(BaseModel):
    """Unified media access descriptor (RND-187) — the one shape the
    frontend consumes regardless of storage_backend, so it never needs to
    understand Qiniu vs local storage details, read storage_ref, or
    construct a media.crowntime.cn URL itself.

    access_type="signed_url": url is a short-lived, browser-usable Qiniu
    signed URL good until expires_at; the browser fetches it directly, no
    FastAPI proxying of image bytes.
    access_type="proxy": url is this backend's own authenticated route
    (unchanged local-media behavior); expires_at is None since the URL
    itself carries no time-boxed credential — the session cookie is what
    authorizes each request.
    """

    media_id: int
    storage_backend: str
    access_type: str
    url: str
    expires_at: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None


class NestedMediaAccessOut(BaseModel):
    """Public access descriptor for a nested mixed/chatrecord media item
    (RND-200 QA fix — security remediation). Deliberately a SEPARATE model
    from MediaAccessOut, not that model reused: MediaAccessOut.media_id
    exposes the internal MediaFile primary key, which was an accepted,
    pre-existing exposure for the single-media-per-message top-level
    contract (see get_message_media_access) but is NOT part of the
    approved nested-media public contract — the parent message + nested
    node path is the only public handle nested media is ever looked up
    by, and no internal database identifier may accompany it.

    Every other field mirrors MediaAccessOut's meaning exactly
    (access_type="proxy" vs "signed_url", expires_at only set for the
    latter) so nested and top-level media remain trivially similar for
    any consumer that already understands one of the two shapes — only
    the internal-id field is omitted, and mime_type/filename are named to
    match the per-node structured_content descriptor's own field names
    (see _build_nested_media_descriptor) rather than MediaAccessOut's
    content_type, for consistency within the nested contract itself.
    filename is always None today (no media type in this system carries
    one — see _build_nested_media_descriptor's docstring) but is kept as
    an explicit, documented field rather than omitted, matching the
    shape independent QA approved.
    """

    storage_backend: str
    access_type: str
    url: str
    expires_at: Optional[str] = None
    mime_type: Optional[str] = None
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
