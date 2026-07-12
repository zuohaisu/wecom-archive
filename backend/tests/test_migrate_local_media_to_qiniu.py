"""
Tests for RND-186 — Local -> Qiniu historical media migration tool.

Scope: scripts/migrate_local_media_to_qiniu.py. Exercises the candidate
query shape (local-only, tenant-scoped, download_status="downloaded",
file_type restricted to app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES,
migration_status gating incl. --retry), pagination (--batch-size vs
--limit), the per-row migration function for EVERY supported media type
(image/video/voice/file — content-signature verification, media_type
mismatch detection, deterministic per-type-category Qiniu key, full
metadata persistence: storage_ref/file_size/mime_type/checksum_sha256/
bucket), the migration_status state machine (never-attempted -> migrated /
failed), --dry-run performing zero writes and zero Qiniu calls, --count-only
performing zero reads/writes against storage, idempotent re-runs uploading
nothing for already-migrated rows, the concurrent-run lock, tenant
isolation, and the local-cleanup extension point (report-only, no delete).

Run (from backend/):
    pytest tests/test_migrate_local_media_to_qiniu.py -v
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as RealSession

from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)
from tests.test_tenant_media_access import _insert_media_file


# ---------------------------------------------------------------------------
# Fake Qiniu SDK boundary (same shape as tests/test_qiniu_worker_integration.py)
# ---------------------------------------------------------------------------


class _FakeInfo:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def ok(self) -> bool:
        return self.status_code // 100 == 2


def _make_qiniu_provider():
    from app.qiniu_storage import QiniuStorageProvider

    return QiniuStorageProvider(
        access_key="fake-ak",
        secret_key="fake-sk",
        bucket="test-bucket",
        domain="https://cdn.example.com",
    )


def _wire_success(
    provider, monkeypatch, store: dict, content_types: dict | None = None
) -> None:
    """content_types, when given, records key -> mime_type actually passed
    to the fake Qiniu put_data() call — i.e. what would become the
    object's real Content-Type in Qiniu (RND-186 QA fix coverage)."""

    def _put_data(up_token, key, data, mime_type=None, **kwargs):
        store[key] = data
        if content_types is not None:
            content_types[key] = mime_type
        return {"key": key}, _FakeInfo(200)

    def _stat(bucket, key):
        if key not in store:
            return None, _FakeInfo(612)
        return {"fsize": len(store[key])}, _FakeInfo(200)

    monkeypatch.setattr(provider._qiniu, "put_data", _put_data)
    monkeypatch.setattr(provider._bucket_manager, "stat", _stat)


def _wire_upload_failure(provider, monkeypatch, status_code: int = 579) -> None:
    monkeypatch.setattr(
        provider._qiniu, "put_data", lambda *a, **k: (None, _FakeInfo(status_code))
    )


# ---------------------------------------------------------------------------
# Candidate query shape
# ---------------------------------------------------------------------------


def _compiled_sql(query) -> str:
    return str(query.statement.compile(compile_kwargs={"literal_binds": True}))


def test_candidate_query_is_local_only_downloaded_only_tenant_scoped() -> None:
    from scripts.migrate_local_media_to_qiniu import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", retry=False))

    assert "media_files.tenant_id = 'tenant-a'" in sql
    assert "media_files.storage_backend = 'local'" in sql
    assert "media_files.download_status = 'downloaded'" in sql


def test_candidate_query_default_excludes_failed_migration_rows() -> None:
    from scripts.migrate_local_media_to_qiniu import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", retry=False))

    assert "migration_status IS NULL" in sql
    assert "'failed'" not in sql


def test_candidate_query_retry_includes_failed_migration_rows() -> None:
    from scripts.migrate_local_media_to_qiniu import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", retry=True))

    assert "migration_status IS NULL" in sql
    assert "'failed'" in sql


def test_candidate_query_never_selects_already_migrated_rows() -> None:
    """storage_backend == "local" is the sole gate that excludes an
    already-migrated (storage_backend == "qiniu_kodo") row — this is what
    makes a repeated run a true no-op for those rows."""
    from scripts.migrate_local_media_to_qiniu import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", retry=True))

    assert "'qiniu_kodo'" not in sql


def test_candidate_query_restricts_to_supported_media_types() -> None:
    """RND-186 fix (media scope revision): candidates are no longer
    image-only — the query must restrict to file_type IN
    app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES, which naturally
    excludes both a NULL file_type and any future/unrecognized value."""
    from app.media_storage import SUPPORTED_MIGRATION_MEDIA_TYPES
    from scripts.migrate_local_media_to_qiniu import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", retry=False))

    assert "media_files.file_type IN" in sql
    for media_type in SUPPORTED_MIGRATION_MEDIA_TYPES:
        assert f"'{media_type}'" in sql


# ---------------------------------------------------------------------------
# is_local_cleanup_candidate / find_local_cleanup_candidates — extension point
# ---------------------------------------------------------------------------


