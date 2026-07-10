"""
Media classification for timeline messages (RND-133 Phase 1).

Phase 1 does NOT implement media download, object storage, or a
file-serving route (see docs/research for the full RND-133 scope) — this
module only classifies what a message's msgtype *is* and, given that no
download path exists yet, what status the UI should present for it. It
never touches sdkfileid content, local_path, oss_key, or any encrypted
payload — only the msgtype string and a boolean presence flag.

The product model supports text/image/video/voice/file/unsupported/unknown
regardless of what the current tenant/deployment can actually render — a
media type being unavailable today must never be reported as "only images
are supported" or similar tenant-specific claims baked into the contract.
"""

from __future__ import annotations

from typing import NamedTuple, Optional


class MediaClassification(NamedTuple):
    media_type: str
    media_status: Optional[str]
    unsupported_reason: Optional[str]


def classify_media(msgtype: Optional[str], has_sdkfileid: bool) -> MediaClassification:
    """Classify a message's media type and current renderability.

    Does not require or perform any DB/file access — msgtype and
    has_sdkfileid are the only inputs, so this stays pure and cheap to call
    per message in the timeline response.
    """
    if not msgtype:
        return MediaClassification("unknown", "unknown", "missing_msgtype")

    if msgtype == "text":
        return MediaClassification("text", None, None)

    if msgtype == "image":
        if has_sdkfileid:
            return MediaClassification(
                "image", "not_downloaded", "media_download_not_implemented"
            )
        return MediaClassification("image", "unknown", "media_download_not_implemented")

    if msgtype == "video":
        status = "not_downloaded" if has_sdkfileid else "unsupported"
        return MediaClassification("video", status, "video_playback_not_implemented")

    if msgtype == "voice":
        status = "not_downloaded" if has_sdkfileid else "unsupported"
        return MediaClassification("voice", status, "voice_playback_not_implemented")

    if msgtype == "file":
        status = "not_downloaded" if has_sdkfileid else "unsupported"
        return MediaClassification("file", status, "file_download_not_implemented")

    return MediaClassification("unsupported", "unsupported", "unsupported_msgtype")


def resolve_image_media_status(
    base: MediaClassification,
    media_file_download_status: Optional[str],
    file_state: str = "missing",
) -> MediaClassification:
    """Combine a base image classification with media_files download state
    (RND-144 Phase 1 — image download/render).

    Stays pure: callers are responsible for the media_files DB lookup and
    the storage servability check (see
    app/media_storage.py:resolve_image_file_state /
    resolve_media_file_state) and pass the result in as a plain string.
    Only ever changes anything when base.media_type == "image" — every
    other media type passes through unchanged.

    file_state must be one of "servable" / "unsupported_type" / "missing" /
    "unavailable":

      "servable"         — the media route can serve this file (200).
      "unsupported_type" — exists but has a disallowed extension.
      "missing"          — the object is *confirmed* not to exist (or no
                            storage is configured at all). Never used for a
                            provider that could not be reached — see
                            "unavailable".
      "unavailable"      — the storage provider could not confirm the
                            object's state (a transient outage, timeout, or
                            configuration problem) — see
                            app.media_storage.MediaStorageUnavailable /
                            MediaStorageConfigurationError. Distinct from
                            "missing" (RND-174 QA fix): a temporary Qiniu
                            outage must never be reported as if the media
                            were actually gone — the media route uses the
                            same distinction to return 503 instead of 404.

    "servable"/"unsupported_type"/"missing" keep the media route and this
    classifier in agreement on media_status=="available" (RND-144 QA fix,
    unchanged); "unavailable" is additive and does not change that
    guarantee.

    media_file_download_status is None when no media_files row exists for
    the message (nothing to combine with — base classification stands).
    """
    if base.media_type != "image":
        return base

    if media_file_download_status == "downloaded":
        if file_state == "servable":
            return MediaClassification("image", "available", None)
        if file_state == "unsupported_type":
            return MediaClassification("image", "failed", "media_file_type_unsupported")
        if file_state == "unavailable":
            return MediaClassification("image", "unavailable", "media_storage_unavailable")
        return MediaClassification("image", "failed", "media_file_missing_on_disk")

    if media_file_download_status == "failed":
        return MediaClassification("image", "failed", "media_download_failed")

    # "pending", or no media_files row at all (None) — base classification
    # (not_downloaded/unknown, media_download_not_implemented) already
    # covers this correctly.
    return base
