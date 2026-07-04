"""
Safe local media path resolution for RND-144 (image download/render).

This module never logs or returns a raw filesystem path to a caller that
might expose it in an API response — it only returns Path objects for
internal use by the media-serving route, or None when the path cannot be
trusted. It performs no DB access; callers pass in local_path values they
already loaded from media_files.

Storage root: STORAGE_LOCAL_PATH (already documented in .env.example for
local media storage, previously unused by any code path).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_ALLOWED_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def get_media_root() -> Optional[Path]:
    """Return the configured media storage root, or None if unset.

    Not required for app startup — routes that depend on it degrade to a
    safe 404 when it is missing rather than raising at import/startup time.
    """
    raw = os.environ.get("STORAGE_LOCAL_PATH", "").strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except (OSError, ValueError):
        return None


def resolve_safe_media_path(local_path: Optional[str]) -> Optional[Path]:
    """Resolve local_path to a real, existing file under the media root.

    Returns None (never raises) if: no media root is configured, local_path
    is empty, the resolved real path escapes the media root (directory
    traversal via symlink or "../" segments), or the file does not exist.
    Callers must treat None as "safe 404" and must not include local_path
    itself in any response or log line.
    """
    if not local_path:
        return None
    root = get_media_root()
    if root is None:
        return None

    candidate = Path(local_path)
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None

    try:
        resolved.relative_to(root)
    except ValueError:
        return None

    if not resolved.is_file():
        return None

    return resolved


def detect_image_content_type(path: Path) -> Optional[str]:
    """Return a safe image Content-Type for *path* based on its extension,
    or None if the extension is not an allow-listed image type (reject
    rather than fall back to application/octet-stream — this route is
    image-only by scope)."""
    return _ALLOWED_IMAGE_CONTENT_TYPES.get(path.suffix.lower())


def resolve_image_file_state(local_path: Optional[str]) -> str:
    """Single source of truth for "can we serve local_path as an image" —
    shared by the timeline serializer (media_url / media_status decision)
    and the media route (actual file serving) so the two can never
    disagree (RND-144 QA fix: the timeline previously only checked
    resolve_safe_media_path, while the route additionally rejected
    disallowed extensions, so a same-tenant .bmp could be reported as
    available yet 404 when actually requested).

    Returns exactly one of:
      "servable"          — resolves safely under the media root, exists as
                             a file, and has an allow-listed image extension.
      "unsupported_type"  — resolves safely and exists, but the extension is
                             not one this route serves as an image.
      "missing"           — anything else: no media root configured, empty
                             local_path, directory traversal, or the file
                             does not exist.
    """
    resolved = resolve_safe_media_path(local_path)
    if resolved is None:
        return "missing"
    if detect_image_content_type(resolved) is None:
        return "unsupported_type"
    return "servable"


def resolve_servable_image_path(local_path: Optional[str]) -> Optional[Path]:
    """Return the resolved Path only when resolve_image_file_state(local_path)
    == "servable" — i.e. exactly the case the media route is allowed to
    serve. Convenience wrapper for callers (the media route) that need the
    actual Path, not just the tri-state classification."""
    resolved = resolve_safe_media_path(local_path)
    if resolved is None:
        return None
    if detect_image_content_type(resolved) is None:
        return None
    return resolved