def test_is_local_cleanup_candidate_true_only_for_migrated_row_with_local_path() -> None:
    from scripts.migrate_local_media_to_qiniu import is_local_cleanup_candidate

    migrated_with_local = SimpleNamespace(
        storage_backend="qiniu_kodo", migration_status="migrated", local_path="/old/path.jpg"
    )
    assert is_local_cleanup_candidate(migrated_with_local) is True

    migrated_no_local = SimpleNamespace(
        storage_backend="qiniu_kodo", migration_status="migrated", local_path=None
    )
    assert is_local_cleanup_candidate(migrated_no_local) is False

    still_local = SimpleNamespace(
        storage_backend="local", migration_status=None, local_path="/old/path.jpg"
    )
    assert is_local_cleanup_candidate(still_local) is False

    failed_migration = SimpleNamespace(
        storage_backend="local", migration_status="failed", local_path="/old/path.jpg"
    )
    assert is_local_cleanup_candidate(failed_migration) is False


def test_find_local_cleanup_candidates_query_shape() -> None:
    from scripts.migrate_local_media_to_qiniu import find_local_cleanup_candidates

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(find_local_cleanup_candidates(session, "tenant-a"))

    assert "media_files.tenant_id = 'tenant-a'" in sql
    assert "media_files.storage_backend = 'qiniu_kodo'" in sql
    assert "media_files.migration_status = 'migrated'" in sql
    assert "local_path IS NOT NULL" in sql


# ---------------------------------------------------------------------------
# target_storage_ref — deterministic, tenant-aware, media-type-aware key
# ---------------------------------------------------------------------------


def test_target_storage_ref_matches_download_worker_key_format_for_image() -> None:
    from scripts.download_wecom_image_media_once import target_storage_refs
    from scripts.migrate_local_media_to_qiniu import target_storage_ref

    worker_base, _part = target_storage_refs("tenant-a", 42)
    migrated_ref = target_storage_ref("tenant-a", 42, "image", ".jpg")

    assert migrated_ref == f"{worker_base}.jpg"
    assert migrated_ref == "tenants/tenant-a/images/42.jpg"


@pytest.mark.parametrize(
    "media_type,ext,expected",
    [
        ("image", ".jpg", "tenants/tenant-a/images/1.jpg"),
        ("video", ".mp4", "tenants/tenant-a/videos/1.mp4"),
        ("voice", ".amr", "tenants/tenant-a/voice/1.amr"),
        ("file", ".pdf", "tenants/tenant-a/files/1.pdf"),
    ],
)
def test_target_storage_ref_uses_per_media_type_category(media_type, ext, expected) -> None:
    from scripts.migrate_local_media_to_qiniu import target_storage_ref

    assert target_storage_ref("tenant-a", 1, media_type, ext) == expected


# ---------------------------------------------------------------------------
# migrate_one() — per-row migration, no DB writes, all failure classes,
# ALL supported media types (RND-186 media scope revision)
# ---------------------------------------------------------------------------

_JPEG_BYTES = b"\xff\xd8\xff" + b"jpeg-body"
_MP4_BYTES = b"\x00\x00\x00\x20ftypisom" + b"mp4-body"
_AMR_BYTES = b"#!AMR\n" + b"amr-voice-body"
_PDF_BYTES = b"%PDF-1.4\n" + b"pdf-file-body"

_SAMPLE_BY_TYPE = {
    "image": ("42.jpg", _JPEG_BYTES, "images", ".jpg", "image/jpeg"),
    "video": ("42.dat", _MP4_BYTES, "videos", ".mp4", "video/mp4"),
    "voice": ("42.dat", _AMR_BYTES, "voice", ".amr", "audio/amr"),
    "file": ("42.dat", _PDF_BYTES, "files", ".pdf", "application/pdf"),
}


@pytest.mark.parametrize("media_type", ["image", "video", "voice", "file"])
def test_migrate_one_success_uploads_bytes_and_returns_full_metadata(
    media_type, tmp_path, monkeypatch
) -> None:
    """Migration selection/metadata requirement: image, video, voice, and
    file candidates each migrate successfully, uploading to the correct
    per-type object key and returning full, content-derived metadata
    (never trusted from any pre-existing field)."""
    from app.media_storage import LocalStorageProvider
    from scripts.migrate_local_media_to_qiniu import migrate_one

    filename, data, category, ext, mime = _SAMPLE_BY_TYPE[media_type]
    local_dir = tmp_path / "tenants" / "tenant-a" / category
    local_dir.mkdir(parents=True)
    local_file = local_dir / filename
    local_file.write_bytes(data)

    source = LocalStorageProvider(tmp_path)
    target = _make_qiniu_provider()
    store: dict = {}
    content_types: dict = {}
    _wire_success(target, monkeypatch, store, content_types=content_types)

    media_file = SimpleNamespace(
        storage_ref=str(local_file),
        local_path=str(local_file),
        archive_message_id=42,
        file_type=media_type,
    )

    result = migrate_one(source, target, "tenant-a", media_file)

    assert result.storage_ref == f"tenants/tenant-a/{category}/42{ext}"
    assert result.media_type == media_type
    assert result.mime_type == mime
    assert result.file_size == len(data)
    assert result.checksum_sha256 == hashlib.sha256(data).hexdigest()
    assert result.bucket == "test-bucket"
    assert store[result.storage_ref] == data

    # RND-186 QA fix: the mime_type actually sent to Qiniu's put_data() —
    # i.e. what becomes the object's real Content-Type in Qiniu's own
    # metadata — must match the database-recorded mime_type, for every
    # supported category. Before the fix this was always
    # "application/octet-stream" for video/voice/file.
    assert content_types[result.storage_ref] == mime
    assert content_types[result.storage_ref] != "application/octet-stream"


