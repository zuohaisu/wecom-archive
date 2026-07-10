"""
Tests for RND-174 — provider factory selection (local vs qiniu_kodo) and
the tenant-aware object-key builder.

Scope: app/media_storage.py's get_media_storage_provider() and
build_tenant_media_key(). Never calls the real Qiniu service.

Run (from backend/):
    pytest tests/test_qiniu_provider_factory.py -v
"""

from __future__ import annotations

import sys

import pytest

_QINIU_ENV_VARS = [
    "QINIU_ACCESS_KEY",
    "QINIU_SECRET_KEY",
    "QINIU_BUCKET",
    "QINIU_REGION",
    "QINIU_DOMAIN",
    "QINIU_TIMEOUT_SECONDS",
]


def _clear_qiniu_env(monkeypatch) -> None:
    for name in _QINIU_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_valid_qiniu_env(monkeypatch) -> None:
    monkeypatch.setenv("QINIU_ACCESS_KEY", "fake-ak")
    monkeypatch.setenv("QINIU_SECRET_KEY", "fake-sk")
    monkeypatch.setenv("QINIU_BUCKET", "test-bucket")
    monkeypatch.setenv("QINIU_DOMAIN", "https://cdn.example.com")


# ---------------------------------------------------------------------------
# Provider factory — selection
# ---------------------------------------------------------------------------


def test_factory_local_returns_local_storage_provider(tmp_path, monkeypatch) -> None:
    from app.media_storage import LocalStorageProvider, get_media_storage_provider

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "local")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))

    provider = get_media_storage_provider()
    assert isinstance(provider, LocalStorageProvider)


def test_factory_qiniu_kodo_returns_qiniu_storage_provider(monkeypatch) -> None:
    from app.media_storage import get_media_storage_provider
    from app.qiniu_storage import QiniuStorageProvider

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "qiniu_kodo")
    _set_valid_qiniu_env(monkeypatch)

    provider = get_media_storage_provider()
    assert isinstance(provider, QiniuStorageProvider)
    assert provider.supports_local_path() is False


def test_factory_unknown_provider_raises_clear_error(monkeypatch) -> None:
    from app.media_storage import UnsupportedMediaStorageProvider, get_media_storage_provider

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "totally-not-a-provider")

    with pytest.raises(UnsupportedMediaStorageProvider) as exc_info:
        get_media_storage_provider()
    assert "totally-not-a-provider" in str(exc_info.value)


@pytest.mark.parametrize(
    "missing_var",
    ["QINIU_ACCESS_KEY", "QINIU_SECRET_KEY", "QINIU_BUCKET", "QINIU_DOMAIN"],
)
def test_factory_qiniu_kodo_missing_required_config_fails_clearly(
    missing_var, monkeypatch
) -> None:
    from app.media_storage import get_media_storage_provider
    from app.qiniu_storage import QiniuConfigurationError

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "qiniu_kodo")
    _set_valid_qiniu_env(monkeypatch)
    monkeypatch.delenv(missing_var, raising=False)

    with pytest.raises(QiniuConfigurationError) as exc_info:
        get_media_storage_provider()
    assert missing_var in str(exc_info.value)


def test_factory_qiniu_kodo_missing_config_does_not_fall_back_to_local(monkeypatch, tmp_path) -> None:
    """A misconfigured qiniu_kodo selection must fail loudly, never silently
    hand back a LocalStorageProvider instead."""
    from app.media_storage import get_media_storage_provider
    from app.qiniu_storage import QiniuConfigurationError

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "qiniu_kodo")
    _clear_qiniu_env(monkeypatch)
    # Even with a perfectly valid local root configured, qiniu_kodo must
    # still fail rather than quietly resolving to LocalStorageProvider.
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))

    with pytest.raises(QiniuConfigurationError):
        get_media_storage_provider()


def test_factory_provider_name_is_case_and_whitespace_insensitive(monkeypatch) -> None:
    from app.media_storage import get_media_storage_provider
    from app.qiniu_storage import QiniuStorageProvider

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "  Qiniu_Kodo  ")
    _set_valid_qiniu_env(monkeypatch)

    provider = get_media_storage_provider()
    assert isinstance(provider, QiniuStorageProvider)


