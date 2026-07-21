"""Unit tests for RND-207 signed-URL fixed-window stabilization + thumbnail
object-key derivation.

Covers app.media_storage.get_signed_url_window_seconds /
compute_signed_url_deadline / build_thumbnail_storage_ref, and the deadline
signing path in app.qiniu_storage.QiniuStorageProvider — proving the signed
URL is byte-identical within a window (browser-cache reuse) and changes across
windows, without ever logging or asserting a real signed URL.
"""

from __future__ import annotations

import pytest

from app.media_storage import (
    MediaStorageConfigurationError,
    build_thumbnail_storage_ref,
    compute_signed_url_deadline,
    get_signed_url_window_seconds,
)


def test_window_defaults_to_ttl(monkeypatch) -> None:
    monkeypatch.delenv("MEDIA_SIGNED_URL_WINDOW_SECONDS", raising=False)
    monkeypatch.setenv("MEDIA_SIGNED_URL_TTL_SECONDS", "900")
    assert get_signed_url_window_seconds() == 900


def test_window_explicit_value(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_SIGNED_URL_WINDOW_SECONDS", "300")
    assert get_signed_url_window_seconds() == 300


@pytest.mark.parametrize("bad", ["not-int", "10", "5000", "-1"])
def test_window_invalid_fails_loudly(monkeypatch, bad) -> None:
    monkeypatch.setenv("MEDIA_SIGNED_URL_WINDOW_SECONDS", bad)
    with pytest.raises(MediaStorageConfigurationError):
        get_signed_url_window_seconds()


def test_deadline_is_stable_within_window() -> None:
    window = 900
    base = 1_800_000_000
    # every instant inside [k*w, (k+1)*w) snaps to the SAME deadline
    d0 = compute_signed_url_deadline(base, window_seconds=window)
    d1 = compute_signed_url_deadline(base + 1, window_seconds=window)
    d2 = compute_signed_url_deadline(base + window - 1, window_seconds=window)
    assert d0 == d1 == d2


def test_deadline_changes_across_windows() -> None:
    window = 900
    base = 1_800_000_000
    inside = compute_signed_url_deadline(base, window_seconds=window)
    nextwin = compute_signed_url_deadline(base + window, window_seconds=window)
    assert nextwin == inside + window


def test_deadline_guarantees_at_least_one_window_of_life() -> None:
    window = 900
    for offset in (0, 1, window // 2, window - 1):
        now = 1_800_000_000 + offset
        deadline = compute_signed_url_deadline(now, window_seconds=window)
        remaining = deadline - now
        assert remaining > window
        assert remaining <= 2 * window


def test_thumbnail_key_is_deterministic_and_co_located() -> None:
    original = "tenants/tenant-a/images/42.jpg"
    key1 = build_thumbnail_storage_ref(original, "tenant-a", ".jpg")
    key2 = build_thumbnail_storage_ref(original, "tenant-a", ".jpg")
    assert key1 == key2  # deterministic -> idempotent overwrite
    assert key1.startswith("tenants/tenant-a/thumbnails/")
    assert key1.endswith(".jpg")
    # traceable to the original's stem
    assert "42" in key1


def test_thumbnail_key_distinct_per_original_and_ext() -> None:
    a = build_thumbnail_storage_ref("tenants/t/images/1.jpg", "t", ".jpg")
    b = build_thumbnail_storage_ref("tenants/t/images/2.jpg", "t", ".jpg")
    png = build_thumbnail_storage_ref("tenants/t/images/1.jpg", "t", ".png")
    assert a != b
    assert a != png


def test_qiniu_deadline_signing_is_stable_and_windowed() -> None:
    # Dummy (non-production) credentials — no real bucket, no secret leak.
    from app.qiniu_storage import QiniuStorageProvider

    provider = QiniuStorageProvider(
        access_key="AK_dummy",
        secret_key="SK_dummy",
        bucket="test-bucket",
        domain="https://media-origin.example.com",
    )
    ref = "tenants/tenant-a/images/9.jpg"
    deadline = 1_800_000_000

    url_a = provider.get_download_url(ref, deadline=deadline)
    url_b = provider.get_download_url(ref, deadline=deadline)
    assert url_a == url_b  # byte-identical within a window -> browser cache hit
    assert f"e={deadline}" in url_a
    assert "token=" in url_a

    # a different window's deadline yields a different URL
    url_next = provider.get_download_url(ref, deadline=deadline + 900)
    assert url_next != url_a


def test_qiniu_requires_expiry_or_deadline() -> None:
    from app.qiniu_storage import QiniuStorageProvider

    provider = QiniuStorageProvider(
        access_key="AK_dummy",
        secret_key="SK_dummy",
        bucket="test-bucket",
        domain="https://media-origin.example.com",
    )
    with pytest.raises(MediaStorageConfigurationError):
        provider.get_download_url("tenants/t/images/1.jpg")
