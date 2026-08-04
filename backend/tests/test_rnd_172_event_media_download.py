"""RND-172 regression coverage after RND-343 generalised its dispatch seam."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from app.db.models import MediaFile
from app.media_event_dispatch import MediaWorkerDispatch, dispatch_media_worker

from tests.fakes import (
    _TENANT_A,
    insert_archive_message,
    insert_tenant,
    insert_tenant_wecom_config,
    worker_db,  # noqa: F401 -- pytest fixture registration
)


def _configure(monkeypatch, db) -> None:
    from app import media_event_dispatch

    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_ENABLED", "true")
    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_BATCH_LIMIT", "20")
    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_RECENT_WINDOW_HOURS", "24")
    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_RETRY_COUNT", "3")
    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_BACKOFF_SECONDS", "30")
    monkeypatch.setenv("WECOM_CORP_ID", "corp-a")
    monkeypatch.setattr(media_event_dispatch, "get_engine", lambda: db.bind)
    insert_tenant(db, _TENANT_A)
    insert_tenant_wecom_config(db, _TENANT_A, "corp-a")


@pytest.mark.parametrize("msgtype", ["image", "voice", "video", "file", "emotion"])
def test_archive_complete_dispatch_accepts_each_generic_top_level_media_type(
    worker_db, monkeypatch, msgtype
) -> None:
    from app import media_event_dispatch

    _configure(monkeypatch, worker_db)
    insert_archive_message(
        worker_db,
        tenant_id=_TENANT_A,
        decrypt_status="success",
        msgtype=msgtype,
        sdkfileid=f"safe-{msgtype}",
        msgtime=int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    worker_db.rollback()  # release StaticPool's transaction for preflight Session
    signals: list[object] = []
    monkeypatch.setattr(
        media_event_dispatch,
        "_signal_media_worker",
        lambda: signals.append(1) or True,
    )

    assert dispatch_media_worker() is MediaWorkerDispatch.ACCEPTED
    # The bounded event signal starts the existing generic worker through the
    # systemd path unit; it carries no image-only implementation or IDs.
    assert signals == [1]


def test_archive_complete_dispatch_accepts_nested_generic_media(worker_db, monkeypatch) -> None:
    from app import media_event_dispatch

    _configure(monkeypatch, worker_db)
    insert_archive_message(
        worker_db,
        tenant_id=_TENANT_A,
        decrypt_status="success",
        msgtype="mixed",
        msgtime=int(datetime.now(timezone.utc).timestamp() * 1000),
        structured_content={
            "media_refs": [
                {"path": "0", "type": "voice", "sdkfileid": "safe-nested-voice"},
                {"path": "1", "type": "emotion", "sdkfileid": "safe-nested-emotion"},
            ]
        },
    )
    worker_db.rollback()
    signal = MagicMock(return_value=True)
    monkeypatch.setattr(media_event_dispatch, "_signal_media_worker", signal)

    assert dispatch_media_worker() is MediaWorkerDispatch.ACCEPTED
    signal.assert_called_once()


def test_event_signal_is_one_empty_coalescing_file(monkeypatch, tmp_path) -> None:
    from app import media_event_dispatch

    signal_path = tmp_path / "media-dispatch.trigger"
    monkeypatch.setattr(media_event_dispatch, "_DEFAULT_SIGNAL_PATH", str(signal_path))

    assert media_event_dispatch._signal_media_worker() is True
    assert signal_path.exists()
    assert signal_path.read_bytes() == b""


def test_archive_complete_dispatch_does_not_signal_for_no_media(worker_db, monkeypatch, caplog) -> None:
    from app import media_event_dispatch

    _configure(monkeypatch, worker_db)
    worker_db.rollback()
    signal = MagicMock(return_value=True)
    monkeypatch.setattr(media_event_dispatch, "_signal_media_worker", signal)

    with caplog.at_level(logging.INFO, logger=media_event_dispatch.__name__):
        assert dispatch_media_worker() is MediaWorkerDispatch.NO_WORK

    signal.assert_not_called()
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "trigger=no-work" in log_text
    assert "tenant-a" not in log_text


def test_archive_complete_dispatch_failure_is_safe_and_non_raising(worker_db, monkeypatch, caplog) -> None:
    from app import media_event_dispatch

    _configure(monkeypatch, worker_db)
    sentinel = "SENTINEL_TRACEBACK sdkfileid=unsafe-path"
    insert_archive_message(
        worker_db,
        tenant_id=_TENANT_A,
        decrypt_status="success",
        msgtype="file",
        sdkfileid="safe-file",
        msgtime=int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    worker_db.rollback()
    monkeypatch.setattr(
        media_event_dispatch,
        "_signal_media_worker",
        lambda: (_ for _ in ()).throw(RuntimeError(sentinel)),
    )

    with caplog.at_level(logging.INFO, logger=media_event_dispatch.__name__):
        assert dispatch_media_worker() is MediaWorkerDispatch.FAILED

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "trigger=dispatch-failed" in log_text
    assert sentinel not in log_text


def test_generic_retry_policy_defers_old_failure_without_blocking_new_media(worker_db, monkeypatch) -> None:
    import scripts.download_wecom_media_once as script

    _configure(monkeypatch, worker_db)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    old = insert_archive_message(
        worker_db,
        tenant_id=_TENANT_A,
        decrypt_status="success",
        msgtype="image",
        sdkfileid="old-failed",
        msgtime=now_ms - 1000,
    )
    fresh = insert_archive_message(
        worker_db,
        tenant_id=_TENANT_A,
        decrypt_status="success",
        msgtype="video",
        sdkfileid="fresh-video",
        msgtime=now_ms,
    )
    worker_db.add(
        MediaFile(
            sdkfileid="old-failed",
            archive_message_id=old.id,
            tenant_id=_TENANT_A,
            download_status="failed",
            download_attempts=1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    worker_db.commit()

    selected, nested, skipped = script._filter_retryable_candidates(
        worker_db, _TENANT_A, [old, fresh], [], retry_count=3, backoff_seconds=30
    )
    assert [message.sdkfileid for message in selected] == ["fresh-video"]
    assert nested == []
    assert skipped == 1

    failed_row = worker_db.query(MediaFile).filter_by(sdkfileid="old-failed").one()
    failed_row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=31)
    worker_db.commit()
    selected, _nested, skipped = script._filter_retryable_candidates(
        worker_db, _TENANT_A, [old, fresh], [], retry_count=3, backoff_seconds=30
    )
    assert {message.sdkfileid for message in selected} == {"old-failed", "fresh-video"}
    assert skipped == 0