def test_migrate_one_missing_local_reference_raises_skip() -> None:
    from app.media_storage import LocalStorageProvider
    from scripts.migrate_local_media_to_qiniu import MigrationSkip, migrate_one

    source = LocalStorageProvider("/tmp")
    target = MagicMock()
    media_file = SimpleNamespace(
        storage_ref=None, local_path=None, archive_message_id=1, file_type="image"
    )

    with pytest.raises(MigrationSkip) as exc:
        migrate_one(source, target, "tenant-a", media_file)
    assert exc.value.reason == "missing_local_reference"
    target.save_bytes.assert_not_called()


def test_migrate_one_local_file_missing_raises_skip(tmp_path) -> None:
    from app.media_storage import LocalStorageProvider
    from scripts.migrate_local_media_to_qiniu import MigrationSkip, migrate_one

    source = LocalStorageProvider(tmp_path)
    target = MagicMock()
    media_file = SimpleNamespace(
        storage_ref="tenants/tenant-a/images/99.jpg",
        local_path=None,
        archive_message_id=99,
        file_type="image",
    )

    with pytest.raises(MigrationSkip) as exc:
        migrate_one(source, target, "tenant-a", media_file)
    assert exc.value.reason == "local_file_missing"
    target.save_bytes.assert_not_called()


def test_migrate_one_unrecognized_signature_raises_skip(tmp_path) -> None:
    """RND-186 explicit exclusion: "无法安全识别类型的文件" — bytes that don't
    match any known signature at all (not PDF, not ZIP, not any known
    image/video/voice format) must never be migrated, regardless of what
    file_type claims."""
    from app.media_storage import LocalStorageProvider
    from scripts.migrate_local_media_to_qiniu import MigrationSkip, migrate_one

    local_dir = tmp_path / "tenants" / "tenant-a" / "files"
    local_dir.mkdir(parents=True)
    local_file = local_dir / "7.dat"
    local_file.write_bytes(b"totally-unrecognizable-binary-garbage")

    source = LocalStorageProvider(tmp_path)
    target = MagicMock()
    media_file = SimpleNamespace(
        storage_ref=str(local_file), local_path=None, archive_message_id=7, file_type="file"
    )

    with pytest.raises(MigrationSkip) as exc:
        migrate_one(source, target, "tenant-a", media_file)
    assert exc.value.reason == "unrecognized_signature"
    target.save_bytes.assert_not_called()


def test_migrate_one_media_type_mismatch_raises_skip(tmp_path) -> None:
    """A stale/corrupted file_type must never silently relabel what gets
    uploaded: bytes that ARE a recognized signature, but for a DIFFERENT
    media_type than the row's own file_type, must be rejected rather than
    migrated under the wrong category."""
    from app.media_storage import LocalStorageProvider
    from scripts.migrate_local_media_to_qiniu import MigrationSkip, migrate_one

    local_dir = tmp_path / "tenants" / "tenant-a" / "videos"
    local_dir.mkdir(parents=True)
    local_file = local_dir / "9.dat"
    local_file.write_bytes(_JPEG_BYTES)  # actually a JPEG, not a video

    source = LocalStorageProvider(tmp_path)
    target = MagicMock()
    media_file = SimpleNamespace(
        storage_ref=str(local_file), local_path=None, archive_message_id=9, file_type="video"
    )

    with pytest.raises(MigrationSkip) as exc:
        migrate_one(source, target, "tenant-a", media_file)
    assert exc.value.reason == "media_type_mismatch"
    target.save_bytes.assert_not_called()


def test_migrate_one_qiniu_upload_failure_raises_skip(tmp_path, monkeypatch) -> None:
    from app.media_storage import LocalStorageProvider
    from scripts.migrate_local_media_to_qiniu import MigrationSkip, migrate_one

    local_dir = tmp_path / "tenants" / "tenant-a" / "images"
    local_dir.mkdir(parents=True)
    local_file = local_dir / "8.jpg"
    local_file.write_bytes(_JPEG_BYTES)

    source = LocalStorageProvider(tmp_path)
    target = _make_qiniu_provider()
    _wire_upload_failure(target, monkeypatch)

    media_file = SimpleNamespace(
        storage_ref="tenants/tenant-a/images/8.jpg",
        local_path=None,
        archive_message_id=8,
        file_type="image",
    )

    with pytest.raises(MigrationSkip) as exc:
        migrate_one(source, target, "tenant-a", media_file)
    assert exc.value.reason == "qiniu_upload_error"


def test_migrate_one_falls_back_to_legacy_local_path_when_storage_ref_unset(
    tmp_path, monkeypatch
) -> None:
    """A pre-migration-0005 row (storage_ref never backfilled) must still
    be migratable via its legacy local_path."""
    from app.media_storage import LocalStorageProvider
    from scripts.migrate_local_media_to_qiniu import migrate_one

    local_dir = tmp_path / "tenants" / "tenant-a" / "images"
    local_dir.mkdir(parents=True)
    local_file = local_dir / "5.jpg"
    local_file.write_bytes(b"\xff\xd8\xff" + b"legacy-body")

    source = LocalStorageProvider(tmp_path)
    target = _make_qiniu_provider()
    store: dict = {}
    _wire_success(target, monkeypatch, store)

    media_file = SimpleNamespace(
        storage_ref=None, local_path=str(local_file), archive_message_id=5, file_type="image"
    )

    result = migrate_one(source, target, "tenant-a", media_file)
    assert store[result.storage_ref] == b"\xff\xd8\xff" + b"legacy-body"


