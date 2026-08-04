"""RND-343 scheduling contracts: archive-complete dispatch and timer safety."""
from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path

import pytest
from app.media_event_dispatch import _DEFAULT_SIGNAL_PATH, MediaWorkerDispatch

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_SCRIPT = ROOT / "backend/scripts/run_archive_worker_once.py"
ARCHIVE_SERVICE = ROOT / "deploy/systemd/wecom-archive-worker.service"
ARCHIVE_TIMER = ROOT / "deploy/systemd/wecom-archive-worker.timer"
MEDIA_SERVICE = ROOT / "deploy/systemd/wecom-archive-media-download.service"
MEDIA_TIMER = ROOT / "deploy/systemd/wecom-archive-media-download.timer"
MEDIA_EVENT_SERVICE = ROOT / "deploy/systemd/wecom-archive-media-event.service"
MEDIA_EVENT_PATH = ROOT / "deploy/systemd/wecom-archive-media-event.path"
ARCHIVE_5MIN_OVERRIDE = ROOT / "deploy/systemd/overrides/wecom-archive-worker-5min.conf"
MEDIA_5MIN_OVERRIDE = ROOT / "deploy/systemd/overrides/wecom-archive-media-download-5min.conf"


def _module():
    spec = importlib.util.spec_from_file_location("archive_worker_rnd343", ARCHIVE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pairs(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def test_successful_archive_dispatches_media_only_after_archive_lock_releases(
    monkeypatch, tmp_path, capsys
) -> None:
    worker = _module()
    calls: list[str] = []
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setenv("ARCHIVE_WORKER_TRIGGER_SOURCE", "callback")
    monkeypatch.setattr(worker, "_run_script", lambda _path, label: calls.append(label))
    monkeypatch.setattr(worker, "_run_best_effort_reachability_automation", lambda: calls.append("reachability"))

    def _media_dispatch() -> MediaWorkerDispatch:
        # A second fd can acquire the archive lock only after main()'s finally
        # released it, proving the media child cannot overlap archive work.
        fd = os.open(str(tmp_path / "archive.lock"), os.O_CREAT | os.O_RDWR, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        calls.append("media")
        return MediaWorkerDispatch.ACCEPTED

    monkeypatch.setattr(worker, "_request_media_worker_after_archive", _media_dispatch)

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 0
    assert calls == ["sync_wecom_archive_once.py", "decrypt_wecom_messages_once.py", "reachability", "media"]
    out = capsys.readouterr().out
    assert "archive_worker trigger_source=callback lifecycle=ended result=completed" in out
    assert "error_class=none" in out
    assert "completed_at=" in out


@pytest.mark.parametrize("failing_label", ["sync_wecom_archive_once.py", "decrypt_wecom_messages_once.py"])
def test_archive_failure_never_dispatches_media(monkeypatch, tmp_path, failing_label, capsys) -> None:
    worker = _module()
    dispatched: list[object] = []
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setattr(
        worker,
        "_run_script",
        lambda _path, label: (
            (_ for _ in ()).throw(worker.ArchiveWorkerExit(1, "child_worker_failed"))
            if label == failing_label
            else None
        ),
    )
    monkeypatch.setattr(worker, "_request_media_worker_after_archive", lambda: dispatched.append(1))

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 1
    assert dispatched == []
    out = capsys.readouterr().out
    assert "error_class=child_worker_failed" in out
    assert "lifecycle=ended result=failed" in out
    assert "completed_at=" in out


def test_archive_unexpected_failure_is_redacted_and_has_final_lifecycle(
    monkeypatch, tmp_path, capsys
) -> None:
    worker = _module()
    unsafe_detail = "SENTINEL private_path=/srv/private?sig=fixture-only"
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setattr(
        worker,
        "_run_script",
        lambda _path, _label: (_ for _ in ()).throw(RuntimeError(unsafe_detail)),
    )

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 1
    out = capsys.readouterr().out
    assert unsafe_detail not in out
    assert "error_class=unexpected_failure" in out
    assert "lifecycle=ended result=failed" in out
    assert "completed_at=" in out


def test_archive_lock_held_is_safe_noop_and_never_dispatches_media(monkeypatch, tmp_path, capsys) -> None:
    worker = _module()
    lock_path = tmp_path / "archive.lock"
    monkeypatch.setenv("WORKER_LOCK_PATH", str(lock_path))
    monkeypatch.setenv("ARCHIVE_WORKER_TRIGGER_SOURCE", "timer")
    dispatched: list[object] = []
    monkeypatch.setattr(worker, "_request_media_worker_after_archive", lambda: dispatched.append(1))
    holder = worker._acquire_lock(str(lock_path))
    try:
        with pytest.raises(SystemExit) as result:
            worker.main()
    finally:
        worker._release_lock(holder)

    assert result.value.code == 0
    assert dispatched == []
    out = capsys.readouterr().out
    assert "trigger_source=timer trigger=skipped-locked" in out
    assert "lifecycle=ended result=skipped" in out
    assert "error_class=none" in out
    assert "completed_at=" in out


def test_timer_templates_are_low_frequency_staggered_and_retryable() -> None:
    archive_timer = _pairs(ARCHIVE_TIMER)
    media_timer = _pairs(MEDIA_TIMER)
    archive_service = _pairs(ARCHIVE_SERVICE)
    media_service = _pairs(MEDIA_SERVICE)
    media_event_service = _pairs(MEDIA_EVENT_SERVICE)
    media_event_path = _pairs(MEDIA_EVENT_PATH)

    assert archive_timer["OnCalendar"] == "*:0/30"
    assert media_timer["OnCalendar"] == "*:15/30"
    assert archive_timer["Persistent"] == "true"
    assert media_timer["Persistent"] == "true"
    assert archive_timer["Unit"] == "wecom-archive-worker.service"
    assert media_timer["Unit"] == "wecom-archive-media-download.service"
    assert archive_service["Environment"] == "ARCHIVE_WORKER_TRIGGER_SOURCE=timer"
    assert "--retry" in media_service["ExecStart"]
    assert "--trigger-source timer" in media_service["ExecStart"]
    assert media_event_service["Type"] == "oneshot"
    for key in (
        "User",
        "WorkingDirectory",
        "EnvironmentFile",
        "NoNewPrivileges",
        "ProtectSystem",
        "ProtectHome",
        "ReadWritePaths",
        "ReadOnlyPaths",
        "PrivateTmp",
    ):
        assert media_event_service[key] == media_service[key]
    assert media_event_service["ExecStart"].endswith("--trigger-source archive-complete")
    assert "scripts/download_wecom_media_once.py" in media_event_service["ExecStart"]
    assert "--types" not in media_event_service["ExecStart"]
    assert media_event_path["PathChanged"] == _DEFAULT_SIGNAL_PATH
    assert media_event_path["Unit"] == "wecom-archive-media-event.service"


def test_versioned_5_minute_rollback_overrides_preserve_prior_offset() -> None:
    assert "OnCalendar=*:0/5" in ARCHIVE_5MIN_OVERRIDE.read_text(encoding="utf-8")
    assert "OnCalendar=*:2/5" in MEDIA_5MIN_OVERRIDE.read_text(encoding="utf-8")
