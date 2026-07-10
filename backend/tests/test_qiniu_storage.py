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

from types import SimpleNamespace

import pytest

from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageOperationError,
    MediaStorageUnavailable,
)
from app.qiniu_storage import QiniuConfigurationError, QiniuStorageProvider

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


def test_get_download_url_always_none() -> None:
    """RND-174 scope: no signed/public URL delivery — media stays behind
    the existing authenticated backend route."""
    provider = _make_provider()
    assert provider.get_download_url("tenants/t1/images/1.jpg") is None


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
