"""
Tests for RND-174 — QiniuStorageProvider.

Scope: app/qiniu_storage.py. Never calls the real Qiniu service — every
test mocks at the SDK boundary (qiniu.put_data, BucketManager.stat/
delete/move, Auth.private_download_url, httpx.get). Also proves secret
values and raw SDK error bodies never leak into raised exceptions.

Run (from backend/):
    pytest tests/test_qiniu_storage.py -v
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageUnavailable,
)
from app.qiniu_storage import (
    QiniuConfigurationError,
    QiniuStorageProvider,
    redact_signed_url_for_log,
)

_FAKE_ACCESS_KEY = "fake-access-key-should-never-leak"
_FAKE_SECRET_KEY = "fake-secret-key-should-never-leak"
_FAKE_DOMAIN = "https://cdn.example.com"


class _FakeInfo:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def ok(self) -> bool:
        return self.status_code // 100 == 2


def _make_provider(**overrides) -> QiniuStorageProvider:
    kwargs = dict(
        access_key=_FAKE_ACCESS_KEY,
        secret_key=_FAKE_SECRET_KEY,
        bucket="test-bucket",
        domain=_FAKE_DOMAIN,
    )
    kwargs.update(overrides)
    return QiniuStorageProvider(**kwargs)


# ---------------------------------------------------------------------------
# Construction / configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["access_key", "secret_key", "bucket", "domain"]
)
def test_missing_required_field_raises_configuration_error(field) -> None:
    kwargs = dict(
        access_key=_FAKE_ACCESS_KEY,
        secret_key=_FAKE_SECRET_KEY,
        bucket="test-bucket",
        domain=_FAKE_DOMAIN,
    )
    kwargs[field] = ""
    with pytest.raises(QiniuConfigurationError):
        QiniuStorageProvider(**kwargs)


def test_supports_local_path_is_false() -> None:
    provider = _make_provider()
    assert provider.supports_local_path() is False


def test_get_local_path_always_none() -> None:
    provider = _make_provider()
    assert provider.get_local_path("tenants/t1/images/1.jpg") is None
    assert provider.get_local_path(None) is None


def test_get_download_url_none_without_expires_in() -> None:
    """The provider is deliberately TTL-policy-agnostic: expires_in is
    required, no implicit default. TTL bounds/defaulting live one layer up
    in app.media_storage.get_signed_url_ttl_seconds()."""
    provider = _make_provider()
    with pytest.raises(MediaStorageConfigurationError):
        provider.get_download_url("tenants/t1/images/1.jpg")


def test_get_download_url_empty_ref_raises_object_not_found() -> None:
    provider = _make_provider()
    with pytest.raises(MediaObjectNotFound):
        provider.get_download_url("", expires_in=900)
    with pytest.raises(MediaObjectNotFound):
        provider.get_download_url(None, expires_in=900)


# ---------------------------------------------------------------------------
# get_download_url (RND-187 — client-facing signed URL generation)
# ---------------------------------------------------------------------------


def test_get_download_url_success_uses_media_example_domain(monkeypatch) -> None:
    provider = _make_provider(domain="https://media.example.com")
    seen = {}

    def _fake_private_download_url(url, expires=3600):
        seen["url"] = url
        seen["expires"] = expires
        return "https://media.example.com/tenants/t1/images/1.jpg?e=1234567890&token=fake-token"

    monkeypatch.setattr(provider._auth, "private_download_url", _fake_private_download_url)

    url = provider.get_download_url("tenants/t1/images/1.jpg", expires_in=900)

    assert seen["url"] == "https://media.example.com/tenants/t1/images/1.jpg"
    assert url.startswith("https://media.example.com/")
    assert "token=" in url


def test_get_download_url_passes_expires_in_through_as_ttl(monkeypatch) -> None:
    """TTL correctness: the exact expires_in the caller supplies must be
    forwarded to the SDK's expires= parameter unchanged — this is what
    determines the URL's real expiry, not a provider-side default."""
    provider = _make_provider()
    seen = {}

    def _fake_private_download_url(url, expires=3600):
        seen["expires"] = expires
        return "https://cdn.example.com/signed?e=999&token=abc"

    monkeypatch.setattr(provider._auth, "private_download_url", _fake_private_download_url)

    provider.get_download_url("tenants/t1/images/1.jpg", expires_in=123)
    assert seen["expires"] == 123

    provider.get_download_url("tenants/t1/images/1.jpg", expires_in=3600)
    assert seen["expires"] == 3600


