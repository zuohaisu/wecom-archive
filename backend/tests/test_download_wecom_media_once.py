"""
Tests for the unified WeCom media download pipeline — image (RND-147) +
voice/video/file/emotion (RND-199), consolidated onto one implementation
after independent QA rejected an earlier revision that kept image on a
second, parallel script/pipeline.

Scope: app/media_download.py (the single, msgtype-parameterized pipeline
— candidate selection, stale repair, download, persistence, locking,
retry) and scripts/download_wecom_media_once.py (its sole CLI entry
point). Covers every supported type, including image, through this one
implementation — there is no longer a separate image-only test file
(tests/test_download_wecom_image_media_once.py, which exercised the now-
deleted scripts/download_wecom_image_media_once.py, was retired; its
coverage is folded in here).

Run (from backend/):
    pytest tests/test_download_wecom_media_once.py -v
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as RealSession

from app.qiniu_storage import QiniuStorageProvider


def _compiled_sql(query) -> str:
    return str(query.statement.compile(compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# key_category_for_msgtype / target_storage_refs
# ---------------------------------------------------------------------------


def test_key_category_reuses_existing_media_type_key_categories() -> None:
    from app.media_download import key_category_for_msgtype

    assert key_category_for_msgtype("voice") == "voice"
    assert key_category_for_msgtype("video") == "videos"
    assert key_category_for_msgtype("file") == "files"


def test_key_category_emotion_uses_dedicated_override_not_registry_gated_table() -> None:
    """emotion is UNSUPPORTED in MessageTypeRegistry (intentional, see
    app.media_storage.SERVABLE_MEDIA_MSGTYPES's docstring) so it must never
    be added to MEDIA_TYPE_KEY_CATEGORIES (that table is validated against
    the registry at import time) — it gets its own override instead."""
    from app import media_storage
    from app.media_download import key_category_for_msgtype

    assert "emotion" not in media_storage.MEDIA_TYPE_KEY_CATEGORIES
    assert key_category_for_msgtype("emotion") == "emotions"


def test_target_storage_refs_are_deterministic_and_category_scoped() -> None:
    from app.media_download import target_storage_refs

    base, part = target_storage_refs("tenant-a", "voice", 42)
    assert base == "tenants/tenant-a/voice/42"
    assert part == "tenants/tenant-a/voice/42.part"

    base2, _part2 = target_storage_refs("tenant-a", "emotion", 42)
    assert base2 == "tenants/tenant-a/emotions/42"
    # Same archive_message_id, different category — must not collide.
    assert base != base2


# ---------------------------------------------------------------------------
# Candidate queries — multi-msgtype, tenant-scoped
# ---------------------------------------------------------------------------


def test_build_candidate_query_covers_every_requested_msgtype() -> None:
    from app.media_download import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(
            build_candidate_query(session, "tenant-a", frozenset({"voice", "video"}), retry=False)
        )

    assert "archive_messages.tenant_id = 'tenant-a'" in sql
    assert "archive_messages.msgtype IN ('voice', 'video')" in sql or (
        "'voice'" in sql and "'video'" in sql and "msgtype IN" in sql
    )
    assert "media_files" in sql


def test_build_candidate_query_single_type_set_still_uses_in_clause() -> None:
    from app.media_download import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(
            build_candidate_query(session, "tenant-a", frozenset({"emotion"}), retry=False)
        )
    assert "'emotion'" in sql


def test_build_downloaded_repair_query_is_tenant_scoped_and_downloaded_only() -> None:
    from app.media_download import build_downloaded_repair_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(
            build_downloaded_repair_query(session, "tenant-a", frozenset({"file", "emotion"}))
        )
    assert "media_files.download_status = 'downloaded'" in sql
    assert "archive_messages.tenant_id = 'tenant-a'" in sql


def test_build_candidate_query_image_only_shape_matches_pre_unification_behavior() -> None:
    """Regression: image, run through the now-unified pipeline with
    msgtypes={"image"}, must produce the same tenant-scoped/decrypt-status
    /msgtype/sdkfileid-not-null filter shape the retired image-only script
    used to build directly."""
    from app.media_download import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", frozenset({"image"}), retry=False))

    assert "archive_messages.tenant_id = 'tenant-a'" in sql
    assert "archive_messages.decrypt_status = 'success'" in sql
    assert "'image'" in sql
    assert "media_files" in sql  # outer join present
    assert "'pending'" in sql
    assert "'failed'" not in sql


def test_build_candidate_query_image_retry_includes_failed_but_never_downloaded() -> None:
    from app.media_download import build_candidate_query

    engine = create_engine("sqlite:///:memory:")
    with RealSession(engine) as session:
        sql = _compiled_sql(build_candidate_query(session, "tenant-a", frozenset({"image"}), retry=True))

    assert "'pending'" in sql
    assert "'failed'" in sql
    assert "'downloaded'" not in sql


# ---------------------------------------------------------------------------
# is_downloaded_media_file_stale — generic servability predicate
# ---------------------------------------------------------------------------


def test_is_downloaded_media_file_stale_false_for_servable_voice_file(tmp_path, monkeypatch) -> None:
    from app.media_download import is_downloaded_media_file_stale

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    voice = tmp_path / "clip.amr"
    voice.write_bytes(b"#!AMR fake amr body")
    media_file = SimpleNamespace(local_path=str(voice))
    assert is_downloaded_media_file_stale(media_file) is False


def test_is_downloaded_media_file_stale_true_for_missing_video_file(tmp_path, monkeypatch) -> None:
    from app.media_download import is_downloaded_media_file_stale

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    media_file = SimpleNamespace(local_path=str(tmp_path / "gone.mp4"))
    assert is_downloaded_media_file_stale(media_file) is True


# ---------------------------------------------------------------------------
# download_one — local storage, per-type signature gating, dedup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ext,body",
    [
        (".jpg", b"\xff\xd8\xff" + b"jpeg-body-bytes"),
        (".png", b"\x89PNG\r\n\x1a\n" + b"png-body-bytes"),
        (".gif", b"GIF89a" + b"gif-body-bytes"),
        (".webp", b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"webp-body-bytes"),
    ],
)
def test_download_one_image_success_local(ext, body, tmp_path, monkeypatch) -> None:
    """Image regression (RND-147, now served by the unified pipeline):
    every previously-supported image signature still downloads and
    publishes exactly as before consolidation."""
    import app.media_download as media_download

    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([body])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 999, "image", "sdk-image-1", timeout=5
    )

    assert outcome == "downloaded"
    final_path = Path(detail)
    assert final_path.suffix == ext
    assert final_path.read_bytes() == body
    assert not (final_path.parent / "999.part").exists()
    assert file_size == len(body)
    assert "tenants/tenant-a/images/999" in detail


def test_download_one_image_unsupported_type_rejected_and_part_cleaned_up(tmp_path, monkeypatch) -> None:
    import app.media_download as media_download

    garbage = b"totally-not-an-image-signature"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([garbage])
    )

    outcome, detail, _file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 998, "image", "sdk-image-2", timeout=5
    )

    assert outcome == "failed"
    assert detail == "unsupported_type"
    directory = tmp_path / "tenants" / "tenant-a" / "images"
    assert not (directory / "998.part").exists()
    assert not any(directory.glob("998.*"))


def test_download_one_voice_success_local(tmp_path, monkeypatch) -> None:
    import app.media_download as media_download

    amr_bytes = b"#!AMR" + b"voice-body-bytes"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([amr_bytes])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 100, "voice", "sdk-voice-1", timeout=5
    )

    assert outcome == "downloaded"
    final_path = Path(detail)
    assert final_path.suffix == ".amr"
    assert final_path.read_bytes() == amr_bytes
    assert not (final_path.parent / "100.part").exists()
    assert file_size == len(amr_bytes)
    assert "tenants/tenant-a/voice/100.amr" in detail


def test_download_one_video_success_local(tmp_path, monkeypatch) -> None:
    import app.media_download as media_download

    mp4_bytes = b"\x00\x00\x00\x18ftypmp42" + b"rest-of-video"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([mp4_bytes])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 101, "video", "sdk-video-1", timeout=5
    )

    assert outcome == "downloaded"
    assert Path(detail).suffix == ".mp4"
    assert Path(detail).read_bytes() == mp4_bytes
    assert file_size == len(mp4_bytes)


def test_download_one_file_success_local(tmp_path, monkeypatch) -> None:
    import app.media_download as media_download

    pdf_bytes = b"%PDF-1.4" + b"fake pdf body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([pdf_bytes])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 102, "file", "sdk-file-1", timeout=5
    )

    assert outcome == "downloaded"
    assert Path(detail).suffix == ".pdf"
    assert Path(detail).read_bytes() == pdf_bytes
    assert file_size == len(pdf_bytes)


def test_download_one_emotion_success_local(tmp_path, monkeypatch) -> None:
    """emotion messages carry ordinary image bytes (stickers/animated
    emoji) — the pipeline accepts an image signature for msgtype
    "emotion" and files it under the "emotions" key category."""
    import app.media_download as media_download

    gif_bytes = b"GIF89a" + b"fake gif body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([gif_bytes])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 103, "emotion", "sdk-emotion-1", timeout=5
    )

    assert outcome == "downloaded"
    assert Path(detail).suffix == ".gif"
    assert "tenants/tenant-a/emotions/103.gif" in detail
    assert file_size == len(gif_bytes)


@pytest.mark.parametrize(
    "msgtype,bad_bytes",
    [
        ("voice", b"%PDF-1.4 this is a pdf not a voice clip"),
        ("video", b"#!AMR this is voice not video"),
        ("file", b"\xff\xd8\xff this is a jpeg not a generic file"),
    ],
)
def test_download_one_rejects_mismatched_signature_category(msgtype, bad_bytes, tmp_path, monkeypatch) -> None:
    """A recognized-but-wrong-category signature (e.g. real PDF bytes
    arriving for a "voice" message) must be rejected as unsupported_type,
    never silently accepted under the wrong category — content is
    verified against the expected category, not just "is it any known
    format"."""
    import app.media_download as media_download

    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([bad_bytes])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 200, msgtype, "sdk-mismatch", timeout=5
    )

    assert outcome == "failed"
    assert detail == "unsupported_type"
    assert file_size is None
    directory = tmp_path / "tenants" / "tenant-a"
    assert not any(directory.rglob("200.part"))


