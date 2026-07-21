"""Server-side thumbnail generation for list/timeline image display (RND-207).

The chat archive UI previously loaded full-resolution originals in the
timeline. This module produces a small, downscaled derivative (a *separate*
stored object, never a Qiniu CDN image-processing transform) so the list can
render a lightweight thumbnail while the viewer still opens the original.

Design constraints (all enforced here):

- **Pure + failure-isolated.** generate_thumbnail() returns None on any
  problem (unsupported/corrupt bytes, decompression bomb, encode failure)
  and never raises. A thumbnail is an optimization, never a correctness
  requirement — its absence must never fail original-media archival or
  serving (the caller falls back to the original). Callers still wrap the
  call defensively; this is belt-and-suspenders.
- **JPEG + PNG only** (RND-207 decision): opaque images -> JPEG; images with
  an alpha channel -> PNG (transparency preserved). No WebP/AVIF.
- **EXIF orientation is applied** (ImageOps.exif_transpose) so a
  phone-rotated photo thumbnails right-side-up.
- **Animated GIFs collapse to a static first frame** — the list shows a
  still preview; the original animated GIF is untouched in storage and the
  viewer still plays it. We deliberately do NOT re-encode animation.
- **No secrets / no raw content in logs.** This module never logs; it
  returns structured results and lets the caller log sanitized outcomes.
- **Resource-bounded** for the 2C2G box: a decompression-bomb guard rejects
  absurd pixel counts before decoding a full raster.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# Config (read fresh from env on each call, matching media_storage's
# get_signed_url_ttl_seconds convention — so tests can monkeypatch per case
# and an operator can change a value without a code change).
# ---------------------------------------------------------------------------

_MAX_EDGE_DEFAULT = 360
_MAX_EDGE_MIN = 32
_MAX_EDGE_MAX = 2048
_JPEG_QUALITY_DEFAULT = 82
_JPEG_QUALITY_MIN = 1
_JPEG_QUALITY_MAX = 95

# Reject inputs whose declared raster is implausibly large before we ever
# decode the full image (decompression-bomb defense). 40 megapixels is far
# above any legitimate chat photo yet well under Pillow's own default
# DecompressionBombError threshold, giving us an early, cheap rejection.
_MAX_SOURCE_PIXELS = 40_000_000


def thumbnails_enabled() -> bool:
    """Whether new-upload thumbnail generation and the backfill are active.
    Defaults to True; any value other than a recognized false-y token
    ("false"/"0"/"no"/"off") is treated as enabled."""
    raw = os.environ.get("MEDIA_THUMBNAIL_ENABLED", "").strip().lower()
    if not raw:
        return True
    return raw not in {"false", "0", "no", "off"}


def _max_edge() -> int:
    raw = os.environ.get("MEDIA_THUMBNAIL_MAX_EDGE", "").strip()
    if not raw:
        return _MAX_EDGE_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _MAX_EDGE_DEFAULT
    return max(_MAX_EDGE_MIN, min(_MAX_EDGE_MAX, value))


def _jpeg_quality() -> int:
    raw = os.environ.get("MEDIA_THUMBNAIL_JPEG_QUALITY", "").strip()
    if not raw:
        return _JPEG_QUALITY_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _JPEG_QUALITY_DEFAULT
    return max(_JPEG_QUALITY_MIN, min(_JPEG_QUALITY_MAX, value))


@dataclass(frozen=True)
class ThumbnailResult:
    """A generated thumbnail plus the dimensions the frontend needs.

    extension is an allow-listed image extension (".jpg" or ".png") suitable
    for building a thumbnail object key; content_type is the matching MIME
    type. width/height are the THUMBNAIL's pixel dimensions;
    source_width/source_height are the ORIGINAL's post-EXIF-orientation
    dimensions (stored so the list can reserve the correct aspect-ratio box
    even though it renders the smaller thumbnail)."""

    data: bytes
    extension: str
    content_type: str
    width: int
    height: int
    source_width: int
    source_height: int


def _has_alpha(image: Image.Image) -> bool:
    if image.mode in ("RGBA", "LA"):
        return True
    if image.mode == "P" and "transparency" in image.info:
        return True
    return False


def generate_thumbnail(data: bytes) -> Optional[ThumbnailResult]:
    """Generate a downscaled JPEG/PNG thumbnail from raw image bytes.

    Returns None (never raises) when the bytes are not a supported image,
    are corrupt, exceed the decompression-bomb guard, or fail to encode —
    the caller must treat None as "no thumbnail; serve the original".
    """
    if not data:
        return None

    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            if width <= 0 or height <= 0:
                return None
            if width * height > _MAX_SOURCE_PIXELS:
                return None
            is_animated = getattr(probe, "is_animated", False)
            if is_animated:
                # Static first frame only — never re-encode animation.
                probe.seek(0)
            # Apply EXIF orientation, then fully load into an independent
            # image so we can close the source handle. exif_transpose returns
            # a new image with orientation baked in and the EXIF tag dropped.
            image = ImageOps.exif_transpose(probe)
            image.load()
    except Exception:  # noqa: BLE001 - any decode failure -> no thumbnail
        return None

    try:
        oriented_w, oriented_h = image.size
        want_alpha = _has_alpha(image) and not is_animated

        if want_alpha:
            if image.mode != "RGBA":
                image = image.convert("RGBA")
        else:
            # Flatten any transparency/palette onto white for a clean JPEG.
            if image.mode != "RGB":
                image = image.convert("RGB")

        # Only ever downscale — never upscale a small original.
        max_edge = _max_edge()
        image.thumbnail((max_edge, max_edge), Image.LANCZOS)
        thumb_w, thumb_h = image.size

        buffer = io.BytesIO()
        if want_alpha:
            image.save(buffer, format="PNG", optimize=True)
            extension, content_type = ".png", "image/png"
        else:
            image.save(
                buffer,
                format="JPEG",
                quality=_jpeg_quality(),
                optimize=True,
                progressive=True,
            )
            extension, content_type = ".jpg", "image/jpeg"
    except Exception:  # noqa: BLE001 - any encode failure -> no thumbnail
        return None
    finally:
        try:
            image.close()
        except Exception:  # noqa: BLE001
            pass

    payload = buffer.getvalue()
    if not payload:
        return None

    return ThumbnailResult(
        data=payload,
        extension=extension,
        content_type=content_type,
        width=thumb_w,
        height=thumb_h,
        source_width=oriented_w,
        source_height=oriented_h,
    )