def test_get_download_url_contains_expiry_and_token_query_params(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._auth,
        "private_download_url",
        lambda url, expires=3600: "https://cdn.example.com/tenants/t1/images/1.jpg?e=1735689600&token=signed-token-value",
    )

    url = provider.get_download_url("tenants/t1/images/1.jpg", expires_in=900)

    assert "e=1735689600" in url
    assert "token=signed-token-value" in url


def test_get_download_url_never_contains_ak_or_sk(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._auth,
        "private_download_url",
        lambda url, expires=3600: "https://cdn.example.com/tenants/t1/images/1.jpg?e=1&token=abc123",
    )

    url = provider.get_download_url("tenants/t1/images/1.jpg", expires_in=900)

    assert _FAKE_ACCESS_KEY not in url
    assert _FAKE_SECRET_KEY not in url


def test_get_download_url_single_object_key_only(monkeypatch) -> None:
    """The signed URL must be scoped to exactly the object_url built from
    this single storage_ref — never a bucket-wide or wildcard URL."""
    provider = _make_provider()
    seen = {}
    monkeypatch.setattr(
        provider._auth,
        "private_download_url",
        lambda url, expires=3600: seen.setdefault("url", url) or f"{url}?e=1&token=abc",
    )

    provider.get_download_url("tenants/t1/images/1.jpg", expires_in=900)

    assert seen["url"] == "https://cdn.example.com/tenants/t1/images/1.jpg"


def test_get_download_url_sdk_exception_sanitized(monkeypatch) -> None:
    """An SDK/signing exception must not leak secrets or raw URLs into the
    raised error."""
    provider = _make_provider()

    def _raise(url, expires=3600):
        raise RuntimeError(f"signing failed for url={url} secret={_FAKE_SECRET_KEY}")

    monkeypatch.setattr(provider._auth, "private_download_url", _raise)

    with pytest.raises(MediaStorageOperationError) as exc_info:
        provider.get_download_url("tenants/t1/images/1.jpg", expires_in=900)

    assert _FAKE_SECRET_KEY not in str(exc_info.value)
    assert _FAKE_ACCESS_KEY not in str(exc_info.value)


