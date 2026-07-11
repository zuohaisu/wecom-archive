"""
Media storage abstraction and safe local media path resolution.

RND-185 introduced the provider boundary; RND-174 added QiniuStorageProvider
alongside LocalStorageProvider, selectable per deployment via
MEDIA_STORAGE_PROVIDER. A post-ship QA pass on RND-174 found that reusing
the deployment-wide provider setting to interpret *historical* media rows
was unsafe once mixed local/Qiniu storage was possible (switching the
default write provider must never reinterpret existing rows). This module
now resolves the provider per media_files row, from that row's own
storage_backend/storage_ref columns — see resolve_media_file_state() and
get_media_storage_provider(storage_backend=...). The no-argument factory
remains the deployment's configured *default write* provider only.

This module never logs or returns a raw filesystem path or object key to a
caller that might expose it in an API response. Local paths are returned
only for internal compatibility paths such as FastAPI FileResponse.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
import re
from pathlib import Path
from typing import Optional, Tuple

_ALLOWED_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


# ---------------------------------------------------------------------------
# Exception hierarchy (RND-174 QA remediation)
#
# Each subclasses the builtin exception type existing callers already catch
# (FileNotFoundError / OSError / ValueError) so pre-existing except clauses
# keep working unchanged, while new call sites can catch the specific type
# to distinguish "confirmed missing" from "provider unavailable" from
# "misconfigured" — collapsing all three into a bare 404 is exactly the bug
# this hierarchy exists to prevent.
# ---------------------------------------------------------------------------


class MediaObjectNotFound(FileNotFoundError):
    """The referenced object is confirmed not to exist (a real "not found"
    response from the provider, not a network/auth/SDK failure)."""


class MediaStorageUnavailable(OSError):
    """A storage operation could not be confirmed to succeed or fail —
    network timeout, auth failure, bucket error, SDK/service failure. Must
    never be treated as "the object doesn't exist"."""


class MediaStorageConfigurationError(ValueError):
    """Provider configuration is missing or invalid."""


class MediaStorageOperationError(OSError):
    """A storage operation (upload/move/delete) failed for a reason other
    than a confirmed "not found" or a confirmed "unavailable"."""


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

    def get_download_url(self, storage_ref: str, expires_in: Optional[int] = None) -> Optional[str]:
        """Return a direct, browser-usable provider URL when one exists,
        valid for approximately expires_in seconds (provider-specific;
        ignored by providers that never return a direct URL).

        The local provider intentionally returns None because this app
        serves local media through the authenticated API route — there is
        no direct "provider URL" for the local filesystem.
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


class UnsupportedMediaStorageProvider(MediaStorageConfigurationError):
    """Raised when configuration, or a media row's storage_backend, names a
    provider not implemented."""


_SAFE_KEY_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize_key_segment(value: str) -> str:
    """Collapse anything outside [A-Za-z0-9_.-] to '_' and strip leading
    dots, so a segment built from this can never become '..', an absolute
    path, or an empty/hidden path element. tenant_id and message identifiers
    are already trusted (DB-generated), but object keys are namespacing —
    not an authorization boundary (see module docstring / RND-156) — so
    this sanitization is defense in depth, applied unconditionally rather
    than only when a caller-supplied value is detected."""
    cleaned = _SAFE_KEY_SEGMENT_RE.sub("_", str(value)).lstrip(".")
    return cleaned or "_"


def build_tenant_media_key(tenant_id: str, category: str, identifier: str, suffix: str = "") -> str:
    """Build a stable, tenant-aware, traversal-safe storage reference:

        tenants/{tenant_id}/{category}/{identifier}{suffix}

    Used as both the local-provider relative path and the Qiniu object key
    — the same reference format works for either provider since it is just
    a '/'-separated string, never a caller-controlled filename. Deterministic
    per (tenant_id, category, identifier): the same inputs always produce the
    same key, so repeated/retried processing overwrites in place instead of
    accumulating duplicate objects.
    """
    safe_tenant = _sanitize_key_segment(tenant_id)
    safe_category = _sanitize_key_segment(category)
    safe_identifier = _sanitize_key_segment(identifier)
    return f"tenants/{safe_tenant}/{safe_category}/{safe_identifier}{suffix}"