def test_download_one_unrecognized_bytes_rejected_and_cleaned_up(tmp_path, monkeypatch) -> None:
    import app.media_download as media_download

    monkeypatch.setattr(
        media_download.wecom_sdk,
        "iter_media_chunks",
        lambda *a, **k: iter([b"totally-unrecognized-garbage"]),
    )

    outcome, detail, _file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 201, "file", "sdk-garbage", timeout=5
    )

    assert outcome == "failed"
    assert detail == "unsupported_type"
    directory = tmp_path / "tenants" / "tenant-a" / "files"
    assert not any(directory.glob("201.*"))


def test_download_one_empty_payload_is_failure(tmp_path, monkeypatch) -> None:
    import app.media_download as media_download

    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([])
    )
    outcome, detail, _file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 202, "voice", "sdk-empty", timeout=5
    )
    assert outcome == "failed"
    assert detail == "empty_payload"


def test_download_one_sdk_error_leaves_no_part_file(tmp_path, monkeypatch) -> None:
    import app.media_download as media_download

    def _raise(*_a, **_k):
        raise media_download.wecom_sdk.SdkMediaError("boom")

    monkeypatch.setattr(media_download.wecom_sdk, "iter_media_chunks", _raise)

    outcome, detail, _file_size = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 203, "video", "sdk-err", timeout=5
    )
    assert outcome == "failed"
    assert detail == "sdk_error"
    directory = tmp_path / "tenants" / "tenant-a" / "videos"
    assert not (directory / "203.part").exists()


