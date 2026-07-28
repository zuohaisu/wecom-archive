"""RND-172 event image sweep: bounded ordering, retry and safe logs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from app.db.models import MediaFile
from app.media_event_dispatch import run_recent_image_sweep, shutdown
from app.sdk import wecom_sdk
from tests.fakes import FakeWecomSdk, _TENANT_A, insert_archive_message, install_fake_sdk, worker_db  # noqa: F401


def _configure(monkeypatch, tmp_path, db, fake):
    from app import media_event_dispatch

    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_ENABLED", "true")
    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_BATCH_LIMIT", "20")
    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_RECENT_WINDOW_HOURS", "24")
    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_RETRY_COUNT", "3")
    monkeypatch.setenv("EVENT_MEDIA_DOWNLOAD_BACKOFF_SECONDS", "30")
    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", str(tmp_path / "media.lock"))
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path / "media"))
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "fake")
    monkeypatch.setenv("WECOM_CORP_ID", "corp-a")
    monkeypatch.setenv("WECOM_ARCHIVE_SECRET", "fake-secret")
    monkeypatch.setattr(media_event_dispatch, "get_engine", lambda: db.bind)
    install_fake_sdk(monkeypatch, fake, wecom_sdk)


def test_event_sweep_downloads_new_decrypted_image_and_logs_safely(worker_db, tmp_path, monkeypatch, caplog):
    fake = FakeWecomSdk()
    fake.set_media_chunks("image-safe-id", [b"\xff\xd8\xff\xe0" + b"jpeg"])
    _configure(monkeypatch, tmp_path, worker_db, fake)
    message = insert_archive_message(worker_db, tenant_id=_TENANT_A, decrypt_status="success", msgtype="image", sdkfileid="image-safe-id", msgtime=int(datetime.now().timestamp() * 1000))

    worker_db.rollback()  # release StaticPool's test transaction for the sweep Session
    with caplog.at_level(logging.INFO):
        summary = run_recent_image_sweep(_TENANT_A, "sync")

    row = worker_db.query(MediaFile).filter_by(archive_message_id=message.id).one()
    assert (summary.downloaded, row.download_status, row.download_attempts) == (1, "downloaded", 1)
    record = next(r.message for r in caplog.records if "event_media_download_sweep" in r.message)
    assert "downloaded=" in record and "failed=" in record and "duration_ms=" in record
    assert "image-safe-id" not in record
    shutdown()


def test_event_sweep_prioritizes_new_image_and_backoff_does_not_block_it(worker_db, tmp_path, monkeypatch):
    fake = FakeWecomSdk()
    fake.set_media_error("old-failed", RuntimeError("network"))
    fake.set_media_chunks("new-image", [b"\xff\xd8\xff\xe0" + b"new"])
    _configure(monkeypatch, tmp_path, worker_db, fake)
    now_ms = int(datetime.now().timestamp() * 1000)
    old = insert_archive_message(worker_db, tenant_id=_TENANT_A, decrypt_status="success", msgtype="image", sdkfileid="old-failed", msgtime=now_ms - 1000)
    new = insert_archive_message(worker_db, tenant_id=_TENANT_A, decrypt_status="success", msgtype="image", sdkfileid="new-image", msgtime=now_ms)
    worker_db.add(MediaFile(sdkfileid="old-failed", archive_message_id=old.id, tenant_id=_TENANT_A, download_status="failed", download_attempts=1, updated_at=datetime.now(timezone.utc)))
    worker_db.commit()

    worker_db.rollback()  # release StaticPool's test transaction for the sweep Session
    run_recent_image_sweep(_TENANT_A)
    new_row = worker_db.query(MediaFile).filter_by(archive_message_id=new.id).one()
    old_row = worker_db.query(MediaFile).filter_by(archive_message_id=old.id).one()
    assert new_row.download_status == "downloaded"
    assert old_row.download_attempts == 1

    old_row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=31)
    worker_db.commit()
    worker_db.rollback()
    run_recent_image_sweep(_TENANT_A)
    assert worker_db.query(MediaFile).filter_by(archive_message_id=old.id).one().download_attempts == 2
    shutdown()