def object_key_tenant_prefix_matches(storage_ref: Optional[str], tenant_id: str) -> bool:
    """True iff storage_ref's leading "tenants/{tenant}/" segment (see
    build_tenant_media_key) matches tenant_id's own sanitized form.

    RND-187 defense-in-depth: a media_files row is already resolved through
    a tenant-scoped query before a signed URL is ever generated, so this key
    is never attacker-controlled in production. This check exists so a
    corrupted/mistagged row (whose storage_ref embeds a different tenant's
    prefix than its own tenant_id column) can never mint a signed URL for
    another tenant's object on the strength of tenant_id alone — the object
    key itself must independently agree.
    """
    if not storage_ref or not tenant_id:
        return False
    expected_prefix = f"tenants/{_sanitize_key_segment(tenant_id)}/"
    return storage_ref.startswith(expected_prefix)


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


def _require_qiniu_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        # QiniuConfigurationError import is deferred (see _build_qiniu_provider)
        # so local-mode startup never imports the qiniu package.
        from app.qiniu_storage import QiniuConfigurationError

        raise QiniuConfigurationError(
            f"MEDIA_STORAGE_PROVIDER=qiniu_kodo requires {name} to be set"
        )
    return value


def _build_qiniu_provider() -> MediaStorageProvider:
    """Construct QiniuStorageProvider from QINIU_* environment variables.

    Only called when qiniu_kodo is the selected provider — this is the one
    place a missing/invalid Qiniu credential can fail, and it fails loudly
    (QiniuConfigurationError) rather than falling back to local.
    """
    from app.qiniu_storage import QiniuStorageProvider

    access_key = _require_qiniu_env("QINIU_ACCESS_KEY")
    secret_key = _require_qiniu_env("QINIU_SECRET_KEY")
    bucket = _require_qiniu_env("QINIU_BUCKET")
    domain = _require_qiniu_env("QINIU_DOMAIN")
    region = os.environ.get("QINIU_REGION", "").strip() or None
    timeout_raw = os.environ.get("QINIU_TIMEOUT_SECONDS", "").strip()
    timeout = float(timeout_raw) if timeout_raw else 30.0

    return QiniuStorageProvider(
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        domain=domain,
        region=region,
        timeout=timeout,
    )


_SIGNED_URL_TTL_DEFAULT_SECONDS = 900
_SIGNED_URL_TTL_MIN_SECONDS = 60
_SIGNED_URL_TTL_MAX_SECONDS = 3600


def get_signed_url_ttl_seconds() -> int:
    """Validated MEDIA_SIGNED_URL_TTL_SECONDS accessor (RND-187).

    Bounds [60, 3600] seconds, default 900 when unset. Reads the env var
    fresh on every call — matching every other *_env accessor in this
    module — rather than caching, so tests can monkeypatch os.environ per
    case without a process restart. An invalid (non-integer) or
    out-of-bounds value fails loudly (MediaStorageConfigurationError)
    rather than silently clamping, matching this module's existing
    QiniuConfigurationError convention for misconfiguration.
    """
    raw = os.environ.get("MEDIA_SIGNED_URL_TTL_SECONDS", "").strip()
    if not raw:
        return _SIGNED_URL_TTL_DEFAULT_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise MediaStorageConfigurationError(
            "MEDIA_SIGNED_URL_TTL_SECONDS must be an integer number of seconds"
        ) from exc
    if value < _SIGNED_URL_TTL_MIN_SECONDS or value > _SIGNED_URL_TTL_MAX_SECONDS:
        raise MediaStorageConfigurationError(
            "MEDIA_SIGNED_URL_TTL_SECONDS must be between "
            f"{_SIGNED_URL_TTL_MIN_SECONDS} and {_SIGNED_URL_TTL_MAX_SECONDS} seconds"
        )
    return value


def _resolve_default_provider_name() -> str:
    """The deployment's configured *default write* provider name — where
    NEW media is written. MEDIA_STORAGE_PROVIDER is the RND-185 selector;
    STORAGE_BACKEND is honored as a compatibility alias. Defaults to
    "local" when unset."""
    return (
        os.environ.get("MEDIA_STORAGE_PROVIDER")
        or os.environ.get("STORAGE_BACKEND")
        or "local"
    ).strip().lower()


def get_configured_write_backend_name() -> str:
    """Public accessor for the default write provider's name (not the
    provider instance) — used by the download worker to stamp new rows with
    the backend they were actually written through."""
    return _resolve_default_provider_name()