def test_get_download_url_object_key_cannot_alter_host(monkeypatch) -> None:
    """A storage_ref containing path-traversal-like or otherwise unusual
    segments must never change the URL's scheme/host away from the
    validated HTTPS base — same safety property _object_url already
    guarantees for read_bytes() (proven there by
    test_read_bytes_passes_https_object_url_to_private_download_url)."""
    provider = _make_provider()
    seen = {}
    monkeypatch.setattr(
        provider._auth,
        "private_download_url",
        lambda url, expires=3600: seen.setdefault("url", url) or f"{url}?e=1&token=abc",
    )

    provider.get_download_url("../../etc/passwd", expires_in=900)

    parsed = urlsplit(seen["url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "cdn.example.com"


# ---------------------------------------------------------------------------
# redact_signed_url_for_log (RND-187 — log/exception redaction backstop)
# ---------------------------------------------------------------------------


def test_redact_signed_url_for_log_replaces_query_values() -> None:
    url = "https://media.example.com/tenants/t1/images/1.jpg?e=1735689600&token=super-secret-token"
    redacted = redact_signed_url_for_log(url)

    assert "super-secret-token" not in redacted
    assert "1735689600" not in redacted
    assert "token=[REDACTED]" in redacted
    assert "e=[REDACTED]" in redacted
    assert redacted.startswith("https://media.example.com/tenants/t1/images/1.jpg?")


def test_redact_signed_url_for_log_handles_no_query() -> None:
    url = "https://media.example.com/tenants/t1/images/1.jpg"
    assert redact_signed_url_for_log(url) == url


def test_redact_signed_url_for_log_handles_empty() -> None:
    assert redact_signed_url_for_log(None) == ""
    assert redact_signed_url_for_log("") == ""


# ---------------------------------------------------------------------------
# save_bytes (upload)
# ---------------------------------------------------------------------------


def test_save_bytes_success(monkeypatch) -> None:
    provider = _make_provider()

    calls = {}

    def _fake_put_data(up_token, key, data, mime_type=None, **kwargs):
        calls["up_token"] = up_token
        calls["key"] = key
        calls["data"] = data
        calls["mime_type"] = mime_type
        return {"key": key}, _FakeInfo(200)

    monkeypatch.setattr(provider._qiniu, "put_data", _fake_put_data)

    result = provider.save_bytes("tenants/t1/images/1.part", b"image-bytes")

    assert result == "tenants/t1/images/1.part"
    assert calls["key"] == "tenants/t1/images/1.part"
    assert calls["data"] == b"image-bytes"


# ---------------------------------------------------------------------------
# RND-186 QA fix: save_bytes() must set the correct Content-Type in
# Qiniu's own object metadata for every supported media category — not
# just images. Before this fix, save_bytes() called
# detect_image_content_type_for_ref() (image-only), so any video/voice/
# file upload's Content-Type silently degraded to
# "application/octet-stream" even though media_files.mime_type had
# already recorded the correct value in the database — the two disagreed.
# These tests assert on the exact mime_type argument passed to the Qiniu
# SDK's put_data() call, i.e. what actually becomes the object's
# Content-Type in Qiniu.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "storage_ref,expected_content_type",
    [
        ("tenants/t1/images/1.jpg", "image/jpeg"),
        ("tenants/t1/images/2.png", "image/png"),
        ("tenants/t1/images/3.gif", "image/gif"),
        ("tenants/t1/images/4.webp", "image/webp"),
        ("tenants/t1/videos/5.mp4", "video/mp4"),
        ("tenants/t1/voice/6.amr", "audio/amr"),
        ("tenants/t1/voice/7.silk", "audio/silk"),
        ("tenants/t1/voice/8.wav", "audio/wav"),
        ("tenants/t1/voice/9.mp3", "audio/mpeg"),
        ("tenants/t1/files/10.pdf", "application/pdf"),
        ("tenants/t1/files/11.zip", "application/zip"),
    ],
)
def test_save_bytes_sets_correct_content_type_per_media_category(
    monkeypatch, storage_ref, expected_content_type
) -> None:
    provider = _make_provider()
    calls = {}

    def _fake_put_data(up_token, key, data, mime_type=None, **kwargs):
        calls["mime_type"] = mime_type
        return {"key": key}, _FakeInfo(200)

    monkeypatch.setattr(provider._qiniu, "put_data", _fake_put_data)

    provider.save_bytes(storage_ref, b"payload")

    assert calls["mime_type"] == expected_content_type


def test_save_bytes_unrecognized_extension_falls_back_to_octet_stream(monkeypatch) -> None:
    """An extension outside every known allow-list (image/video/voice/
    file) must still fall back to "application/octet-stream" — never a
    guess, and never a crash — matching the pre-existing fallback
    behavior for a genuinely unrecognized object key."""
    provider = _make_provider()
    calls = {}

    def _fake_put_data(up_token, key, data, mime_type=None, **kwargs):
        calls["mime_type"] = mime_type
        return {"key": key}, _FakeInfo(200)

    monkeypatch.setattr(provider._qiniu, "put_data", _fake_put_data)

    provider.save_bytes("tenants/t1/images/1.part", b"payload")  # .part: no allow-listed ext

    assert calls["mime_type"] == "application/octet-stream"


def test_save_bytes_image_content_type_unchanged_by_generalization(monkeypatch) -> None:
    """Regression proof: every image extension resolves to the exact same
    Content-Type as before this fix — the image entries in the combined
    lookup table are the unmodified _ALLOWED_IMAGE_CONTENT_TYPES table."""
    from app.media_storage import detect_image_content_type_for_ref

    provider = _make_provider()
    calls = {}

    def _fake_put_data(up_token, key, data, mime_type=None, **kwargs):
        calls["mime_type"] = mime_type
        return {"key": key}, _FakeInfo(200)

    monkeypatch.setattr(provider._qiniu, "put_data", _fake_put_data)

    for ref in [
        "tenants/t1/images/1.jpg",
        "tenants/t1/images/2.png",
        "tenants/t1/images/3.gif",
        "tenants/t1/images/4.webp",
    ]:
        provider.save_bytes(ref, b"payload")
        assert calls["mime_type"] == detect_image_content_type_for_ref(ref)


def test_save_bytes_failure_raises_operation_error(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._qiniu, "put_data", lambda *a, **k: (None, _FakeInfo(579))
    )

    with pytest.raises(MediaStorageOperationError) as exc_info:
        provider.save_bytes("tenants/t1/images/1.part", b"data")

    assert _FAKE_SECRET_KEY not in str(exc_info.value)
    assert _FAKE_ACCESS_KEY not in str(exc_info.value)


def test_save_bytes_network_exception_sanitized(monkeypatch) -> None:
    """An underlying network exception must not leak its raw text (which
    could embed request URLs or the upload token) into the raised error."""
    provider = _make_provider()

    def _raise(*_a, **_k):
        raise ConnectionError(f"connect failed for secret={_FAKE_SECRET_KEY}")

    monkeypatch.setattr(provider._qiniu, "put_data", _raise)

    with pytest.raises(MediaStorageOperationError) as exc_info:
        provider.save_bytes("tenants/t1/images/1.part", b"data")

    assert _FAKE_SECRET_KEY not in str(exc_info.value)


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


def test_exists_true(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager,
        "stat",
        lambda bucket, key: ({"fsize": 10}, _FakeInfo(200)),
    )
    assert provider.exists("tenants/t1/images/1.jpg") is True


def test_exists_false_when_not_found(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager, "stat", lambda bucket, key: (None, _FakeInfo(612))
    )
    assert provider.exists("tenants/t1/images/missing.jpg") is False


def test_exists_false_for_empty_ref() -> None:
    provider = _make_provider()
    assert provider.exists(None) is False
    assert provider.exists("") is False


def test_exists_raises_unavailable_on_unexpected_exception(monkeypatch) -> None:
    """RND-174 QA fix: an unconfirmed stat failure (network/SDK exception)
    must raise MediaStorageUnavailable, never silently return False —
    collapsing "the provider is down" into "the object is missing" is the
    bug this test guards against."""
    provider = _make_provider()

    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(provider._bucket_manager, "stat", _raise)
    with pytest.raises(MediaStorageUnavailable):
        provider.exists("tenants/t1/images/1.jpg")


def test_exists_raises_unavailable_on_ambiguous_status(monkeypatch) -> None:
    """A stat status that is neither 2xx (confirmed exists) nor 612
    (confirmed not found) — e.g. an auth or bucket error — must also raise,
    not be silently treated as "missing"."""
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager, "stat", lambda bucket, key: (None, _FakeInfo(401))
    )
    with pytest.raises(MediaStorageUnavailable):
        provider.exists("tenants/t1/images/1.jpg")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_success(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager, "delete", lambda bucket, key: (None, _FakeInfo(200))
    )
    assert provider.delete("tenants/t1/images/1.jpg") is True


