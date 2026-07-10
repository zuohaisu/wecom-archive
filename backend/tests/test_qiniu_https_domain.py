"""
Tests for RND-174 QA remediation — QINIU_DOMAIN must be a validated HTTPS
base URL.

The original implementation hardcoded `f"http://{domain}/..."` regardless
of what QINIU_DOMAIN held, which both downgraded private media retrieval to
plaintext and produced a malformed URL ("http://https://host/...") if an
operator supplied the documented https:// form. This file exercises
app.qiniu_storage.normalize_qiniu_https_base_url() directly, plus proves
QiniuStorageProvider actually uses the validated base when building the
object URL it hands to Auth.private_download_url.

Run (from backend/):
    pytest tests/test_qiniu_https_domain.py -v
"""

from __future__ import annotations

import pytest

from app.qiniu_storage import (
    QiniuConfigurationError,
    QiniuStorageProvider,
    normalize_qiniu_https_base_url,
)

_FAKE_ACCESS_KEY = "fake-ak"
_FAKE_SECRET_KEY = "fake-sk"


# ---------------------------------------------------------------------------
# normalize_qiniu_https_base_url — direct unit coverage
# ---------------------------------------------------------------------------


def test_valid_https_domain_accepted() -> None:
    assert normalize_qiniu_https_base_url("https://private-media.example.com") == (
        "https://private-media.example.com"
    )


def test_trailing_slash_normalized() -> None:
    assert normalize_qiniu_https_base_url("https://private-media.example.com/") == (
        "https://private-media.example.com"
    )


def test_multiple_trailing_slashes_normalized() -> None:
    assert normalize_qiniu_https_base_url("https://private-media.example.com///") == (
        "https://private-media.example.com"
    )


def test_https_with_path_prefix_preserved_and_normalized() -> None:
    assert normalize_qiniu_https_base_url("https://cdn.example.com/media/") == (
        "https://cdn.example.com/media"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "http://private-media.example.com",
        "HTTP://private-media.example.com",
        "http://private-media.example.com/",
    ],
)
def test_http_scheme_rejected(raw) -> None:
    with pytest.raises(QiniuConfigurationError):
        normalize_qiniu_https_base_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "private-media.example.com",  # bare host, no scheme
        "//private-media.example.com",  # scheme-relative
        "private-media.example.com/path",
    ],
)
def test_missing_scheme_rejected(raw) -> None:
    with pytest.raises(QiniuConfigurationError):
        normalize_qiniu_https_base_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://private-media.example.com",
        "https://",
        "https:// private-media.example.com",
        "https://has space.example.com",
        "https://cdn.example.com?token=abc",
        "https://cdn.example.com#frag",
        "not a url at all",
    ],
)
def test_malformed_or_unsupported_domain_rejected(raw) -> None:
    with pytest.raises(QiniuConfigurationError):
        normalize_qiniu_https_base_url(raw)


def test_error_message_never_echoes_raw_input() -> None:
    """The rejection message must describe the shape problem, not repeat
    the operator's raw (possibly secret-laden, e.g. copy-pasted with an
    embedded token) input verbatim."""
    raw = "http://leaked-looking-value?token=should-not-appear"
    with pytest.raises(QiniuConfigurationError) as exc_info:
        normalize_qiniu_https_base_url(raw)
    assert raw not in str(exc_info.value)
    assert "should-not-appear" not in str(exc_info.value)


def test_does_not_downgrade_https_to_http() -> None:
    """A valid https:// value must round-trip as https:// — never silently
    rewritten to http://."""
    result = normalize_qiniu_https_base_url("https://private-media.example.com")
    assert result.startswith("https://")
    assert "http://" not in result


# ---------------------------------------------------------------------------
# QiniuStorageProvider construction — end-to-end domain validation
# ---------------------------------------------------------------------------


def _make_provider(domain: str) -> QiniuStorageProvider:
    return QiniuStorageProvider(
        access_key=_FAKE_ACCESS_KEY,
        secret_key=_FAKE_SECRET_KEY,
        bucket="test-bucket",
        domain=domain,
    )


def test_provider_construction_accepts_valid_https_domain() -> None:
    provider = _make_provider("https://private-media.example.com")
    assert provider._base_url == "https://private-media.example.com"


def test_provider_construction_rejects_http_domain() -> None:
    with pytest.raises(QiniuConfigurationError):
        _make_provider("http://private-media.example.com")


def test_provider_construction_rejects_missing_scheme() -> None:
    with pytest.raises(QiniuConfigurationError):
        _make_provider("private-media.example.com")


def test_object_url_uses_safe_joining_no_double_slash() -> None:
    provider = _make_provider("https://private-media.example.com/")
    url = provider._object_url("tenants/t1/images/1.jpg")
    assert url == "https://private-media.example.com/tenants/t1/images/1.jpg"
    assert "//tenants" not in url


def test_object_url_is_always_https() -> None:
    provider = _make_provider("https://private-media.example.com")
    url = provider._object_url("tenants/t1/images/1.jpg")
    assert url.startswith("https://")


def test_object_url_percent_encodes_unsafe_characters() -> None:
    provider = _make_provider("https://private-media.example.com")
    # Defense in depth: even though build_tenant_media_key already
    # sanitizes keys, _object_url must not construct a broken/ambiguous URL
    # if it is ever handed a ref with a space or other unsafe character.
    url = provider._object_url("tenants/t1/images/weird name.jpg")
    assert " " not in url
    assert url.startswith("https://private-media.example.com/")