# ---------------------------------------------------------------------------
# _select_candidates pagination — batch_size independent of limit, cursor by id
# ---------------------------------------------------------------------------


def _make_sqlite_engine(tmp_path, name="migration_test.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE media_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sdkfileid TEXT NOT NULL,
                    archive_message_id INTEGER NOT NULL,
                    file_type TEXT,
                    local_path TEXT,
                    oss_key TEXT,
                    storage_backend TEXT,
                    storage_ref TEXT,
                    file_size INTEGER,
                    download_status TEXT NOT NULL DEFAULT 'pending',
                    migration_status TEXT,
                    migration_attempted_at TEXT,
                    migration_error TEXT,
                    bucket TEXT,
                    mime_type TEXT,
                    checksum_sha256 TEXT,
                    tenant_id TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    UNIQUE(tenant_id, sdkfileid)
                )
                """
            )
        )
    return engine


def _insert_local_downloaded_row(
    session,
    row_id: int,
    tenant_id: str,
    sdkfileid: str,
    local_path: str,
    migration_status=None,
    file_type="image",
    download_status="downloaded",
) -> None:
    from app.db.models import MediaFile

    session.add(
        MediaFile(
            id=row_id,
            sdkfileid=sdkfileid,
            archive_message_id=row_id,
            tenant_id=tenant_id,
            file_type=file_type,
            local_path=local_path,
            storage_backend="local",
            storage_ref=local_path,
            download_status=download_status,
            migration_status=migration_status,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Migration selection by media type (RND-186 explicit test requirement):
# image/video/voice/file selected; unsupported/no-file-type/no-local-file/
# failed-download excluded outright at the query level.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("media_type", ["image", "video", "voice", "file"])
def test_select_candidates_selects_each_supported_media_type(media_type, tmp_path) -> None:
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)
    _insert_local_downloaded_row(
        session, 1, "tenant-a", "sdk-1", "/media/1.dat", file_type=media_type
    )

    selected = _select_candidates(session, "tenant-a", retry=False, limit=10, batch_size=10)
    assert [row.id for row in selected] == [1]
    assert selected[0].file_type == media_type


def test_select_candidates_excludes_unsupported_media_type(tmp_path) -> None:
    """A row whose file_type is some unrecognized/future value (e.g. a
    structured/placeholder type that never has bytes) must be excluded
    outright — not selected and then skipped, excluded from the candidate
    set entirely."""
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)
    _insert_local_downloaded_row(
        session, 1, "tenant-a", "sdk-1", "/media/1.dat", file_type="miniprogram"
    )

    selected = _select_candidates(session, "tenant-a", retry=False, limit=10, batch_size=10)
    assert selected == []


def test_select_candidates_excludes_row_with_no_file_type(tmp_path) -> None:
    """A row with file_type=NULL — "the system cannot identify the media
    type" — must be excluded outright per RND-186's explicit admission
    criterion, not merely skipped at migration time."""
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)
    _insert_local_downloaded_row(
        session, 1, "tenant-a", "sdk-1", "/media/1.dat", file_type=None
    )

    selected = _select_candidates(session, "tenant-a", retry=False, limit=10, batch_size=10)
    assert selected == []


def test_select_candidates_excludes_failed_download(tmp_path) -> None:
    """RND-186 explicit exclusion: a row whose download itself failed
    (download_status != "downloaded") has no bytes to migrate and must
    never be a candidate."""
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)
    _insert_local_downloaded_row(
        session, 1, "tenant-a", "sdk-1", "/media/1.dat", download_status="failed"
    )
    _insert_local_downloaded_row(
        session, 2, "tenant-a", "sdk-2", "/media/2.dat", download_status="pending"
    )

    selected = _select_candidates(session, "tenant-a", retry=False, limit=10, batch_size=10)
    assert selected == []


def test_select_candidates_excludes_row_with_no_local_file_at_migration_time(tmp_path) -> None:
    """A row is still *selected* at the query level even if its local file
    is missing on disk (the query has no way to check the filesystem) —
    but migrate_one() must then skip it (see
    test_migrate_one_local_file_missing_raises_skip). This test documents
    that boundary: selection is DB-only, existence is checked per-row
    during migration, never conflated."""
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)
    _insert_local_downloaded_row(
        session, 1, "tenant-a", "sdk-1", str(tmp_path / "does" / "not" / "exist.jpg")
    )

    selected = _select_candidates(session, "tenant-a", retry=False, limit=10, batch_size=10)
    assert [row.id for row in selected] == [1]  # selected; migrate_one() is what skips it


def test_select_candidates_paginates_and_respects_limit(tmp_path) -> None:
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    for i in range(1, 11):
        _insert_local_downloaded_row(session, i, "tenant-a", f"sdk-{i}", f"/media/{i}.jpg")

    selected = _select_candidates(session, "tenant-a", retry=False, limit=4, batch_size=2)

    assert [row.id for row in selected] == [1, 2, 3, 4]


def test_select_candidates_batch_size_independent_of_result_correctness(tmp_path) -> None:
    """A tiny batch_size must still find every eligible row, not just the
    first page — same pagination-correctness guarantee the download
    worker's _scan_for_stale_downloaded already proves for its own scan."""
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    for i in range(1, 8):
        _insert_local_downloaded_row(session, i, "tenant-a", f"sdk-{i}", f"/media/{i}.jpg")

    selected = _select_candidates(session, "tenant-a", retry=False, limit=100, batch_size=2)

    assert [row.id for row in selected] == list(range(1, 8))