def test_delete_returns_false_when_missing(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager, "delete", lambda bucket, key: (None, _FakeInfo(612))
    )
    assert provider.delete("tenants/t1/images/missing.jpg") is False


def test_delete_never_raises_on_exception(monkeypatch) -> None:
    """delete() is documented as best-effort — never raises."""
    provider = _make_provider()

    def _raise(*_a, **_k):
        raise RuntimeError("network blip")

    monkeypatch.setattr(provider._bucket_manager, "delete", _raise)
    assert provider.delete("tenants/t1/images/1.jpg") is False


def test_delete_false_for_empty_ref() -> None:
    provider = _make_provider()
    assert provider.delete(None) is False


# ---------------------------------------------------------------------------
# size_bytes
# ---------------------------------------------------------------------------


def test_size_bytes_success(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager,
        "stat",
        lambda bucket, key: ({"fsize": 12345}, _FakeInfo(200)),
    )
    assert provider.size_bytes("tenants/t1/images/1.jpg") == 12345


def test_size_bytes_missing_raises_object_not_found(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager, "stat", lambda bucket, key: (None, _FakeInfo(612))
    )
    with pytest.raises(MediaObjectNotFound):
        provider.size_bytes("tenants/t1/images/missing.jpg")


def test_size_bytes_other_error_raises_unavailable(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager, "stat", lambda bucket, key: (None, _FakeInfo(500))
    )
    with pytest.raises(MediaStorageUnavailable):
        provider.size_bytes("tenants/t1/images/1.jpg")


