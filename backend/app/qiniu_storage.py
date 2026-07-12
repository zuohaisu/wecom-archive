"""
Qiniu Kodo object storage provider (RND-174).

This module is only imported when MEDIA_STORAGE_PROVIDER=qiniu_kodo is
selected, or when a media_files row's own storage_backend is "qiniu_kodo"
(see app/media_storage.py:get_media_storage_provider's lazy import).
Importing the application in local mode never imports this module and
never requires the `qiniu` package to have valid credentials configured.

SDK boundary: every call into the `qiniu` package lives in this file.
Callers (the download worker, the media route) only ever see the
MediaStorageProvider contract — they never import `qiniu` or construct
Qiniu requests themselves.

Serving strategy: RND-174 kept media entirely behind the existing
authenticated backend route (get_download_url() always returned None).
RND-187 adds real signed-URL delivery: get_download_url(storage_ref,
expires_in) now returns a short-lived, browser-usable signed URL via the
official SDK's Auth.private_download_url — but only ever called by
app.routers.conversations' media-access-descriptor route, and only after
that route completes the full authenticate -> tenant -> media-ownership ->
object-key-prefix check chain. This module itself has no authorization
logic; it trusts that any storage_ref it is asked to sign has already been
authorized by the caller. read_bytes() is unchanged: it still fetches the
object server-side using its own short-lived (60s) *internal* private
download credential over HTTPS, discarded immediately after the HTTP fetch
and never logged or returned — that internal fetch token is a different,
unrelated use of the same SDK primitive from the client-facing signed URL
get_download_url() now produces.

Error handling never surfaces raw Qiniu SDK response bodies, URLs, or
exception text (qiniu.http.response.ResponseInfo can carry the request URL
and body in its `error`/`text_body`/`url` attributes) — only a short,
fixed message plus an HTTP-style status code, matching how
LocalStorageProvider's own errors carry no filesystem paths. Exceptions
raised here are the shared app.media_storage classification
(MediaObjectNotFound / MediaStorageUnavailable / MediaStorageOperationError
/ QiniuConfigurationError) so callers can distinguish "confirmed missing"
from "provider unavailable" instead of every failure collapsing into a
404-shaped "missing" (a confirmed post-ship QA finding — see
docs/research/rnd_174_qiniu_kodo_provider.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageProvider,
    MediaStorageUnavailable,
)

_INTERNAL_FETCH_URL_EXPIRY_SECONDS = 60
_QINIU_NOT_FOUND_STATUS = 612


class QiniuConfigurationError(MediaStorageConfigurationError):
    """Raised when qiniu_kodo is selected but required configuration is
    missing or invalid. Messages only ever name the missing/invalid
    variable, never its value."""


def _sanitized(exc: Exception, fallback: str) -> str:
    """Short, fixed-shape error text — never includes exc's str(), which for
    network/SDK exceptions can embed request URLs (including signed query
    strings) or response bodies."""
    return f"{fallback} ({type(exc).__name__})"


def redact_signed_url_for_log(url: Optional[str]) -> str:
    """Return url with every query-string value replaced by [REDACTED].

    No current call site logs a signed URL — every call site that issues
    one (app.routers.conversations' media-access route) deliberately omits
    it from log lines entirely, which is the preferred discipline. This
    helper exists as a hard backstop for the rare diagnostic path that might
    otherwise be tempted to include a URL in a log line, so a future call
    site cannot accidentally leak the `token`/`e` (expiry) query parameters
    a private_download_url embeds.
    """
    if not url:
        return ""
    split = urlsplit(url)
    if not split.query:
        return url
    redacted_query = "&".join(
        f"{pair.split('=', 1)[0]}=[REDACTED]" if "=" in pair else f"{pair}=[REDACTED]"
        for pair in split.query.split("&")
        if pair
    )
    return urlunsplit((split.scheme, split.netloc, split.path, redacted_query, split.fragment))


def normalize_qiniu_https_base_url(raw: Optional[str]) -> str:
    """Validate and normalize QINIU_DOMAIN into a clean HTTPS base URL.

    Requirements (RND-174 QA remediation — the original implementation
    hardcoded `http://` regardless of what QINIU_DOMAIN contained, which
    both downgraded transport security and produced a malformed URL like
    "http://https://host/..." whenever an operator supplied a fully
    qualified https:// value):

      - scheme must be exactly "https" — a missing scheme or "http://" (or
        any other scheme) is rejected outright rather than silently
        upgraded/downgraded, so a misconfiguration fails loudly instead of
        quietly serving over plaintext.
      - host (netloc) must be present and well-formed (no embedded spaces).
      - no query string or fragment (QINIU_DOMAIN is a base URL, not a
        pre-built request).
      - a trailing slash (or any trailing-slash path) is normalized away so
        joining with a storage_ref never produces a double slash.

    Raises QiniuConfigurationError with a message that only ever describes
    *what is wrong with the shape* of the value — never echoes the raw
    input back (avoids ever logging an operator's pasted-in secret/typo
    verbatim).
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise QiniuConfigurationError("QINIU_DOMAIN must not be empty")

    parsed = urlsplit(candidate)

    if not parsed.scheme:
        raise QiniuConfigurationError(
            "QINIU_DOMAIN must be a full HTTPS base URL, e.g. "
            "https://your-bound-domain.example.com (missing scheme)"
        )
    scheme = parsed.scheme.lower()
    if scheme == "http":
        raise QiniuConfigurationError(
            "QINIU_DOMAIN must use https:// — http:// is not permitted for "
            "private media retrieval"
        )
    if scheme != "https":
        raise QiniuConfigurationError(
            "QINIU_DOMAIN has an unsupported URL scheme; use https://"
        )
    if not parsed.netloc or " " in parsed.netloc:
        raise QiniuConfigurationError("QINIU_DOMAIN has a missing or malformed host")
    if parsed.query or parsed.fragment:
        raise QiniuConfigurationError(
            "QINIU_DOMAIN must be a bare base URL — no query string or fragment"
        )

    normalized_path = parsed.path.rstrip("/")
    return urlunsplit(("https", parsed.netloc, normalized_path, "", ""))


class QiniuStorageProvider(MediaStorageProvider):
    """Object-storage provider backed by Qiniu Kodo.

    storage_ref is always a Qiniu object key — a provider-opaque string,
    never a local filesystem path. supports_local_path() is False and
    get_local_path() always returns None so callers can never be tempted
    to treat a Qiniu key as something Path()-like.
    """

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        bucket: str,
        domain: str,
        region: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        if not access_key or not secret_key or not bucket or not domain:
            raise QiniuConfigurationError(
                "QiniuStorageProvider requires access_key, secret_key, bucket, and domain"
            )

        self._base_url = normalize_qiniu_https_base_url(domain)

        import qiniu  # local import: keep the SDK out of local-mode startup

        self._qiniu = qiniu
        self._auth = qiniu.Auth(access_key, secret_key)

        bucket_manager_kwargs = {}
        if region:
            try:
                bucket_manager_kwargs["regions"] = [
                    qiniu.Region.from_region_id(region)
                ]
            except Exception:  # noqa: BLE001 - fall back to auto region discovery
                pass
        self._bucket_manager = qiniu.BucketManager(self._auth, **bucket_manager_kwargs)

        self._bucket = bucket
        self._timeout = timeout

    def _object_url(self, storage_ref: str) -> str:
        """URL-safe join of the validated HTTPS base URL and storage_ref —
        never raw string concatenation, so a storage_ref can never smuggle
        in path segments that alter the base host/scheme."""
        return f"{self._base_url}/{quote(storage_ref, safe='/')}"

    # -- write path ---------------------------------------------------

    def save_bytes(self, storage_ref: str, data: bytes) -> str:
        # RND-186 QA fix: this used to call detect_image_content_type_for_ref
        # (image-only), so uploading a video/voice/file object silently
        # degraded its Qiniu-side Content-Type to "application/octet-stream"
        # even when media_files.mime_type had already recorded the correct
        # value in the database. detect_media_content_type_for_ref() is the
        # generalized lookup (image/video/voice/file) — see its docstring
        # in app/media_storage.py for why trusting storage_ref's extension
        # here is safe. Image uploads are completely unaffected: every
        # image extension this function ever recognized is still resolved
        # identically, since the image entries are the same
        # _ALLOWED_IMAGE_CONTENT_TYPES table, unmodified.
        from app.media_storage import detect_media_content_type_for_ref

        mime_type = detect_media_content_type_for_ref(storage_ref) or "application/octet-stream"
        up_token = self._auth.upload_token(self._bucket, key=storage_ref, expires=3600)
        try:
            _ret, info = self._qiniu.put_data(up_token, storage_ref, data, mime_type=mime_type)
        except Exception as exc:  # noqa: BLE001 - network/SDK errors, never re-raised raw
            raise MediaStorageOperationError(_sanitized(exc, "qiniu upload failed")) from exc
        if not info.ok():
            raise MediaStorageOperationError(f"qiniu upload failed (status={info.status_code})")
        return storage_ref

    def replace(self, source_ref: str, target_ref: str) -> str:
        try:
            _ret, info = self._bucket_manager.move(
                self._bucket, source_ref, self._bucket, target_ref, force="true"
            )
        except Exception as exc:  # noqa: BLE001
            raise MediaStorageOperationError(_sanitized(exc, "qiniu move failed")) from exc
        if info.ok():
            return target_ref
        if info.status_code == _QINIU_NOT_FOUND_STATUS:
            raise MediaObjectNotFound("source media object is missing")
        raise MediaStorageOperationError(f"qiniu move failed (status={info.status_code})")

    # -- read path ------------------------------------------------------

    def read_bytes(self, storage_ref: str) -> bytes:
        if not storage_ref:
            raise MediaObjectNotFound("media object is missing")

        import httpx

        object_url = self._object_url(storage_ref)
        # private_download_url embeds a short-lived signed token in the
        # query string — the resulting signed_url is deliberately never
        # logged, returned, or included in any exception raised below.
        signed_url = self._auth.private_download_url(
            object_url, expires=_INTERNAL_FETCH_URL_EXPIRY_SECONDS
        )
        try:
            resp = httpx.get(signed_url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise MediaStorageUnavailable(
                _sanitized(exc, "qiniu download request failed")
            ) from exc

        if resp.status_code == 404:
            raise MediaObjectNotFound("media object is missing")
        if resp.status_code != 200:
            raise MediaStorageUnavailable(f"qiniu download failed (status={resp.status_code})")
        return resp.content

    def exists(self, storage_ref: Optional[str]) -> bool:
        """Return True/False only for a *confirmed* stat result. Any
        ambiguous outcome (network timeout, auth failure, bucket error, SDK
        failure) raises MediaStorageUnavailable rather than being reported
        as "does not exist" — collapsing a provider outage into a false
        "missing" is exactly the RND-174 QA finding this fixes."""
        if not storage_ref:
            return False
        try:
            _ret, info = self._bucket_manager.stat(self._bucket, storage_ref)
        except Exception as exc:  # noqa: BLE001
            raise MediaStorageUnavailable(_sanitized(exc, "qiniu stat failed")) from exc
        if info.ok():
            return True
        if info.status_code == _QINIU_NOT_FOUND_STATUS:
            return False
        raise MediaStorageUnavailable(f"qiniu stat failed (status={info.status_code})")

    def delete(self, storage_ref: Optional[str]) -> bool:
        """Best-effort delete — never raises, mirrors LocalStorageProvider.
        Deletion is a cleanup operation, not a read the caller branches on,
        so swallowing failures here (unlike exists()/size_bytes()) is
        intentional; callers that need to know cleanup failed should log
        the False return, not rely on an exception."""
        if not storage_ref:
            return False
        try:
            _ret, info = self._bucket_manager.delete(self._bucket, storage_ref)
        except Exception:  # noqa: BLE001
            return False
        return info.ok()

    def size_bytes(self, storage_ref: str) -> int:
        try:
            ret, info = self._bucket_manager.stat(self._bucket, storage_ref)
        except Exception as exc:  # noqa: BLE001
            raise MediaStorageUnavailable(_sanitized(exc, "qiniu stat failed")) from exc
        if info.ok() and ret:
            return int(ret.get("fsize", 0))
        if info.status_code == _QINIU_NOT_FOUND_STATUS:
            raise MediaObjectNotFound("media object is missing")
        raise MediaStorageUnavailable(f"qiniu stat failed (status={info.status_code})")

    def get_download_url(self, storage_ref: str, expires_in: Optional[int] = None) -> Optional[str]:
        """Return a short-lived, browser-usable signed URL for storage_ref
        (RND-187), built from the same validated HTTPS base URL and the
        official SDK's Auth.private_download_url — never a hand-rolled
        signature.

        expires_in is required (seconds) — TTL policy (bounds, default)
        lives one layer up in app.media_storage.get_signed_url_ttl_seconds(),
        not here; this method is deliberately TTL-policy-agnostic and just
        signs for whatever duration the caller, which has already completed
        tenant/permission authorization, asks for.

        Raises MediaObjectNotFound for an empty storage_ref,
        MediaStorageConfigurationError if expires_in is missing, and
        MediaStorageOperationError (sanitized — never the raw SDK exception
        text, which could embed the object URL) if the SDK call itself
        fails. The returned URL is never logged by this method.
        """
        if not storage_ref:
            raise MediaObjectNotFound("media object is missing")
        if not expires_in:
            raise MediaStorageConfigurationError(
                "expires_in is required to generate a signed download URL"
            )

        object_url = self._object_url(storage_ref)
        try:
            return self._auth.private_download_url(object_url, expires=expires_in)
        except Exception as exc:  # noqa: BLE001 - never re-raised raw (may embed the URL)
            raise MediaStorageOperationError(
                _sanitized(exc, "qiniu signed url generation failed")
            ) from exc

    def supports_local_path(self) -> bool:
        return False

    def get_local_path(self, storage_ref: Optional[str]) -> Optional[Path]:
        return None

    @property
    def bucket(self) -> Optional[str]:
        """The bucket this provider instance was constructed with (RND-186)
        — read by a caller (the migration tool) that wants to persist which
        bucket an upload actually went to, sourced directly from this
        provider rather than re-reading QINIU_BUCKET independently, so it
        can never drift from what was actually used for the upload."""
        return self._bucket