def test_select_candidates_skips_already_migrated_rows(tmp_path) -> None:
    from app.db.models import MediaFile
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    _insert_local_downloaded_row(session, 1, "tenant-a", "sdk-1", "/media/1.jpg")
    session.add(
        MediaFile(
            id=2,
            sdkfileid="sdk-2",
            archive_message_id=2,
            tenant_id="tenant-a",
            storage_backend="qiniu_kodo",
            storage_ref="tenants/tenant-a/images/2.jpg",
            download_status="downloaded",
            migration_status="migrated",
        )
    )
    session.commit()

    selected = _select_candidates(session, "tenant-a", retry=False, limit=100, batch_size=10)
    assert [row.id for row in selected] == [1]


def test_select_candidates_excludes_failed_without_retry_includes_with_retry(tmp_path) -> None:
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    _insert_local_downloaded_row(session, 1, "tenant-a", "sdk-1", "/media/1.jpg")
    _insert_local_downloaded_row(
        session, 2, "tenant-a", "sdk-2", "/media/2.jpg", migration_status="failed"
    )

    no_retry = _select_candidates(session, "tenant-a", retry=False, limit=100, batch_size=10)
    assert [row.id for row in no_retry] == [1]

    with_retry = _select_candidates(session, "tenant-a", retry=True, limit=100, batch_size=10)
    assert [row.id for row in with_retry] == [1, 2]


def test_select_candidates_tenant_isolation(tmp_path) -> None:
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)

    _insert_local_downloaded_row(session, 1, "tenant-a", "sdk-1", "/media/a1.jpg")
    _insert_local_downloaded_row(session, 2, "tenant-b", "sdk-1", "/media/b1.jpg")

    selected = _select_candidates(session, "tenant-a", retry=False, limit=100, batch_size=10)
    assert [row.id for row in selected] == [1]

    selected_b = _select_candidates(session, "tenant-b", retry=False, limit=100, batch_size=10)
    assert [row.id for row in selected_b] == [2]


# ---------------------------------------------------------------------------
# Concurrent-run lock (same fcntl pattern as the download worker)
# ---------------------------------------------------------------------------


def test_acquire_lock_returns_fd_when_available(tmp_path) -> None:
    import scripts.migrate_local_media_to_qiniu as script

    lock_path = str(tmp_path / "sub" / "migration.lock")
    fd = script._acquire_lock(lock_path)
    assert fd is not None
    script._release_lock(fd)