def get_media_storage_provider(storage_backend: Optional[str] = None) -> MediaStorageProvider:
    """Build a media storage provider.

    storage_backend, when given, selects the provider explicitly — this is
    how RND-174's remediation resolves media on a *per-row* basis (see
    resolve_media_file_state()): a media_files row's own storage_backend
    column decides which provider serves it, never the deployment-wide
    write-provider setting. This is what makes switching
    MEDIA_STORAGE_PROVIDER safe for mixed local/Qiniu storage — it changes
    where NEW media is written, not how EXISTING rows are interpreted.

    storage_backend=None (default) falls back to the deployment's
    configured default write provider (MEDIA_STORAGE_PROVIDER /
    STORAGE_BACKEND env vars, defaulting to "local") — this is the RND-185/
    RND-174-original behavior, kept as the default for callers that only
    care about "the provider new writes go through" (the worker) or for
    backward compatibility with existing single-argument call sites.

    qiniu_kodo is only constructed — and QINIU_* env vars only read/
    validated — when explicitly selected (by config or by a row's
    storage_backend); importing this module or running in local mode never
    touches Qiniu configuration or the qiniu package. Unknown provider
    names — from config or from a row — fail loudly
    (UnsupportedMediaStorageProvider) rather than silently falling back to
    local.
    """
    provider_name = (storage_backend or _resolve_default_provider_name()).strip().lower()

    if provider_name == "local":
        return LocalStorageProvider(_configured_media_root())

    if provider_name == "qiniu_kodo":
        return _build_qiniu_provider()

    raise UnsupportedMediaStorageProvider(
        f"Unsupported media storage provider: {provider_name}"
    )


def get_default_media_storage_provider() -> MediaStorageProvider:
    """The deployment's configured default *write* provider. Equivalent to
    get_media_storage_provider() with no argument — named explicitly so
    call sites that write new media (the download worker) read clearly as
    "use the default", distinct from call sites that read existing media
    (which must use get_media_storage_provider_for_backend(row.storage_backend))."""
    return get_media_storage_provider()


def get_media_storage_provider_for_backend(storage_backend: str) -> MediaStorageProvider:
    """Explicit, required-argument form of get_media_storage_provider() for
    call sites resolving a specific media_files row's provider. Raises
    UnsupportedMediaStorageProvider (not a silent default) when
    storage_backend is empty/None — a media row should never reach this
    call with an unset backend; see resolve_media_file_state() for the
    legacy-local_path compatibility fallback that normally prevents that."""
    if not storage_backend:
        raise UnsupportedMediaStorageProvider("storage_backend is required and was not set")
    return get_media_storage_provider(storage_backend)


def resolve_safe_media_path(
    local_path: Optional[str], storage_backend: Optional[str] = None
) -> Optional[Path]:
    """Resolve local_path to a real, existing file under the media root.

    storage_backend selects the provider explicitly (see
    get_media_storage_provider); defaults to the deployment's default write
    provider when omitted, for backward compatibility.

    Returns None (never raises) if: the selected provider is not a local
    provider, no media root is configured, local_path is empty, the
    resolved real path escapes the media root (directory traversal via
    symlink or "../" segments), or the file does not exist. Callers must
    treat None as "safe 404" and must not include local_path itself in any
    response or log line.
    """
    provider = get_media_storage_provider(storage_backend)
    if not provider.supports_local_path():
        return None
    return provider.get_local_path(local_path)


def detect_image_content_type(path: Path) -> Optional[str]:
    """Return a safe image Content-Type for *path* based on its extension,
    or None if the extension is not an allow-listed image type (reject
    rather than fall back to application/octet-stream — this route is
    image-only by scope)."""
    return _ALLOWED_IMAGE_CONTENT_TYPES.get(path.suffix.lower())


def detect_image_content_type_for_ref(storage_ref: Optional[str]) -> Optional[str]:
    """Same allow-list lookup as detect_image_content_type, but for a
    provider-opaque storage reference (e.g. a Qiniu object key) rather than
    a real filesystem Path — cloud storage_refs have no Path semantics to
    resolve, only a suffix to read."""
    if not storage_ref:
        return None
    return _ALLOWED_IMAGE_CONTENT_TYPES.get(Path(storage_ref).suffix.lower())


