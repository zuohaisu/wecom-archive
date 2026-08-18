"""
Tests for RND-174 — download worker writing through QiniuStorageProvider.

Scope: app/media_download.py's download_one() (the unified pipeline —
RND-199 folded image into it; see that module's docstring), proving it
writes through the storage-provider boundary unchanged (the worker never
calls the qiniu SDK directly) whether the provider is Local or Qiniu, for
the image message type specifically. Mocks the Qiniu SDK boundary with an
in-memory dict standing in for bucket contents; never calls the real Qiniu
service.

Run (from backend/):
    pytest tests/test_qiniu_worker_integration.py -v
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.qiniu_storage import QiniuStorageProvider


@pytest.fixture(autouse=True)
def _allow_capacity_for_pre_rnd385_worker_contracts(monkeypatch):
    """These provider tests predate billing and isolate Qiniu semantics."""
    monkeypatch.setattr(
        "app.services.media_worker.check_storage_write",
        lambda *_args, **_kwargs: SimpleNamespace(reason="allowed"),
    )
    monkeypatch.setattr(
        "app.services.media_worker.refresh_tenant_storage_daily",
        lambda *_args, **_kwargs: 1,
    )


class _FakeInfo:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def ok(self) -> bool:
        return self.status_code // 100 == 2


def _make_qiniu_provider() -> QiniuStorageProvider:
    return QiniuStorageProvider(
        access_key="fake-ak",
        secret_key="fake-sk",
        bucket="test-bucket",
        domain="https://cdn.example.com",
    )


def _wire_success(provider: QiniuStorageProvider, monkeypatch, store: dict) -> None:
    """Wire put_data/move/stat/delete against an in-memory dict standing in
    for bucket contents, so save_bytes/replace/exists/size_bytes behave
    consistently with each other within a single test."""

    def _put_data(up_token, key, data, mime_type=None, **kwargs):
        store[key] = data
        return {"key": key}, _FakeInfo(200)

    def _move(bucket, key, bucket_to, key_to, force="false"):
        if key not in store:
            return None, _FakeInfo(612)
        store[key_to] = store.pop(key)
        return None, _FakeInfo(200)

    def _stat(bucket, key):
        if key not in store:
            return None, _FakeInfo(612)
        return {"fsize": len(store[key])}, _FakeInfo(200)

    def _delete(bucket, key):
        if key in store:
            del store[key]
            return None, _FakeInfo(200)
        return None, _FakeInfo(612)

    monkeypatch.setattr(provider._qiniu, "put_data", _put_data)
    monkeypatch.setattr(provider._bucket_manager, "move", _move)
    monkeypatch.setattr(provider._bucket_manager, "stat", _stat)
    monkeypatch.setattr(provider._bucket_manager, "delete", _delete)


def test_qiniu_mode_download_one_writes_through_provider(monkeypatch) -> None:
    import app.media_download as media_download

    provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(provider, monkeypatch, store)

    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([jpeg_bytes])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), provider, "tenant-a", 42, "image", "sdk-1", timeout=5
    )

    assert outcome == "downloaded"
    assert detail == "tenants/tenant-a/images/42.jpg"
    assert store[detail] == jpeg_bytes
    assert "tenants/tenant-a/images/42.part" not in store
    assert provider.exists(detail) is True
    assert provider.size_bytes(detail) == len(jpeg_bytes)
    # RND-174 QA fix: file_size is the in-memory payload length, computed
    # without any post-publish provider.size_bytes() call on this path.
    assert file_size == len(jpeg_bytes)


def test_qiniu_mode_upload_failure_does_not_mark_downloaded(monkeypatch) -> None:
    import app.media_download as media_download

    provider = _make_qiniu_provider()
    monkeypatch.setattr(provider._qiniu, "put_data", lambda *a, **k: (None, _FakeInfo(579)))
    monkeypatch.setattr(
        provider._bucket_manager, "delete", lambda *a, **k: (None, _FakeInfo(612))
    )

    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([jpeg_bytes])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), provider, "tenant-a", 43, "image", "sdk-2", timeout=5
    )

    assert outcome == "failed"
    assert detail == "write_error"
    assert file_size is None


def test_qiniu_mode_unsupported_type_cleans_up_part_object(monkeypatch) -> None:
    import app.media_download as media_download

    provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(provider, monkeypatch, store)

    garbage = b"not-an-image-at-all"
    monkeypatch.setattr(media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([garbage]))

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), provider, "tenant-a", 44, "image", "sdk-3", timeout=5
    )

    assert outcome == "failed"
    assert detail == "unsupported_type"
    assert file_size is None
    assert store == {}  # the .part object was cleaned up, never left behind


def test_qiniu_mode_retry_is_idempotent_and_overwrites_final_key(monkeypatch) -> None:
    """A retried download of the same message must overwrite the same
    deterministic object key, not accumulate duplicate objects."""
    import app.media_download as media_download

    provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(provider, monkeypatch, store)

    first_bytes = b"\xff\xd8\xff" + b"first-body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([first_bytes])
    )
    outcome1, detail1, file_size1 = media_download.download_one(
        MagicMock(), MagicMock(), provider, "tenant-a", 45, "image", "sdk-4", timeout=5
    )
    assert outcome1 == "downloaded"
    assert file_size1 == len(first_bytes)

    second_bytes = b"\xff\xd8\xff" + b"retried-body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([second_bytes])
    )
    outcome2, detail2, file_size2 = media_download.download_one(
        MagicMock(), MagicMock(), provider, "tenant-a", 45, "image", "sdk-4", timeout=5
    )
    assert outcome2 == "downloaded"
    assert detail1 == detail2  # same deterministic final key
    assert store[detail2] == second_bytes  # overwritten in place, not duplicated
    assert len(store) == 1
    assert file_size2 == len(second_bytes)


def test_local_mode_unchanged_by_qiniu_addition(tmp_path, monkeypatch) -> None:
    """Regression: local mode (a Path passed as `storage`) behaves exactly
    as it did before the Qiniu provider was added."""
    import app.media_download as media_download

    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([jpeg_bytes])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 46, "image", "sdk-5", timeout=5
    )

    assert outcome == "downloaded"
    final_path = Path(detail)
    assert final_path.suffix == ".jpg"
    assert final_path.read_bytes() == jpeg_bytes
    assert not (final_path.parent / "46.part").exists()
    assert file_size == len(jpeg_bytes)


# ---------------------------------------------------------------------------
# RND-174 QA blocker #3: a post-publish metadata/stat failure must not be
# able to delete the just-published final object. Fixed by never calling a
# remote stat on the download_one success path — file_size is the in-memory
# payload length instead. These tests prove that structurally (the stat
# method literally is never invoked), not just "the bug doesn't currently
# reproduce".
# ---------------------------------------------------------------------------


def test_download_one_success_path_never_calls_size_bytes_or_exists(monkeypatch) -> None:
    import app.media_download as media_download

    provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(provider, monkeypatch, store)

    def _raise_if_called(*_a, **_k):
        raise AssertionError(
            "download_one's success path must never call a remote stat "
            "(size_bytes/exists) — file_size must come from the in-memory "
            "payload length"
        )

    monkeypatch.setattr(provider, "size_bytes", _raise_if_called)
    monkeypatch.setattr(provider, "exists", _raise_if_called)

    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    monkeypatch.setattr(media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([jpeg_bytes]))

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), provider, "tenant-a", 50, "image", "sdk-6", timeout=5
    )

    assert outcome == "downloaded"
    assert file_size == len(jpeg_bytes)


def _query_mock(all_result=None, count_result=0, first_result=None):
    q = MagicMock()
    q.outerjoin.return_value = q
    q.join.return_value = q
    q.filter.return_value = q
    q.order_by.return_value = q
    q.limit.return_value = q
    q.all.return_value = list(all_result or [])
    q.count.return_value = count_result
    q.first.return_value = first_result
    return q


def _run_qiniu_main_with_one_candidate(monkeypatch, tmp_path, provider, jpeg_or_garbage_chunks):
    """Same scaffolding as
    tests/test_download_wecom_media_once.py::_run_main_with_one_candidate,
    but wires script.get_media_storage_provider to hand back a
    pre-configured fake-SDK-boundary QiniuStorageProvider instead of
    reading STORAGE_LOCAL_PATH, so the full _run() flow (including the
    media_files persistence step) exercises the Qiniu path end to end for
    an "image" candidate through the unified script."""
    import scripts.download_wecom_media_once as script
    from app.db.models import MediaFile

    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", str(tmp_path / "media-download.lock"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("WECOM_ARCHIVE_SECRET", "secret")
    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "qiniu_kodo")

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    candidate_msg = SimpleNamespace(id=1, sdkfileid="sdk-secret-1", msgtype="image")
    media_file_row = MediaFile(sdkfileid="sdk-secret-1", archive_message_id=1, download_status="pending")

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.Tenant:
            return _query_mock(first_result=SimpleNamespace(lifecycle_status="active"))
        if model is MediaFile:
            return _query_mock(first_result=media_file_row)
        raise AssertionError(f"unexpected model queried: {model}")

    session = MagicMock()
    session.query.side_effect = _query

    @contextmanager
    def _fake_session(_engine):
        yield session

    monkeypatch.setattr(script, "create_engine", lambda _url: "fake-engine")
    monkeypatch.setattr(script, "Session", _fake_session)
    monkeypatch.setattr(script, "select_candidates", lambda *_a, **_k: ([candidate_msg], [], 1))
    # RND-200: this mock session only models the pre-existing --types
    # candidate path — the nested mixed/chatrecord candidate scan is a
    # separate code path covered by its own dedicated tests.
    monkeypatch.setattr(script, "select_nested_media_candidates", lambda *_a, **_k: ([], 0))
    monkeypatch.setattr(script, "get_media_storage_provider", lambda backend=None: provider)
    monkeypatch.setattr(script.wecom_sdk, "load_sdk", lambda _path: MagicMock())
    monkeypatch.setattr(script.wecom_sdk, "configure_sdk", lambda _lib: None)
    monkeypatch.setattr(script.wecom_sdk, "configure_sdk_media_data", lambda _lib: None)
    monkeypatch.setattr(script.wecom_sdk, "new_sdk", lambda _lib: 123)
    monkeypatch.setattr(script.wecom_sdk, "init_sdk", lambda _lib, _h, _c, _s: 0)
    monkeypatch.setattr(script.wecom_sdk, "destroy_sdk", lambda _lib, _h: None)
    monkeypatch.setattr(
        script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter(jpeg_or_garbage_chunks)
    )

    monkeypatch.setattr(sys, "argv", ["prog"])
    return script, media_file_row, session


def test_run_qiniu_success_persists_storage_backend_and_ref_without_stat(
    monkeypatch, tmp_path
) -> None:
    """End-to-end (main()) proof of the QA fix: a successful Qiniu upload
    marks the row downloaded, with storage_backend/storage_ref/file_size
    persisted from the download itself — never from a post-publish
    size_bytes() call, which is monkeypatched here to raise if invoked —
    driven through scripts/download_wecom_media_once.py, the sole
    downloader (RND-199), for an "image" candidate."""
    provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(provider, monkeypatch, store)

    def _raise_if_called(*_a, **_k):
        raise AssertionError("size_bytes must not be called on the success persistence path")

    monkeypatch.setattr(provider, "size_bytes", _raise_if_called)

    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    script, media_file_row, _session = _run_qiniu_main_with_one_candidate(
        monkeypatch, tmp_path, provider, [jpeg_bytes]
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.download_status == "downloaded"
    assert media_file_row.file_type == "image"
    assert media_file_row.storage_backend == "qiniu_kodo"
    assert media_file_row.storage_ref == "tenants/tenant-a/images/1.jpg"
    assert media_file_row.file_size == len(jpeg_bytes)
    # Qiniu-backed rows must never populate local_path — it is legacy/
    # local-only and must never be treated as an authoritative Qiniu ref.
    assert media_file_row.local_path is None
    assert store[media_file_row.storage_ref] == jpeg_bytes


def test_run_qiniu_db_commit_failure_still_removes_orphaned_object(monkeypatch, tmp_path) -> None:
    """The one deliberate exception: a genuine database commit failure
    after a successful publish still triggers cleanup of the now-
    unreferenced object — this must keep working even though the stat-
    triggered variant of the bug is gone."""
    provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(provider, monkeypatch, store)

    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    script, media_file_row, session = _run_qiniu_main_with_one_candidate(
        monkeypatch, tmp_path, provider, [jpeg_bytes]
    )

    commit_calls = {"count": 0}

    def _commit_side_effect():
        commit_calls["count"] += 1
        if commit_calls["count"] == 2:  # 1st = get_or_reset_media_file's reset commit
            raise RuntimeError("simulated DB commit failure")
        return None

    session.commit.side_effect = _commit_side_effect

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert "tenants/tenant-a/images/1.jpg" not in store