def test_acquire_lock_returns_none_when_already_held(tmp_path) -> None:
    import scripts.migrate_local_media_to_qiniu as script

    lock_path = str(tmp_path / "migration.lock")
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert script._acquire_lock(lock_path) is None
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_main_exits_cleanly_without_db_access_when_lock_held(tmp_path, monkeypatch, capsys) -> None:
    import scripts.migrate_local_media_to_qiniu as script

    lock_path = str(tmp_path / "migration.lock")
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setenv("MEDIA_MIGRATION_LOCK_PATH", lock_path)
    monkeypatch.setattr(
        script,
        "create_engine",
        MagicMock(side_effect=AssertionError("must not touch DB when lock is held")),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--count-only"])

    try:
        with pytest.raises(SystemExit) as exc:
            script.main()
        assert exc.value.code == 0
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    captured = capsys.readouterr()
    assert "already holds the run lock" in captured.out
    assert lock_path not in captured.out


# ---------------------------------------------------------------------------
# main() end-to-end — --count-only, --dry-run, success, failure, retry,
# idempotent re-run, db-commit-failure safety
# ---------------------------------------------------------------------------


def _query_mock(all_result=None, count_result=0, first_result=None):
    q = MagicMock()
    q.filter.return_value = q
    q.order_by.return_value = q
    q.limit.return_value = q
    q.all.return_value = list(all_result or [])
    q.count.return_value = count_result
    q.first.return_value = first_result
    return q


def _use_scratch_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_MIGRATION_LOCK_PATH", str(tmp_path / "migration.lock"))


def test_count_only_performs_no_reads_or_writes(tmp_path, monkeypatch, capsys) -> None:
    import scripts.migrate_local_media_to_qiniu as script

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")

    tenant_row = SimpleNamespace(tenant_id="tenant-a")

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.MediaFile:
            return _query_mock(count_result=7)
        raise AssertionError(f"unexpected model queried in count-only mode: {model}")

    session = MagicMock()
    session.query.side_effect = _query
    session.add.side_effect = AssertionError("must not write in count-only mode")
    session.commit.side_effect = AssertionError("must not commit in count-only mode")

    @contextmanager
    def _fake_session(_engine):
        yield session

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(
        script,
        "get_media_storage_provider_for_backend",
        MagicMock(side_effect=AssertionError("must not build storage providers in count-only mode")),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--count-only"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    captured = capsys.readouterr()
    assert "candidate_total: 7" in captured.out
    assert "count-only mode" in captured.out


def _run_main_with_one_candidate(monkeypatch, tmp_path, jpeg_or_garbage_bytes, extra_args=None):
    import scripts.migrate_local_media_to_qiniu as script
    from app.db.models import MediaFile

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")

    media_root = tmp_path / "media"
    local_dir = media_root / "tenants" / "tenant-a" / "images"
    local_dir.mkdir(parents=True)
    local_file = local_dir / "1.jpg"
    local_file.write_bytes(jpeg_or_garbage_bytes)

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    media_file_row = MediaFile(
        id=1,
        sdkfileid="sdk-secret-1",
        archive_message_id=1,
        tenant_id="tenant-a",
        file_type="image",
        storage_backend="local",
        storage_ref=str(local_file),
        local_path=str(local_file),
        file_size=999999,  # deliberately wrong — must be overwritten, never trusted
        download_status="downloaded",
        migration_status=None,
    )

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.MediaFile:
            return _query_mock(count_result=1)
        raise AssertionError(f"unexpected model queried: {model}")

    session = MagicMock()
    session.query.side_effect = _query

    @contextmanager
    def _fake_session(_engine):
        yield session

    from app.media_storage import LocalStorageProvider

    source_provider = LocalStorageProvider(media_root)

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(
        script, "_select_candidates", lambda *_a, **_k: [media_file_row]
    )
    monkeypatch.setattr(sys, "argv", ["prog"] + (extra_args or []))
    return script, media_file_row, session, source_provider, media_root


def test_main_success_flips_storage_backend_and_preserves_local_path(
    tmp_path, monkeypatch
) -> None:
    script, media_file_row, _session, source_provider, _root = _run_main_with_one_candidate(
        monkeypatch, tmp_path, b"\xff\xd8\xff" + b"jpeg-body"
    )

    target_provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(target_provider, monkeypatch, store)
    monkeypatch.setattr(
        script, "_build_providers", lambda: (source_provider, target_provider)
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.storage_backend == "qiniu_kodo"
    assert media_file_row.storage_ref == "tenants/tenant-a/images/1.jpg"
    assert media_file_row.migration_status == "migrated"
    assert media_file_row.migration_error is None
    assert media_file_row.migration_attempted_at is not None
    # local_path is intentionally left untouched — rollback safety net.
    assert media_file_row.local_path is not None
    assert store["tenants/tenant-a/images/1.jpg"] == b"\xff\xd8\xff" + b"jpeg-body"

    # RND-186 QA fix: full storage metadata persisted, all recomputed from
    # the actual uploaded bytes — file_size must be OVERWRITTEN (the row
    # was seeded with a deliberately wrong file_size=999999 above), never
    # trusted from the pre-existing value.
    uploaded_len = len(b"\xff\xd8\xff" + b"jpeg-body")
    assert media_file_row.file_size == uploaded_len
    assert media_file_row.file_size != 999999
    assert media_file_row.mime_type == "image/jpeg"
    assert media_file_row.checksum_sha256 == hashlib.sha256(
        b"\xff\xd8\xff" + b"jpeg-body"
    ).hexdigest()
    assert media_file_row.bucket == "test-bucket"


def test_main_dry_run_never_calls_qiniu_or_writes_db(tmp_path, monkeypatch, capsys) -> None:
    script, media_file_row, session, source_provider, _root = _run_main_with_one_candidate(
        monkeypatch, tmp_path, b"\xff\xd8\xff" + b"jpeg-body", extra_args=["--dry-run"]
    )

    target_provider = MagicMock()
    target_provider.save_bytes.side_effect = AssertionError("dry-run must never call Qiniu")
    monkeypatch.setattr(
        script, "_build_providers", lambda: (source_provider, target_provider)
    )
    session.commit.side_effect = AssertionError("dry-run must never commit")

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.storage_backend == "local"
    assert media_file_row.migration_status is None
    target_provider.save_bytes.assert_not_called()

    captured = capsys.readouterr()
    assert "would_migrate: 1" in captured.out
    assert "sdk-secret-1" not in captured.out


def test_main_qiniu_upload_failure_leaves_row_local_and_records_failure(
    tmp_path, monkeypatch
) -> None:
    script, media_file_row, _session, source_provider, _root = _run_main_with_one_candidate(
        monkeypatch, tmp_path, b"\xff\xd8\xff" + b"jpeg-body"
    )

    target_provider = _make_qiniu_provider()
    _wire_upload_failure(target_provider, monkeypatch)
    monkeypatch.setattr(
        script, "_build_providers", lambda: (source_provider, target_provider)
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.storage_backend == "local"
    assert media_file_row.storage_ref is not None
    assert media_file_row.migration_status == "failed"
    assert media_file_row.migration_error == "qiniu_upload_error"


def test_main_unrecognized_signature_leaves_row_local_and_records_failure(
    tmp_path, monkeypatch
) -> None:
    script, media_file_row, _session, source_provider, _root = _run_main_with_one_candidate(
        monkeypatch, tmp_path, b"totally-not-an-image"
    )

    target_provider = MagicMock()
    target_provider.save_bytes.side_effect = AssertionError(
        "must not attempt upload for an unrecognised byte signature"
    )
    monkeypatch.setattr(
        script, "_build_providers", lambda: (source_provider, target_provider)
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.storage_backend == "local"
    assert media_file_row.migration_status == "failed"
    assert media_file_row.migration_error == "unrecognized_signature"


def test_main_media_type_mismatch_leaves_row_local_and_records_failure(
    tmp_path, monkeypatch
) -> None:
    """A row whose stored file_type ("video") disagrees with what the
    bytes actually are (a JPEG) must fail safely, never be migrated under
    the wrong category."""
    import scripts.migrate_local_media_to_qiniu as script
    from app.db.models import MediaFile
    from app.media_storage import LocalStorageProvider

    _use_scratch_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")

    media_root = tmp_path / "media"
    local_dir = media_root / "tenants" / "tenant-a" / "videos"
    local_dir.mkdir(parents=True)
    local_file = local_dir / "1.dat"
    local_file.write_bytes(_JPEG_BYTES)

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    media_file_row = MediaFile(
        id=1,
        sdkfileid="sdk-secret-1",
        archive_message_id=1,
        tenant_id="tenant-a",
        file_type="video",
        storage_backend="local",
        storage_ref=str(local_file),
        local_path=str(local_file),
        download_status="downloaded",
        migration_status=None,
    )

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.MediaFile:
            return _query_mock(count_result=1)
        raise AssertionError(f"unexpected model queried: {model}")

    session = MagicMock()
    session.query.side_effect = _query

    @contextmanager
    def _fake_session(_engine):
        yield session

    source_provider = LocalStorageProvider(media_root)
    target_provider = MagicMock()
    target_provider.save_bytes.side_effect = AssertionError(
        "must not attempt upload on a media_type mismatch"
    )

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(script, "_select_candidates", lambda *_a, **_k: [media_file_row])
    monkeypatch.setattr(script, "_build_providers", lambda: (source_provider, target_provider))
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.storage_backend == "local"
    assert media_file_row.migration_status == "failed"
    assert media_file_row.migration_error == "media_type_mismatch"


def test_main_db_commit_failure_after_upload_leaves_row_reverted_to_local(
    tmp_path, monkeypatch
) -> None:
    """The one deliberate divergence from a clean success: if the DB commit
    itself fails right after a confirmed-successful Qiniu upload, the ORM
    object's in-memory attribute changes are rolled back (session.rollback())
    so the row is reported/observed as still "local" — matching what every
    other read path sees, since nothing was actually persisted. No Qiniu
    cleanup call is needed (see module docstring)."""
    script, media_file_row, session, source_provider, _root = _run_main_with_one_candidate(
        monkeypatch, tmp_path, b"\xff\xd8\xff" + b"jpeg-body"
    )

    target_provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(target_provider, monkeypatch, store)
    monkeypatch.setattr(
        script, "_build_providers", lambda: (source_provider, target_provider)
    )

    def _commit_side_effect():
        raise RuntimeError("simulated DB commit failure")

    session.commit.side_effect = _commit_side_effect

    def _rollback_side_effect():
        media_file_row.storage_backend = "local"
        media_file_row.storage_ref = str(_root / "tenants" / "tenant-a" / "images" / "1.jpg")
        media_file_row.migration_status = None

    session.rollback.side_effect = _rollback_side_effect

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.storage_backend == "local"
    # The object was still uploaded to the deterministic key — harmless,
    # will be overwritten by the next (successful) attempt.
    assert "tenants/tenant-a/images/1.jpg" in store


def test_main_never_prints_sdkfileid_or_paths(tmp_path, monkeypatch, capsys) -> None:
    script, _media_file_row, _session, source_provider, _root = _run_main_with_one_candidate(
        monkeypatch, tmp_path, b"\xff\xd8\xff" + b"jpeg-body"
    )

    target_provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(target_provider, monkeypatch, store)
    monkeypatch.setattr(
        script, "_build_providers", lambda: (source_provider, target_provider)
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    captured = capsys.readouterr()
    assert "sdk-secret-1" not in captured.out
    assert str(_root) not in captured.out


# ---------------------------------------------------------------------------
# Idempotent re-run: once migrated, a second run finds and uploads nothing
# ---------------------------------------------------------------------------


def test_rerun_after_success_finds_zero_candidates_and_uploads_nothing(tmp_path) -> None:
    """End-to-end idempotency proof against a real SQLite DB (not mocks):
    after one row is migrated, build_candidate_query for the same tenant
    returns nothing — a second script run would perform zero Qiniu calls,
    not just an idempotent overwrite."""
    from scripts.migrate_local_media_to_qiniu import _select_candidates, build_candidate_query

    engine = _make_sqlite_engine(tmp_path)
    session = RealSession(engine)
    _insert_local_downloaded_row(session, 1, "tenant-a", "sdk-1", "/media/1.jpg")

    first_pass = _select_candidates(session, "tenant-a", retry=False, limit=10, batch_size=10)
    assert len(first_pass) == 1

    # Simulate a successful migration commit.
    row = first_pass[0]
    row.storage_backend = "qiniu_kodo"
    row.storage_ref = "tenants/tenant-a/images/1.jpg"
    row.migration_status = "migrated"
    session.commit()

    second_pass = _select_candidates(session, "tenant-a", retry=True, limit=10, batch_size=10)
    assert second_pass == []
    assert build_candidate_query(session, "tenant-a", retry=True).count() == 0


# ---------------------------------------------------------------------------
# End-to-end: a migrated row remains servable via the existing (unmodified)
# authenticated media route — proving RND-186 needs no route/serving change.
# Reuses the shared real-DB test fixture (tests/test_reachability_audit.py)
# and the same fake-cloud-provider patch pattern as
# tests/test_qiniu_media_serving.py.
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _authed(app, db_session, tenant_id):
    from app.auth import get_current_user
    from app.db.session import get_db

    def _db_gen():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: (object(), tenant_id)
    app.dependency_overrides[get_db] = _db_gen


def _patch_target_provider(monkeypatch, objects: dict) -> None:
    from app import media_storage
    from app.routers import conversations as conv

    class _FakeCloudProvider:
        def supports_local_path(self) -> bool:
            return False

        def save_bytes(self, storage_ref: str, data: bytes) -> str:
            objects[storage_ref] = data
            return storage_ref

        def exists(self, storage_ref) -> bool:
            return bool(storage_ref) and storage_ref in objects

        def read_bytes(self, storage_ref: str) -> bytes:
            from app.media_storage import MediaObjectNotFound

            if storage_ref not in objects:
                raise MediaObjectNotFound("media object is missing")
            return objects[storage_ref]

        def get_local_path(self, storage_ref):
            return None

    provider = _FakeCloudProvider()
    real_factory = media_storage.get_media_storage_provider

    def _fake_factory(storage_backend=None):
        if (storage_backend or media_storage.get_configured_write_backend_name()) == "qiniu_kodo":
            return provider
        return real_factory(storage_backend)

    monkeypatch.setattr(media_storage, "get_media_storage_provider", _fake_factory)
    monkeypatch.setattr(conv, "get_media_storage_provider", _fake_factory)
    return provider


def test_migrated_row_remains_servable_via_existing_media_route(
    client, db, monkeypatch, tmp_path
) -> None:
    from app.main import app
    from scripts.migrate_local_media_to_qiniu import migrate_one
    from app.media_storage import LocalStorageProvider

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    jpeg_bytes = b"\xff\xd8\xff" + b"migrated-jpeg-body"
    local_dir = tmp_path / "tenants" / "tenant-a" / "images"
    local_dir.mkdir(parents=True)
    local_file = local_dir / "1.jpg"
    local_file.write_bytes(jpeg_bytes)

    msg = _insert_message(
        db, msgid="msg-migrate-1", msgtype="image", sender="staff_a", roomid="roomMigrate",
        sdkfileid="sdk-migrate-1", tenant_id=_TENANT_A, msgtime=100,
    )
    media_file = _insert_media_file(
        db, msg.id, _TENANT_A, "sdk-migrate-1",
        local_path=str(local_file), storage_backend="local", storage_ref=str(local_file),
    )

    # Sanity: servable via local BEFORE migration.
    _authed(app, db, _TENANT_A)
    try:
        before = client.get(f"/api/conversations/roomMigrate/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()
    assert before.status_code == 200
    assert before.content == jpeg_bytes

    # Migrate: exactly what the script's per-row logic does, no DB-layer
    # shortcuts — migrate_one() itself calls target_provider.save_bytes().
    source_provider = LocalStorageProvider(tmp_path)
    target_store: dict = {}
    target_provider = _patch_target_provider(monkeypatch, target_store)
    result = migrate_one(source_provider, target_provider, _TENANT_A, media_file)

    media_file.storage_backend = "qiniu_kodo"
    media_file.storage_ref = result.storage_ref
    media_file.file_size = result.file_size
    media_file.mime_type = result.mime_type
    media_file.checksum_sha256 = result.checksum_sha256
    media_file.migration_status = "migrated"
    db.commit()

    # Servable via Qiniu AFTER migration — same route, unmodified.
    _authed(app, db, _TENANT_A)
    try:
        after = client.get(f"/api/conversations/roomMigrate/messages/{msg.msgid}/media")
    finally:
        app.dependency_overrides.clear()

    assert after.status_code == 200
    assert after.content == jpeg_bytes
    assert result.storage_ref == "tenants/tenant-a/images/1.jpg"


def test_migration_never_crosses_tenant_boundary(client, db, monkeypatch, tmp_path) -> None:
    """A tenant-a migration run must never select or touch a tenant-b row,
    even one with an identical sdkfileid/local layout."""
    from scripts.migrate_local_media_to_qiniu import _select_candidates

    msg_a = _insert_message(
        db, msgid="msg-tenant-a", msgtype="image", sender="staff_a", roomid="roomTA",
        sdkfileid="sdk-shared", tenant_id=_TENANT_A, msgtime=100,
    )
    media_a = _insert_media_file(
        db, msg_a.id, _TENANT_A, "sdk-shared",
        local_path="/media/a.jpg", storage_backend="local", storage_ref="/media/a.jpg",
    )
    msg_b = _insert_message(
        db, msgid="msg-tenant-b", msgtype="image", sender="staff_b", roomid="roomTB",
        sdkfileid="sdk-shared", tenant_id=_TENANT_B, msgtime=100,
    )
    _insert_media_file(
        db, msg_b.id, _TENANT_B, "sdk-shared",
        local_path="/media/b.jpg", storage_backend="local", storage_ref="/media/b.jpg",
    )

    selected = _select_candidates(db, _TENANT_A, retry=False, limit=100, batch_size=10)
    assert [row.id for row in selected] == [media_a.id]
