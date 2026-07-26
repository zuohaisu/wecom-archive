"""
Tests for RND-186 (media scope revision + QA fixes) — generalized
media-type byte signature detection and Content-Type lookup in
app/media_storage.py.

Scope: detect_media_signature_from_bytes, detect_media_content_type_for_ref,
media_key_category, MEDIA_TYPE_KEY_CATEGORIES,
SUPPORTED_MIGRATION_MEDIA_TYPES, compute_sha256_checksum, and the
QiniuStorageProvider.bucket / MediaStorageProvider.bucket properties. This
section is additive — it does not change detect_image_type_from_bytes,
detect_image_content_type(_for_ref), or any image-serving code path
(resolve_image_file_state and friends remain image-only and untouched).
The actual Qiniu upload Content-Type behavior (QiniuStorageProvider.save_bytes)
is covered end-to-end in tests/test_qiniu_storage.py.

Run (from backend/):
    pytest tests/test_media_signature_detection.py -v
"""

from __future__ import annotations

import hashlib

import pytest


# ---------------------------------------------------------------------------
# detect_media_signature_from_bytes — one case per supported media type
# ---------------------------------------------------------------------------


def test_detects_jpeg_as_image() -> None:
    from app.media_storage import detect_media_signature_from_bytes

    match = detect_media_signature_from_bytes(b"\xff\xd8\xff\xe0rest")
    assert match is not None
    assert (match.media_type, match.extension, match.mime_type) == ("image", ".jpg", "image/jpeg")


def test_detects_png_as_image() -> None:
    from app.media_storage import detect_media_signature_from_bytes

    match = detect_media_signature_from_bytes(b"\x89PNG\r\n\x1a\nrest")
    assert (match.media_type, match.extension) == ("image", ".png")


def test_detects_mp4_ftyp_box_as_video() -> None:
    from app.media_storage import detect_media_signature_from_bytes

    data = b"\x00\x00\x00\x20ftypisom" + b"rest-of-mp4"
    match = detect_media_signature_from_bytes(data)
    assert match is not None
    assert (match.media_type, match.extension, match.mime_type) == ("video", ".mp4", "video/mp4")


def test_detects_amr_as_voice() -> None:
    from app.media_storage import detect_media_signature_from_bytes

    match = detect_media_signature_from_bytes(b"#!AMR\n" + b"voice-body")
    assert match is not None
    assert (match.media_type, match.extension, match.mime_type) == ("voice", ".amr", "audio/amr")


def test_detects_silk_v3_as_voice() -> None:
    from app.media_storage import detect_media_signature_from_bytes

    match = detect_media_signature_from_bytes(b"#!SILK_V3" + b"voice-body")
    assert (match.media_type, match.extension) == ("voice", ".silk")


def test_detects_wav_as_voice() -> None:
    from app.media_storage import detect_media_signature_from_bytes

    data = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"fmt body"
    match = detect_media_signature_from_bytes(data)
    assert (match.media_type, match.extension) == ("voice", ".wav")


def test_detects_id3_mp3_as_voice() -> None:
    from app.media_storage import detect_media_signature_from_bytes

    match = detect_media_signature_from_bytes(b"ID3" + b"\x03\x00\x00\x00" + b"mp3-body")
    assert (match.media_type, match.extension, match.mime_type) == (
        "voice",
        ".mp3",
        "audio/mpeg",
    )


def test_detects_pdf_as_file() -> None:
    from app.media_storage import detect_media_signature_from_bytes

    match = detect_media_signature_from_bytes(b"%PDF-1.4\n" + b"pdf-body")
    assert (match.media_type, match.extension, match.mime_type) == (
        "file",
        ".pdf",
        "application/pdf",
    )


def test_detects_zip_family_as_file() -> None:
    from app.media_storage import detect_media_signature_from_bytes

    match = detect_media_signature_from_bytes(b"PK\x03\x04" + b"zip-body")
    assert (match.media_type, match.extension, match.mime_type) == (
        "file",
        ".zip",
        "application/zip",
    )


def test_returns_none_for_unrecognized_bytes() -> None:
    """RND-186 explicit exclusion: "无法安全识别类型的文件" — bytes that don't
    match any allow-listed signature must never be guessed at."""
    from app.media_storage import detect_media_signature_from_bytes

    assert detect_media_signature_from_bytes(b"totally-unrecognizable-binary-garbage") is None
    assert detect_media_signature_from_bytes(b"") is None
    assert detect_media_signature_from_bytes(b"BM fake bmp bytes") is None


def test_never_guesses_from_extension_or_hint_only_content() -> None:
    """The function takes bytes only — there is no extension/hint parameter
    to accidentally trust; garbage bytes with a plausible-looking prefix
    for one format but not matching any real signature return None."""
    from app.media_storage import detect_media_signature_from_bytes

    assert detect_media_signature_from_bytes(b"fake.mp4 header but not real bytes") is None


# ---------------------------------------------------------------------------
# media_key_category / MEDIA_TYPE_KEY_CATEGORIES / SUPPORTED_MIGRATION_MEDIA_TYPES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "media_type,expected_category",
    [("image", "images"), ("video", "videos"), ("voice", "voice"), ("file", "files")],
)
def test_media_key_category_matches_rnd186_ticket_examples(media_type, expected_category) -> None:
    from app.media_storage import media_key_category

    assert media_key_category(media_type) == expected_category


def test_media_key_category_raises_for_unsupported_type() -> None:
    from app.media_storage import media_key_category

    with pytest.raises(KeyError):
        media_key_category("miniprogram")


