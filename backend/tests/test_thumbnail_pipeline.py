"""Tests for app.thumbnail_pipeline (RND-207).

Two layers:
  - generate_and_persist against lightweight fakes (no real DB / network):
    happy path, non-image skip, idempotent "exists", disabled, read/decode/
    upload failure isolation, and dry-run (no writes).
  - build_backfill_query against the shared sqlite test schema: candidate
    selection + --retry semantics + already-done exclusion.
"""

from __future__ import annotations

import io

from PIL import Image
from sqlalchemy import text

from app.media_storage import MediaObjectNotFound, MediaStorageOperationError
from app.thumbnail_pipeline import build_backfill_query, generate_and_persist

# Reuse the shared sqlite schema + session helper (media_files includes the
# RND-207 thumbnail columns there).
from tests.test_reachability_audit import _SCHEMA_SQL, _make_session  # noqa: F401


def _jpeg(w=800, h=600) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


class _FakeProvider:
    """Minimal storage provider double: an in-memory object store."""

    def __init__(self, objects=None, read_error=None, write_error=None):
        self.objects = dict(objects or {})
        self.read_error = read_error
        self.write_error = write_error
        self.saved = {}

    def read_bytes(self, ref):
        if self.read_error is not None:
            raise self.read_error
        if ref not in self.objects:
            raise MediaObjectNotFound("missing")
        return self.objects[ref]

    def save_bytes(self, ref, data):
        if self.write_error is not None:
            raise self.write_error
        self.saved[ref] = data
        return ref


class _FakeMediaFile:
    def __init__(self, **kw):
        self.file_type = kw.get("file_type", "image")
        self.tenant_id = kw.get("tenant_id", "tenant-a")
        self.storage_backend = kw.get("storage_backend", "qiniu_kodo")
        self.storage_ref = kw.get("storage_ref", "tenants/tenant-a/images/1.jpg")
        self.local_path = kw.get("local_path")
        self.download_status = kw.get("download_status", "downloaded")
        self.thumbnail_ref = None
        self.image_width = None
        self.image_height = None
        self.thumbnail_status = kw.get("thumbnail_status")
        self.thumbnail_attempted_at = None
        self.thumbnail_error = None


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_generate_happy_path_stamps_row_and_uploads(monkeypatch) -> None:
    monkeypatch.delenv("MEDIA_THUMBNAIL_ENABLED", raising=False)
    ref = "tenants/tenant-a/images/1.jpg"
    provider = _FakeProvider({ref: _jpeg()})
    mf = _FakeMediaFile(storage_ref=ref)
    session = _FakeSession()

    status = generate_and_persist(session, provider, mf)

    assert status == "generated"
    assert mf.thumbnail_status == "generated"
    assert mf.thumbnail_ref and mf.thumbnail_ref.startswith("tenants/tenant-a/thumbnails/")
    assert mf.image_width == 800 and mf.image_height == 600
    assert mf.thumbnail_error is None
    assert mf.thumbnail_ref in provider.saved
    assert session.commits == 1


def test_non_image_is_skipped_and_marked(monkeypatch) -> None:
    provider = _FakeProvider()
    mf = _FakeMediaFile(file_type="video")
    session = _FakeSession()
    status = generate_and_persist(session, provider, mf)
    assert status == "skipped"
    assert mf.thumbnail_status == "skipped"


def test_already_generated_is_noop(monkeypatch) -> None:
    provider = _FakeProvider()
    mf = _FakeMediaFile(thumbnail_status="generated")
    session = _FakeSession()
    status = generate_and_persist(session, provider, mf)
    assert status == "exists"
    assert session.commits == 0  # zero-op, no read/write