def test_download_one_retry_overwrites_same_deterministic_key(tmp_path, monkeypatch) -> None:
    import app.media_download as media_download

    first = b"#!AMR" + b"first-body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([first])
    )
    outcome1, detail1, _size1 = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 300, "voice", "sdk-retry", timeout=5
    )
    assert outcome1 == "downloaded"

    second = b"#!AMR" + b"retried-body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([second])
    )
    outcome2, detail2, size2 = media_download.download_one(
        MagicMock(), MagicMock(), tmp_path, "tenant-a", 300, "voice", "sdk-retry", timeout=5
    )
    assert outcome2 == "downloaded"
    assert detail1 == detail2
    assert Path(detail2).read_bytes() == second
    assert size2 == len(second)


# ---------------------------------------------------------------------------
# download_one — Qiniu-backed (mocked SDK boundary), mirrors
# tests/test_qiniu_worker_integration.py's approach for the image script.
# ---------------------------------------------------------------------------


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


def test_qiniu_backed_download_one_writes_through_provider(monkeypatch) -> None:
    import app.media_download as media_download

    provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(provider, monkeypatch, store)

    mp4_bytes = b"\x00\x00\x00\x18ftypmp42" + b"video-body"
    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([mp4_bytes])
    )

    outcome, detail, file_size = media_download.download_one(
        MagicMock(), MagicMock(), provider, "tenant-a", 400, "video", "sdk-q-video", timeout=5
    )

    assert outcome == "downloaded"
    assert detail == "tenants/tenant-a/videos/400.mp4"
    assert store[detail] == mp4_bytes
    assert "tenants/tenant-a/videos/400.part" not in store
    assert file_size == len(mp4_bytes)


