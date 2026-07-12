"""
Media classification for timeline messages (RND-133 Phase 1; RND-196
rework — dispatch now driven by app.message_type_registry).

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

RND-196: this module no longer *owns* message-type definitions — which
msgtypes are text/media, and whether one is supported/partial/unsupported/
unknown, is now decided by app.message_type_registry. This module keeps
only the "media strategy" decision that was never the registry's job: how
a msgtype's own download/playback state (has_sdkfileid, and — for
image — media_files/storage state via resolve_image_media_status)
resolves to media_status/unsupported_reason. Dispatch below branches on
MessageSupportStatus (SUPPORTED vs PARTIAL vs UNSUPPORTED vs UNKNOWN), not
on a literal msgtype name — so a future msgtype the registry marks
SUPPORTED or PARTIAL is handled correctly with zero changes here; only
registering it in the registry is required.

The UNKNOWN branch (a msgtype the registry has never heard of) is
distinct from the UNSUPPORTED branch (a real, named WeCom type the
registry knows about but this project hasn't built rendering for) — see
MessageSupportStatus's docstring. Before RND-196 both collapsed into the
same media_type="unsupported" value; they now report "unsupported" and
"unknown" respectively, matching the missing-msgtype case's existing use
of "unknown".
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from app.message_type_registry import (
    MessageCategory,
    MessageSupportStatus,
    resolve as resolve_message_type,
)

# Human-readable unsupported_reason overrides for the msgtypes that had
# one before this module became registry-driven — preserved verbatim so
# existing callers/tests see byte-identical reason strings. A
# SUPPORTED/PARTIAL msgtype newly registered without an override here
# gets a generic, still-descriptive f"{msgtype}_not_implemented" instead
# of a KeyError — see classify_media().
_REASON_OVERRIDES = {
    "image": "media_download_not_implemented",
    "video": "video_playback_not_implemented",
    "voice": "voice_playback_not_implemented",
    "file": "file_download_not_implemented",
}


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

    definition = resolve_message_type(msgtype)

    if definition.support_status == MessageSupportStatus.UNKNOWN:
        # A msgtype the registry has never heard of — distinct from a
        # real, named WeCom type we simply haven't built (see below).
        return MediaClassification("unknown", "unknown", "unregistered_msgtype")

    if msgtype == "text":
        return MediaClassification("text", None, None)

    if definition.category == MessageCategory.SYSTEM:
        # RND-198: system events get a distinct media_type so the frontend
        # can dispatch to system_card rendering instead of a structured
        # business card or media preview.
        return MediaClassification("system", None, None)

    if definition.support_status in (
        MessageSupportStatus.SUPPORTED,
        MessageSupportStatus.PARTIAL,
    ) and definition.category != MessageCategory.MEDIA:
        # RND-197: link/location/markdown/news/miniprogram/card/docmsg are
        # structured-field types, not byte-bearing media — they have no
        # sdkfileid/download-status concept at all, so the byte-bearing
        # branch below (not_downloaded/unknown reasoning) does not apply.
        # The frontend dispatches these by renderer_strategy ==
        # "structured_card" (via TimelineMessageOut.renderer_strategy),
        # not by media_type, so "structured" only needs to be distinct
        # from every other media_type value — it carries no further
        # meaning. audio_doc stays in the byte-bearing branch below
        # (category MEDIA) since it may reference actual audio bytes.
        return MediaClassification("structured", None, None)

    if definition.support_status in (
        MessageSupportStatus.SUPPORTED,
        MessageSupportStatus.PARTIAL,
    ):
        # image today (SUPPORTED) and video/voice/file/audio_archive/
        # audio_doc (PARTIAL) — the only difference between the two tiers
        # is what to report when there is no sdkfileid at all: SUPPORTED
        # (image) still has a real download path so "unknown" is honest;
        # PARTIAL types have no path at all yet, so "unsupported" is.
        no_sdkfileid_status = (
            "unknown"
            if definition.support_status == MessageSupportStatus.SUPPORTED
            else "unsupported"
        )
        status = "not_downloaded" if has_sdkfileid else no_sdkfileid_status
        reason = _REASON_OVERRIDES.get(msgtype, f"{msgtype}_not_implemented")
        return MediaClassification(msgtype, status, reason)

    # UNSUPPORTED: a real, named WeCom type (location/link/card/emotion/
    # weapp/todo/revoke/mixed/chatrecord/sys/...) the registry knows about
    # but this project has not built rendering for — media_type stays the
    # generic "unsupported" bucket (unchanged contract), not the specific
    # msgtype, since that is what existing callers/tests already expect.
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