# ---------------------------------------------------------------------------
# replace (cloud-safe publish: server-side move, not download+reupload)
# ---------------------------------------------------------------------------


def test_replace_success_moves_part_to_final(monkeypatch) -> None:
    provider = _make_provider()
    calls = {}

    def _fake_move(bucket, key, bucket_to, key_to, force="false"):
        calls["args"] = (bucket, key, bucket_to, key_to, force)
        return None, _FakeInfo(200)

    monkeypatch.setattr(provider._bucket_manager, "move", _fake_move)

    result = provider.replace("tenants/t1/images/1.part", "tenants/t1/images/1.jpg")

    assert result == "tenants/t1/images/1.jpg"
    assert calls["args"][1] == "tenants/t1/images/1.part"
    assert calls["args"][3] == "tenants/t1/images/1.jpg"
    assert calls["args"][4] == "true"


def test_replace_missing_source_raises_object_not_found(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager,
        "move",
        lambda *a, **k: (None, _FakeInfo(612)),
    )
    with pytest.raises(MediaObjectNotFound):
        provider.replace("tenants/t1/images/missing.part", "tenants/t1/images/1.jpg")


def test_replace_other_error_raises_operation_error(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._bucket_manager, "move", lambda *a, **k: (None, _FakeInfo(579))
    )
    with pytest.raises(MediaStorageOperationError):
        provider.replace("tenants/t1/images/1.part", "tenants/t1/images/1.jpg")


# ---------------------------------------------------------------------------
# read_bytes (controlled server-side fetch via short-lived private URL)
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, status_code: int, content: bytes = b""):
        self.status_code = status_code
        self.content = content


def test_read_bytes_success(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._auth,
        "private_download_url",
        lambda url, expires=3600: "https://cdn.example.com/signed?token=abc",
    )

    import httpx

    monkeypatch.setattr(
        httpx, "get", lambda url, timeout=None: _FakeHttpResponse(200, b"image-bytes")
    )

    data = provider.read_bytes("tenants/t1/images/1.jpg")
    assert data == b"image-bytes"


def test_read_bytes_passes_https_object_url_to_private_download_url(monkeypatch) -> None:
    """The URL handed to Auth.private_download_url (before signing) must be
    built from the validated HTTPS base — never the old hardcoded
    'http://' — and must not be malformed by naive string concatenation."""
    provider = _make_provider()
    seen = {}

    def _fake_private_download_url(url, expires=3600):
        seen["url"] = url
        return "https://cdn.example.com/signed?token=abc"

    monkeypatch.setattr(provider._auth, "private_download_url", _fake_private_download_url)

    import httpx

    monkeypatch.setattr(
        httpx, "get", lambda url, timeout=None: _FakeHttpResponse(200, b"image-bytes")
    )

    provider.read_bytes("tenants/t1/images/1.jpg")

    assert seen["url"] == "https://cdn.example.com/tenants/t1/images/1.jpg"
    assert seen["url"].startswith("https://")
    assert "http://" not in seen["url"]


def test_read_bytes_not_found_raises_object_not_found(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._auth, "private_download_url", lambda url, expires=3600: "https://x/y"
    )

    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _FakeHttpResponse(404))

    with pytest.raises(MediaObjectNotFound):
        provider.read_bytes("tenants/t1/images/missing.jpg")


def test_read_bytes_empty_ref_raises_object_not_found() -> None:
    provider = _make_provider()
    with pytest.raises(MediaObjectNotFound):
        provider.read_bytes("")


def test_read_bytes_other_status_raises_unavailable(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._auth, "private_download_url", lambda url, expires=3600: "https://x/y"
    )

    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _FakeHttpResponse(500))

    with pytest.raises(MediaStorageUnavailable):
        provider.read_bytes("tenants/t1/images/1.jpg")