def test_qiniu_backed_download_one_unsupported_type_cleans_up_part(monkeypatch) -> None:
    import app.media_download as media_download

    provider = _make_qiniu_provider()
    store: dict = {}
    _wire_success(provider, monkeypatch, store)

    monkeypatch.setattr(
        media_download.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter([b"garbage-bytes"])
    )

    outcome, detail, _file_size = media_download.download_one(
        MagicMock(), MagicMock(), provider, "tenant-a", 401, "file", "sdk-q-garbage", timeout=5
    )

    assert outcome == "failed"
    assert detail == "unsupported_type"
    assert store == {}


# ---------------------------------------------------------------------------
# get_or_reset_media_file — msgtype-independent identity, shared with the
# image script's own (separately tested) version.
# ---------------------------------------------------------------------------


def test_get_or_reset_media_file_creates_pending_row() -> None:
    from app.media_download import get_or_reset_media_file

    session = MagicMock()
    q = MagicMock()
    q.filter.return_value = q
    q.first.return_value = None
    session.query.return_value = q

    get_or_reset_media_file(session, "tenant-a", "sdk-1", 100)

    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.download_status == "pending"


# ---------------------------------------------------------------------------
# select_candidates — dedup: an already-"downloaded"+servable row must not
# be re-selected as a fresh candidate, and must not be flagged for repair.
# ---------------------------------------------------------------------------


