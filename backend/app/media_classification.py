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
