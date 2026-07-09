"""
Media storage abstraction and safe local media path resolution.

RND-185 keeps the current local-filesystem behavior but routes media
reads/writes through a provider boundary so a future object-store provider
can be added without pushing SDK calls into the business layer.

This module never logs or returns a raw filesystem path to a caller that
might expose it in an API response. Local paths are returned only for
internal compatibility paths such as FastAPI FileResponse.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
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


class MediaStorageProvider(ABC):
    """Small provider contract for archived media bytes.

    Authorization and tenant checks stay in the application layer. The
    provider only knows how to persist and retrieve bytes for a storage
    reference that the caller has already authorized.
    """

    @abstractmethod
    def save_bytes(self, storage_ref: str, data: bytes) -> str:
        """Persist bytes and return the provider's stored reference."""

    @abstractmethod
    def read_bytes(self, storage_ref: str) -> bytes:
        """Read bytes for an existing media object."""

    @abstractmethod
    def exists(self, storage_ref: Optional[str]) -> bool:
        """Return True only when the referenced object exists."""

    @abstractmethod
    def delete(self, storage_ref: Optional[str]) -> bool:
        """Best-effort delete. Return True if a file/object was removed."""

    @abstractmethod
    def replace(self, source_ref: str, target_ref: str) -> str:
        """Publish source_ref at target_ref, returning the stored target ref."""

    @abstractmethod
    def size_bytes(self, storage_ref: str) -> int:
        """Return object size in bytes."""

    def get_download_url(self, storage_ref: str) -> Optional[str]:
        """Return a direct provider URL when one exists.

        The local provider intentionally returns None because this app
        serves local media through the authenticated API route.
        """
        return None

    def supports_local_path(self) -> bool:
        return False

    def get_local_path(self, storage_ref: Optional[str]) -> Optional[Path]:
        return None


class LocalStorageProvider(MediaStorageProvider):
    """Filesystem-backed provider preserving the existing local behavior."""

    def __init__(self, root: Optional[Path | str]):
        self.root = self._resolve_root(root)

    @staticmethod
    def _resolve_root(root: Optional[Path | str]) -> Optional[Path]:
        if root is None:
            return None
        try:
            return Path(root).resolve()
        except (OSError, ValueError):
            return None

    def _path_for_write(self, storage_ref: str) -> Path:
        if self.root is None:
            raise FileNotFoundError("media storage root is not configured")
        candidate = Path(storage_ref)
        if not candidate.is_absolute():
            candidate = self.root / candidate

        resolved_parent = candidate.parent.resolve()
        try:
            resolved_parent.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("media path escapes storage root") from exc

        if candidate.exists():
            resolved_candidate = candidate.resolve()
            try:
                resolved_candidate.relative_to(self.root)
            except ValueError as exc:
                raise ValueError("media path escapes storage root") from exc
            return resolved_candidate

        return resolved_parent / candidate.name

    def save_bytes(self, storage_ref: str, data: bytes) -> str:
        path = self._path_for_write(storage_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def read_bytes(self, storage_ref: str) -> bytes:
        path = self.get_local_path(storage_ref)
        if path is None:
            raise FileNotFoundError("media object is missing")
        return path.read_bytes()

    def exists(self, storage_ref: Optional[str]) -> bool:
        return self.get_local_path(storage_ref) is not None

    def delete(self, storage_ref: Optional[str]) -> bool:
        path = self.get_local_path(storage_ref)
        if path is None:
            return False
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def replace(self, source_ref: str, target_ref: str) -> str:
        source_path = self.get_local_path(source_ref)
        if source_path is None:
            raise FileNotFoundError("source media object is missing")
        target_path = self._path_for_write(target_ref)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_path, target_path)
        return str(target_path)

    def size_bytes(self, storage_ref: str) -> int:
        path = self.get_local_path(storage_ref)
        if path is None:
            raise FileNotFoundError("media object is missing")
        return path.stat().st_size

    def supports_local_path(self) -> bool:
        return True

    def get_local_path(self, storage_ref: Optional[str]) -> Optional[Path]:
        if not storage_ref or self.root is None:
            return None

        candidate = Path(storage_ref)
        if not candidate.is_absolute():
            candidate = self.root / candidate

        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            return None

        try:
            resolved.relative_to(self.root)
        except ValueError:
            return None

        if not resolved.is_file():
            return None

        return resolved


class UnsupportedMediaStorageProvider(ValueError):
    """Raised when configuration names a provider not implemented yet."""


def _configured_media_root() -> Optional[Path]:
    """Return the configured local media root, or None if unset.

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


def get_media_root() -> Optional[Path]:
    """Backward-compatible accessor for the local storage root."""
    provider = get_media_storage_provider()
    if isinstance(provider, LocalStorageProvider):
        return provider.root
    return None


def get_media_storage_provider() -> MediaStorageProvider:
    """Build the active media storage provider from environment config.

    MEDIA_STORAGE_PROVIDER is the new RND-185 selector. STORAGE_BACKEND is
    honored as a compatibility alias because this repo already documents
    it for the current local backend. Only local is implemented in this
    ticket.
    """
    provider_name = (
        os.environ.get("MEDIA_STORAGE_PROVIDER")
        or os.environ.get("STORAGE_BACKEND")
        or "local"
    ).strip().lower()

    if provider_name == "local":
        return LocalStorageProvider(_configured_media_root())

    raise UnsupportedMediaStorageProvider(
        f"Unsupported media storage provider: {provider_name}"
    )


def resolve_safe_media_path(local_path: Optional[str]) -> Optional[Path]:
    """Resolve local_path to a real, existing file under the media root.

    Returns None (never raises) if: no media root is configured, local_path
    is empty, the resolved real path escapes the media root (directory
    traversal via symlink or "../" segments), or the file does not exist.
    Callers must treat None as "safe 404" and must not include local_path
    itself in any response or log line.
    """
    provider = get_media_storage_provider()
    if not provider.supports_local_path():
        return None
    return provider.get_local_path(local_path)


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


def detect_image_type_from_bytes(data: bytes) -> Optional[str]:
    """Return an allow-listed image extension (".jpg", ".png", ".gif",
    ".webp") based on *data*'s magic-byte header, or None if it does not
    match any allowed image signature.

    Used by the RND-147 download script before marking a freshly
    downloaded file as "downloaded" — the sdkfileid, msgtype, or any
    caller-supplied hint must never be trusted for this decision, only the
    actual byte content.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


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