def _make_sqlite_engine(tmp_path, name):
    from sqlalchemy import text

    engine = create_engine(f"sqlite:///{tmp_path / name}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE archive_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msgid TEXT NOT NULL, seq INTEGER NOT NULL,
                    publickey_ver INTEGER NOT NULL,
                    raw_encrypted_payload TEXT, encrypt_random_key TEXT NOT NULL,
                    encrypt_chat_msg TEXT NOT NULL,
                    decrypt_status TEXT NOT NULL DEFAULT 'pending',
                    decrypted_payload TEXT, structured_content TEXT,
                    content_text TEXT, msgtype TEXT, sender TEXT, roomid TEXT,
                    msgtime INTEGER, tolist TEXT, sdkfileid TEXT,
                    tenant_id TEXT, created_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE media_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, sdkfileid TEXT NOT NULL,
                    archive_message_id INTEGER NOT NULL, file_type TEXT,
                    local_path TEXT, oss_key TEXT, storage_backend TEXT,
                    storage_ref TEXT, file_size INTEGER,
                    download_status TEXT NOT NULL DEFAULT 'pending',
                    migration_status TEXT, migration_attempted_at TEXT,
                    migration_error TEXT, bucket TEXT, mime_type TEXT,
                    checksum_sha256 TEXT, tenant_id TEXT, created_at TEXT,
                    updated_at TEXT, UNIQUE(tenant_id, sdkfileid)
                )
                """
            )
        )
    return engine


def test_select_candidates_skips_already_downloaded_servable_rows(tmp_path, monkeypatch) -> None:
    from app.db.models import ArchiveMessage, MediaFile
    from app.media_download import select_candidates

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path, "dedup-voice.db")

    session = RealSession(engine)
    good_path = tmp_path / "good.amr"
    good_path.write_bytes(b"#!AMR ok")

    session.add(
        ArchiveMessage(
            id=1, msgid="m-1", seq=1, publickey_ver=1, encrypt_random_key="k",
            encrypt_chat_msg="c", decrypt_status="success", msgtype="voice",
            sdkfileid="sdk-1", tenant_id="tenant-a",
        )
    )
    session.add(
        MediaFile(
            sdkfileid="sdk-1", archive_message_id=1, tenant_id="tenant-a",
            file_type="voice", local_path=str(good_path), download_status="downloaded",
        )
    )
    session.commit()

    actionable, repairs, total = select_candidates(
        session, "tenant-a", frozenset({"voice"}), retry=False, limit=10
    )
    assert actionable == []
    assert repairs == []
    assert total == 0


def test_select_candidates_skips_already_downloaded_servable_image_row(tmp_path, monkeypatch) -> None:
    """Image regression: the exact same dedup guarantee the retired
    image-only script provided must hold through the unified pipeline."""
    from app.db.models import ArchiveMessage, MediaFile
    from app.media_download import select_candidates

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path, "dedup-image.db")

    session = RealSession(engine)
    good_path = tmp_path / "good.jpg"
    good_path.write_bytes(b"\xff\xd8\xff ok")

    session.add(
        ArchiveMessage(
            id=1, msgid="m-1", seq=1, publickey_ver=1, encrypt_random_key="k",
            encrypt_chat_msg="c", decrypt_status="success", msgtype="image",
            sdkfileid="sdk-1", tenant_id="tenant-a",
        )
    )
    session.add(
        MediaFile(
            sdkfileid="sdk-1", archive_message_id=1, tenant_id="tenant-a",
            file_type="image", local_path=str(good_path), download_status="downloaded",
        )
    )
    session.commit()

    actionable, repairs, total = select_candidates(
        session, "tenant-a", frozenset({"image"}), retry=False, limit=10
    )
    assert actionable == []
    assert repairs == []
    assert total == 0


def test_select_candidates_repairs_stale_downloaded_image_row(tmp_path, monkeypatch) -> None:
    """Image regression: a "downloaded" row whose file has gone missing on
    disk must still be found by the stale-repair scan (RND-147 QA fix),
    unchanged by unification."""
    from app.db.models import ArchiveMessage, MediaFile
    from app.media_download import select_candidates

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    engine = _make_sqlite_engine(tmp_path, "repair-image.db")

    session = RealSession(engine)

    session.add(
        ArchiveMessage(
            id=1, msgid="m-1", seq=1, publickey_ver=1, encrypt_random_key="k",
            encrypt_chat_msg="c", decrypt_status="success", msgtype="image",
            sdkfileid="sdk-1", tenant_id="tenant-a",
        )
    )
    session.add(
        MediaFile(
            sdkfileid="sdk-1", archive_message_id=1, tenant_id="tenant-a",
            file_type="image", local_path=str(tmp_path / "missing.jpg"),
            download_status="downloaded",
        )
    )
    session.commit()

    actionable, repairs, total = select_candidates(
        session, "tenant-a", frozenset({"image"}), retry=False, limit=10
    )
    assert actionable == []
    assert total == 0
    assert len(repairs) == 1
    assert repairs[0][0].id == 1


# ---------------------------------------------------------------------------
# scripts/download_wecom_media_once.py — CLI: --types parsing, count-only,
# lock behavior, and an end-to-end pending -> downloaded transition.
# ---------------------------------------------------------------------------


def test_parse_types_defaults_to_every_supported_type() -> None:
    import scripts.download_wecom_media_once as script

    assert script._parse_types(None) == script.GENERIC_DOWNLOAD_MSGTYPES
    assert script._parse_types("") == script.GENERIC_DOWNLOAD_MSGTYPES


def test_parse_types_narrows_to_requested_subset() -> None:
    import scripts.download_wecom_media_once as script

    assert script._parse_types("voice,video") == frozenset({"voice", "video"})


def test_parse_types_accepts_image_as_a_unified_pipeline_type() -> None:
    """Image is now one of the pipeline's own supported --types values —
    not a separate script's exclusive concern."""
    import scripts.download_wecom_media_once as script

    assert script._parse_types("image") == frozenset({"image"})


def test_parse_types_rejects_unsupported_type(capsys) -> None:
    import scripts.download_wecom_media_once as script

    with pytest.raises(SystemExit) as exc:
        script._parse_types("location")
    assert exc.value.code == 1
    assert "unsupported message type" in capsys.readouterr().out


def test_main_exits_cleanly_without_db_access_when_lock_held(tmp_path, monkeypatch, capsys) -> None:
    import fcntl
    import os

    import scripts.download_wecom_media_once as script

    lock_path = str(tmp_path / "media.lock")
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", lock_path)
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