def resolve_image_file_state(storage_ref: Optional[str], storage_backend: Optional[str] = None) -> str:
    """Single source of truth for "can we serve storage_ref as an image" —
    shared by the timeline serializer (media_url / media_status decision)
    and the media route (actual file serving) so the two can never
    disagree (RND-144 QA fix: the timeline previously only checked
    resolve_safe_media_path, while the route additionally rejected
    disallowed extensions, so a same-tenant .bmp could be reported as
    available yet 404 when actually requested).

    storage_backend selects the provider explicitly. Production call sites
    (the media route, the timeline serializer) must pass a media_files
    row's own storage_backend — see resolve_media_file_state(), which does
    this plus the legacy local_path compatibility fallback — rather than
    relying on the deployment-wide default write provider, so switching
    MEDIA_STORAGE_PROVIDER never reinterprets an existing row (RND-174 QA
    fix). storage_backend=None falls back to the default write provider,
    kept only for backward compatibility with pre-RND-174-remediation
    single-argument callers.

    Provider-aware: for a provider that supports_local_path()
    (LocalStorageProvider), behavior is unchanged — the reference is
    resolved and existence-checked against the local media root. For a
    provider that does not (e.g. QiniuStorageProvider), the same tri-state
    is derived from provider.exists() plus an extension check on the
    storage_ref string, since there is no local filesystem to resolve.

    Returns exactly one of:
      "servable"          — the object exists (safely, for local; per the
                             provider, for cloud) and has an allow-listed
                             image extension.
      "unsupported_type"  — the object exists but the extension is not one
                             this route serves as an image.
      "missing"           — anything else: no storage configured, empty
                             storage_ref, directory traversal (local), or
                             the object is confirmed not to exist
                             (MediaObjectNotFound from a cloud provider is
                             treated the same way).

    Raises MediaStorageUnavailable or MediaStorageConfigurationError
    (never swallowed into "missing") when a cloud provider cannot confirm
    either way whether the object exists — a provider outage must never be
    reported as missing media (RND-174 QA fix). Callers that want a
    best-effort degrade (e.g. a list view covering many rows) must catch
    these explicitly; the dedicated media-serving route must not.
    """
    provider = get_media_storage_provider(storage_backend)

    if provider.supports_local_path():
        resolved = resolve_safe_media_path(storage_ref, storage_backend)
        if resolved is None:
            return "missing"
        if detect_image_content_type(resolved) is None:
            return "unsupported_type"
        return "servable"

    if not storage_ref:
        return "missing"
    # provider.exists() returns False only for a *confirmed* "not found";
    # it raises MediaStorageUnavailable/MediaStorageConfigurationError for
    # anything ambiguous (network/auth/SDK failure) — deliberately left
    # uncaught here so callers cannot mistake "the provider is down" for
    # "missing" (RND-174 QA fix).
    if not provider.exists(storage_ref):
        return "missing"
    if detect_image_content_type_for_ref(storage_ref) is None:
        return "unsupported_type"
    return "servable"


def resolve_effective_storage_reference(
    storage_backend: Optional[str],
    storage_ref: Optional[str],
    local_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Compatibility rule (RND-174 QA remediation): resolve a media_files
    row's *effective* (storage_backend, storage_ref) pair.

    Rows written by the current code always have storage_backend set
    explicitly. Rows that predate the storage_backend/storage_ref columns
    (or were never backfilled — defensive, should not happen after the
    migration) fall back to treating a populated legacy local_path as a
    local-backend reference: local_path is legacy/local-only and must never
    be treated as a Qiniu (or any non-local) storage reference. A row with
    neither is not yet resolvable (still pending/failed).
    """
    if storage_backend:
        return storage_backend, storage_ref
    if local_path:
        return "local", local_path
    return None, None


def resolve_media_file_state(media_file) -> str:
    """resolve_image_file_state(), but driven entirely by a media_files
    row's own storage fields (storage_backend/storage_ref, falling back to
    legacy local_path per resolve_effective_storage_reference) instead of
    the deployment-wide default provider — the per-row resolution RND-174's
    QA remediation requires. Duck-typed: accepts anything with
    .storage_backend/.storage_ref/.local_path attributes (a MediaFile ORM
    row, or an equivalent test double).

    Propagates MediaStorageUnavailable / MediaStorageConfigurationError —
    see resolve_image_file_state's docstring for why these must not be
    collapsed into "missing".
    """
    effective_backend, effective_ref = resolve_effective_storage_reference(
        getattr(media_file, "storage_backend", None),
        getattr(media_file, "storage_ref", None),
        getattr(media_file, "local_path", None),
    )
    if effective_backend is None:
        return "missing"
    return resolve_image_file_state(effective_ref, effective_backend)


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


def resolve_servable_image_path(
    local_path: Optional[str], storage_backend: Optional[str] = None
) -> Optional[Path]:
    """Return the resolved Path only when resolve_image_file_state(local_path)
    == "servable" — i.e. exactly the case the media route is allowed to
    serve. Convenience wrapper for callers (the media route) that need the
    actual Path, not just the tri-state classification."""
    resolved = resolve_safe_media_path(local_path, storage_backend)
    if resolved is None:
        return None
    if detect_image_content_type(resolved) is None:
        return None
    return resolved