def test_supported_migration_media_types_matches_classifier_recognized_types() -> None:
    """SUPPORTED_MIGRATION_MEDIA_TYPES must line up exactly with
    app.media_classification's own recognized (non-text) media types —
    image/video/voice/file, plus audio_archive since RND-202 gave it a
    real byte-signature detector (the "voice" category — see
    app.media_download._SIGNATURE_CATEGORY_BY_MSGTYPE) and object-key path
    segment ("call_recordings") — so a future download worker for any of
    these types is automatically migratable with zero further changes."""
    from app.media_storage import SUPPORTED_MIGRATION_MEDIA_TYPES

    assert SUPPORTED_MIGRATION_MEDIA_TYPES == frozenset(
        {"image", "video", "voice", "file", "audio_archive"}
    )


# ---------------------------------------------------------------------------
# detect_media_content_type_for_ref (RND-186 QA fix) — the generalized
# lookup QiniuStorageProvider.save_bytes() now uses to set Content-Type on
# upload. End-to-end proof that this is actually what reaches Qiniu's SDK
# call lives in tests/test_qiniu_storage.py; these are pure unit tests of
# the lookup table itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "storage_ref,expected",
    [
        ("tenants/t1/images/1.jpg", "image/jpeg"),
        ("tenants/t1/images/2.jpeg", "image/jpeg"),
        ("tenants/t1/images/3.png", "image/png"),
        ("tenants/t1/images/4.gif", "image/gif"),
        ("tenants/t1/images/5.webp", "image/webp"),
        ("tenants/t1/videos/6.mp4", "video/mp4"),
        ("tenants/t1/voice/7.amr", "audio/amr"),
        ("tenants/t1/voice/8.silk", "audio/silk"),
        ("tenants/t1/voice/9.wav", "audio/wav"),
        ("tenants/t1/voice/10.mp3", "audio/mpeg"),
        ("tenants/t1/files/11.pdf", "application/pdf"),
        ("tenants/t1/files/12.zip", "application/zip"),
    ],
)
def test_detect_media_content_type_for_ref_covers_every_supported_category(
    storage_ref, expected
) -> None:
    from app.media_storage import detect_media_content_type_for_ref

    assert detect_media_content_type_for_ref(storage_ref) == expected


def test_detect_media_content_type_for_ref_returns_none_for_unrecognized_extension() -> None:
    from app.media_storage import detect_media_content_type_for_ref

    assert detect_media_content_type_for_ref("tenants/t1/images/1.part") is None
    assert detect_media_content_type_for_ref("tenants/t1/images/1.bmp") is None


def test_detect_media_content_type_for_ref_returns_none_for_empty_ref() -> None:
    from app.media_storage import detect_media_content_type_for_ref

    assert detect_media_content_type_for_ref(None) is None
    assert detect_media_content_type_for_ref("") is None


def test_detect_media_content_type_for_ref_agrees_with_image_only_lookup_for_images() -> None:
    """The generalized lookup must never disagree with the pre-existing,
    image-serving-route-facing detect_image_content_type_for_ref() for any
    image extension — same underlying table, not a reimplementation."""
    from app.media_storage import (
        detect_image_content_type_for_ref,
        detect_media_content_type_for_ref,
    )

    for ref in [
        "tenants/t1/images/1.jpg",
        "tenants/t1/images/2.png",
        "tenants/t1/images/3.gif",
        "tenants/t1/images/4.webp",
    ]:
        assert detect_media_content_type_for_ref(ref) == detect_image_content_type_for_ref(ref)


# ---------------------------------------------------------------------------
# compute_sha256_checksum
# ---------------------------------------------------------------------------


def test_compute_sha256_checksum_matches_stdlib_hashlib() -> None:
    from app.media_storage import compute_sha256_checksum

    data = b"arbitrary media bytes for checksum test"
    assert compute_sha256_checksum(data) == hashlib.sha256(data).hexdigest()


def test_compute_sha256_checksum_is_deterministic_and_content_sensitive() -> None:
    from app.media_storage import compute_sha256_checksum

    a = compute_sha256_checksum(b"payload-a")
    a_again = compute_sha256_checksum(b"payload-a")
    b = compute_sha256_checksum(b"payload-b")

    assert a == a_again
    assert a != b
    assert len(a) == 64  # hex-encoded SHA-256


# ---------------------------------------------------------------------------
# Provider .bucket property (RND-186 QA fix)
# ---------------------------------------------------------------------------


def test_local_storage_provider_bucket_is_none() -> None:
    from app.media_storage import LocalStorageProvider

    provider = LocalStorageProvider("/tmp")
    assert provider.bucket is None


def test_qiniu_storage_provider_bucket_matches_construction_argument() -> None:
    from app.qiniu_storage import QiniuStorageProvider

    provider = QiniuStorageProvider(
        access_key="fake-ak",
        secret_key="fake-sk",
        bucket="my-real-bucket",
        domain="https://cdn.example.com",
    )
    assert provider.bucket == "my-real-bucket"


# ---------------------------------------------------------------------------
# Regression: image-serving code path is completely unaffected
# ---------------------------------------------------------------------------


def test_detect_image_type_from_bytes_unchanged_by_generalized_detector() -> None:
    """The pre-existing, image-serving-route-facing detector must behave
    exactly as before — this module's generalization is additive, not a
    replacement."""
    from app.media_storage import detect_image_type_from_bytes

    assert detect_image_type_from_bytes(b"\xff\xd8\xff\xe0rest") == ".jpg"
    assert detect_image_type_from_bytes(b"\x89PNG\r\n\x1a\nrest") == ".png"
    assert detect_image_type_from_bytes(b"GIF89a" + b"rest") == ".gif"
    assert detect_image_type_from_bytes(b"not-an-image-at-all") is None