def test_disabled_is_skipped(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_THUMBNAIL_ENABLED", "false")
    provider = _FakeProvider({"tenants/tenant-a/images/1.jpg": _jpeg()})
    mf = _FakeMediaFile()
    session = _FakeSession()
    assert generate_and_persist(session, provider, mf) == "skipped"


def test_read_failure_is_isolated(monkeypatch) -> None:
    monkeypatch.delenv("MEDIA_THUMBNAIL_ENABLED", raising=False)
    provider = _FakeProvider(read_error=MediaObjectNotFound("gone"))
    mf = _FakeMediaFile()
    session = _FakeSession()
    status = generate_and_persist(session, provider, mf)
    assert status == "failed"
    assert mf.thumbnail_status == "failed"
    assert mf.thumbnail_error == "read_failed"


def test_decode_failure_is_isolated(monkeypatch) -> None:
    monkeypatch.delenv("MEDIA_THUMBNAIL_ENABLED", raising=False)
    ref = "tenants/tenant-a/images/1.jpg"
    provider = _FakeProvider({ref: b"not-an-image"})
    mf = _FakeMediaFile(storage_ref=ref)
    session = _FakeSession()
    status = generate_and_persist(session, provider, mf)
    assert status == "failed"
    assert mf.thumbnail_error == "decode_failed"


def test_upload_failure_is_isolated(monkeypatch) -> None:
    monkeypatch.delenv("MEDIA_THUMBNAIL_ENABLED", raising=False)
    ref = "tenants/tenant-a/images/1.jpg"
    provider = _FakeProvider(
        {ref: _jpeg()}, write_error=MediaStorageOperationError("qiniu down")
    )
    mf = _FakeMediaFile(storage_ref=ref)
    session = _FakeSession()
    status = generate_and_persist(session, provider, mf)
    assert status == "failed"
    assert mf.thumbnail_error == "upload_failed"


def test_dry_run_never_writes(monkeypatch) -> None:
    monkeypatch.delenv("MEDIA_THUMBNAIL_ENABLED", raising=False)
    ref = "tenants/tenant-a/images/1.jpg"
    provider = _FakeProvider({ref: _jpeg()})
    mf = _FakeMediaFile(storage_ref=ref)
    session = _FakeSession()
    status = generate_and_persist(session, provider, mf, dry_run=True)
    assert status == "would_generate"
    assert mf.thumbnail_status is None  # unchanged
    assert provider.saved == {}
    assert session.commits == 0


def test_error_tag_never_leaks_secrets(monkeypatch) -> None:
    monkeypatch.delenv("MEDIA_THUMBNAIL_ENABLED", raising=False)
    provider = _FakeProvider(read_error=MediaObjectNotFound("tenants/secret/key.jpg"))
    mf = _FakeMediaFile(storage_ref="tenants/tenant-a/images/secret-sdkfileid.jpg")
    session = _FakeSession()
    generate_and_persist(session, provider, mf)
    # the stamped error is a short fixed tag, never a path/key/exception text
    assert mf.thumbnail_error == "read_failed"
    assert "secret" not in (mf.thumbnail_error or "")


# ---------------------------------------------------------------------------
# build_backfill_query — candidate selection against the sqlite schema
# ---------------------------------------------------------------------------


def _insert_row(session, mf_id, *, file_type, download_status, thumbnail_status,
                tenant_id="tenant-a"):
    session.execute(
        text(
            "INSERT INTO media_files "
            "(id, sdkfileid, archive_message_id, tenant_id, file_type, "
            " storage_backend, storage_ref, download_status, thumbnail_status) "
            "VALUES (:id, :sdk, 1, :tid, :ft, 'qiniu_kodo', :ref, :ds, :ts)"
        ),
        {
            "id": mf_id, "sdk": f"sdk-{mf_id}", "tid": tenant_id, "ft": file_type,
            "ref": f"tenants/{tenant_id}/images/{mf_id}.jpg", "ds": download_status,
            "ts": thumbnail_status,
        },
    )
    session.commit()


def test_backfill_query_selects_only_eligible() -> None:
    session = _make_session()
    # eligible: image, downloaded, no thumbnail yet
    _insert_row(session, 1, file_type="image", download_status="downloaded", thumbnail_status=None)
    _insert_row(session, 2, file_type="emotion", download_status="downloaded", thumbnail_status=None)
    # excluded: already generated
    _insert_row(session, 3, file_type="image", download_status="downloaded", thumbnail_status="generated")
    # excluded: not an image
    _insert_row(session, 4, file_type="video", download_status="downloaded", thumbnail_status=None)
    # excluded: not downloaded
    _insert_row(session, 5, file_type="image", download_status="pending", thumbnail_status=None)
    # excluded: failed (unless --retry)
    _insert_row(session, 6, file_type="image", download_status="downloaded", thumbnail_status="failed")
    # excluded: other tenant
    _insert_row(session, 7, file_type="image", download_status="downloaded", thumbnail_status=None, tenant_id="tenant-b")

    ids = [r.id for r in build_backfill_query(session, "tenant-a", retry=False).all()]
    assert ids == [1, 2]

    retry_ids = [r.id for r in build_backfill_query(session, "tenant-a", retry=True).all()]
    assert retry_ids == [1, 2, 6]