def test_read_bytes_network_error_sanitized(monkeypatch) -> None:
    provider = _make_provider()
    monkeypatch.setattr(
        provider._auth,
        "private_download_url",
        lambda url, expires=3600: f"https://x/y?token={_FAKE_SECRET_KEY}",
    )

    import httpx

    def _raise(url, timeout=None):
        raise httpx.ConnectError(f"failed for url={url}")

    monkeypatch.setattr(httpx, "get", _raise)

    with pytest.raises(MediaStorageUnavailable) as exc_info:
        provider.read_bytes("tenants/t1/images/1.jpg")

    assert _FAKE_SECRET_KEY not in str(exc_info.value)


def test_read_bytes_signed_url_never_returned_or_included_in_error(monkeypatch) -> None:
    """The short-lived signed URL used internally to fetch bytes must never
    surface in a return value or an error message."""
    provider = _make_provider()
    signed_url = f"https://cdn.example.com/tenants/t1/images/1.jpg?e=1&token={_FAKE_SECRET_KEY}"
    monkeypatch.setattr(
        provider._auth, "private_download_url", lambda url, expires=3600: signed_url
    )

    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _FakeHttpResponse(500))

    with pytest.raises(MediaStorageUnavailable) as exc_info:
        provider.read_bytes("tenants/t1/images/1.jpg")

    assert signed_url not in str(exc_info.value)
    assert _FAKE_SECRET_KEY not in str(exc_info.value)


# ---------------------------------------------------------------------------
# File streaming (RND-360 multi-gigabyte ZIP path)
# ---------------------------------------------------------------------------


class _FakeHttpStreamResponse:
    def __init__(self, status_code: int, chunks: tuple[bytes, ...] = ()):
        self.status_code = status_code
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        yield from self._chunks


def test_save_file_uploads_from_disk_without_reading_into_memory(
    tmp_path: Path, monkeypatch
) -> None:
    provider = _make_provider()
    source = tmp_path / "export.zip"
    source.write_bytes(b"PK-export")
    seen = {}
    monkeypatch.setattr(
        provider._auth,
        "upload_token",
        lambda bucket, key, expires=3600: "upload-token",
    )

    def put_file(token, key, path, **kwargs):
        seen.update(token=token, key=key, path=path, kwargs=kwargs)
        return {}, _FakeInfo(200)

    monkeypatch.setattr(provider._qiniu, "put_file", put_file)

    ref = provider.save_file("tenants/t1/exports/job.zip", source)

    assert ref == "tenants/t1/exports/job.zip"
    assert seen["path"] == str(source)
    assert seen["kwargs"]["mime_type"] == "application/zip"
    assert seen["kwargs"]["check_crc"] is True


def test_copy_to_file_streams_chunks_to_disk(tmp_path: Path, monkeypatch) -> None:
    provider = _make_provider()
    destination = tmp_path / "download" / "object.bin"
    monkeypatch.setattr(
        provider._auth,
        "private_download_url",
        lambda url, expires=3600: "https://cdn.example.com/signed?token=abc",
    )

    import httpx

    monkeypatch.setattr(
        httpx,
        "stream",
        lambda method, url, timeout=None: _FakeHttpStreamResponse(
            200, (b"first", b"-second")
        ),
    )

    written = provider.copy_to_file("tenants/t1/images/1.jpg", destination)

    assert written == len(b"first-second")
    assert destination.read_bytes() == b"first-second"


def test_copy_to_file_signing_failure_is_sanitized(
    tmp_path: Path, monkeypatch
) -> None:
    provider = _make_provider()

    def raise_with_secret(*_args, **_kwargs):
        raise RuntimeError(f"secret={_FAKE_SECRET_KEY}")

    monkeypatch.setattr(provider._auth, "private_download_url", raise_with_secret)

    with pytest.raises(MediaStorageOperationError) as exc_info:
        provider.copy_to_file("tenants/t1/images/1.jpg", tmp_path / "object.bin")

    assert _FAKE_SECRET_KEY not in str(exc_info.value)