def test_count_only_performs_no_writes(tmp_path, monkeypatch, capsys) -> None:
    import scripts.download_wecom_media_once as script

    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", str(tmp_path / "media-download.lock"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    candidate_msg = SimpleNamespace(id=1, sdkfileid="sdk-should-not-be-printed", msgtype="voice")

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
        if model is script.ArchiveMessage:
            return _query_mock(count_result=3)
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
    monkeypatch.setattr(script, "select_candidates", lambda *_a, **_k: ([candidate_msg], [], 3))
    monkeypatch.setattr(
        script.wecom_sdk,
        "load_sdk",
        MagicMock(side_effect=AssertionError("must not load SDK in count-only mode")),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--count-only"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    captured = capsys.readouterr()
    assert "candidate_total: 3" in captured.out
    assert "sdk-should-not-be-printed" not in captured.out


def _run_main_with_one_candidate(monkeypatch, tmp_path, msgtype, chunks, extra_args=None):
    import scripts.download_wecom_media_once as script
    from app.db.models import MediaFile

    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", str(tmp_path / "media-download.lock"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("WECOM_ARCHIVE_SECRET", "secret")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))

    tenant_row = SimpleNamespace(tenant_id="tenant-a")
    candidate_msg = SimpleNamespace(id=1, sdkfileid="sdk-secret-1", msgtype=msgtype)
    media_file_row = MediaFile(sdkfileid="sdk-secret-1", archive_message_id=1, download_status="pending")

    def _query(model):
        if model is script.TenantWecomConfig:
            return _query_mock(first_result=tenant_row)
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
    monkeypatch.setattr(script.wecom_sdk, "load_sdk", lambda _path: MagicMock())
    monkeypatch.setattr(script.wecom_sdk, "configure_sdk", lambda _lib: None)
    monkeypatch.setattr(script.wecom_sdk, "configure_sdk_media_data", lambda _lib: None)
    monkeypatch.setattr(script.wecom_sdk, "new_sdk", lambda _lib: 123)
    monkeypatch.setattr(script.wecom_sdk, "init_sdk", lambda _lib, _h, _c, _s: 0)
    monkeypatch.setattr(script.wecom_sdk, "destroy_sdk", lambda _lib, _h: None)
    monkeypatch.setattr(script.wecom_sdk, "iter_media_chunks", lambda *a, **k: iter(chunks))

    monkeypatch.setattr(sys, "argv", ["prog"] + (extra_args or []))
    return script, media_file_row, session


def test_main_image_pending_to_downloaded_transition(tmp_path, monkeypatch, capsys) -> None:
    """Image regression: end-to-end through the unified CLI script, image
    still transitions pending -> downloaded exactly as the retired
    image-only script's own end-to-end test proved."""
    jpeg_bytes = b"\xff\xd8\xff" + b"jpeg-body"
    script, media_file_row, _session = _run_main_with_one_candidate(
        monkeypatch, tmp_path, "image", [jpeg_bytes]
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.download_status == "downloaded"
    assert media_file_row.file_type == "image"
    assert media_file_row.oss_key is None
    assert media_file_row.local_path is not None
    assert Path(media_file_row.local_path).read_bytes() == jpeg_bytes
    assert media_file_row.file_size == len(jpeg_bytes)

    captured = capsys.readouterr()
    assert "sdk-secret-1" not in captured.out
    assert media_file_row.local_path not in captured.out


def test_main_image_pending_to_failed_transition_on_unsupported_type(tmp_path, monkeypatch, capsys) -> None:
    garbage = b"not-a-real-image"
    script, media_file_row, _session = _run_main_with_one_candidate(
        monkeypatch, tmp_path, "image", [garbage]
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.download_status == "failed"
    assert media_file_row.local_path is None

    captured = capsys.readouterr()
    assert "sdk-secret-1" not in captured.out


def test_main_voice_pending_to_downloaded_transition(tmp_path, monkeypatch, capsys) -> None:
    amr_bytes = b"#!AMR" + b"voice-body"
    script, media_file_row, _session = _run_main_with_one_candidate(
        monkeypatch, tmp_path, "voice", [amr_bytes]
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.download_status == "downloaded"
    assert media_file_row.file_type == "voice"
    assert Path(media_file_row.local_path).read_bytes() == amr_bytes
    assert media_file_row.file_size == len(amr_bytes)

    captured = capsys.readouterr()
    assert "sdk-secret-1" not in captured.out


def test_main_emotion_pending_to_downloaded_transition(tmp_path, monkeypatch) -> None:
    gif_bytes = b"GIF89a" + b"emoji-body"
    script, media_file_row, _session = _run_main_with_one_candidate(
        monkeypatch, tmp_path, "emotion", [gif_bytes]
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.download_status == "downloaded"
    assert media_file_row.file_type == "emotion"
    assert "emotions" in media_file_row.local_path
    assert Path(media_file_row.local_path).read_bytes() == gif_bytes


def test_main_unsupported_type_transitions_to_failed(tmp_path, monkeypatch) -> None:
    script, media_file_row, _session = _run_main_with_one_candidate(
        monkeypatch, tmp_path, "video", [b"not-a-real-video"]
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    assert media_file_row.download_status == "failed"
    assert media_file_row.local_path is None