def test_local_mode_does_not_import_qiniu_package(monkeypatch, tmp_path) -> None:
    """Importing/running in local mode must not require the qiniu package to
    be importable — simulate qiniu being unavailable and prove local
    provider construction still succeeds."""
    from app import media_storage

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "local")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    monkeypatch.setitem(sys.modules, "qiniu", None)  # "qiniu" not installed

    provider = media_storage.get_media_storage_provider()
    assert isinstance(provider, media_storage.LocalStorageProvider)


def test_qiniu_region_optional(monkeypatch) -> None:
    from app.media_storage import get_media_storage_provider
    from app.qiniu_storage import QiniuStorageProvider

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "qiniu_kodo")
    _set_valid_qiniu_env(monkeypatch)
    monkeypatch.delenv("QINIU_REGION", raising=False)

    provider = get_media_storage_provider()
    assert isinstance(provider, QiniuStorageProvider)


# ---------------------------------------------------------------------------
# build_tenant_media_key — tenant-aware, traversal-safe object key builder
# ---------------------------------------------------------------------------


def test_object_key_includes_tenant_id() -> None:
    from app.media_storage import build_tenant_media_key

    key = build_tenant_media_key("tenant-a", "images", "42")
    assert key.startswith("tenants/tenant-a/")


def test_object_key_includes_identity() -> None:
    from app.media_storage import build_tenant_media_key

    key = build_tenant_media_key("tenant-a", "images", "42")
    assert "42" in key


def test_object_key_tenant_a_and_tenant_b_have_different_prefixes() -> None:
    from app.media_storage import build_tenant_media_key

    key_a = build_tenant_media_key("tenant-a", "images", "42")
    key_b = build_tenant_media_key("tenant-b", "images", "42")
    assert key_a != key_b
    assert "tenant-a" in key_a and "tenant-a" not in key_b
    assert "tenant-b" in key_b and "tenant-b" not in key_a


def test_object_key_repeated_calls_are_stable() -> None:
    """Same inputs must always produce the same key so retried/idempotent
    processing overwrites in place rather than accumulating duplicates."""
    from app.media_storage import build_tenant_media_key

    key1 = build_tenant_media_key("tenant-a", "images", "42")
    key2 = build_tenant_media_key("tenant-a", "images", "42")
    assert key1 == key2


def test_object_key_suffix_is_appended() -> None:
    from app.media_storage import build_tenant_media_key

    key = build_tenant_media_key("tenant-a", "images", "42", suffix=".part")
    assert key.endswith("42.part")


@pytest.mark.parametrize(
    "unsafe_tenant_id",
    [
        "../../etc/passwd",
        "tenant/../../escape",
        "tenant\\..\\escape",
        "..",
        "/absolute/path",
    ],
)
def test_object_key_sanitizes_traversal_in_tenant_id(unsafe_tenant_id) -> None:
    """No path *segment* (a '/'-delimited component of the key) may equal
    '..' or be empty — that is the actual traversal-safety property. A
    literal '..' substring embedded inside a single sanitized segment (e.g.
    the slashes in 'tenant/../../escape' having been collapsed to '_') is
    inert: it can never be interpreted as a parent-directory reference
    because it is not its own path component."""
    from app.media_storage import build_tenant_media_key

    key = build_tenant_media_key(unsafe_tenant_id, "images", "42")
    segments = key.split("/")
    assert ".." not in segments
    assert "" not in segments
    assert "." not in segments
    # Must stay confined under the fixed tenants/ prefix segment.
    assert key.startswith("tenants/")


@pytest.mark.parametrize(
    "unsafe_identifier",
    ["../../../etc/passwd", "1/../../2", "1;rm -rf", "1 2 3"],
)
def test_object_key_sanitizes_unsafe_identifier_segments(unsafe_identifier) -> None:
    from app.media_storage import build_tenant_media_key

    key = build_tenant_media_key("tenant-a", "images", unsafe_identifier)
    segments = key.split("/")
    assert ".." not in segments
    assert "" not in segments
    # No traversal or shell-metacharacter segment should survive verbatim.
    assert unsafe_identifier not in segments


def test_object_key_never_contains_credential_like_substrings() -> None:
    from app.media_storage import build_tenant_media_key

    key = build_tenant_media_key("tenant-a", "images", "42")
    assert "QINIU_SECRET_KEY" not in key
    assert "access_key" not in key.lower()
