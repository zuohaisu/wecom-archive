"""The RND-339 hook cannot change archive sync/decrypt exit truth."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_archive_worker_once.py"


def _module():
    spec = importlib.util.spec_from_file_location("archive_worker_rnd339", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("failing_label", ["sync_wecom_archive_once.py", "decrypt_wecom_messages_once.py"])
def test_sync_or_decrypt_failure_never_reaches_diagnostic(monkeypatch, tmp_path, failing_label) -> None:
    worker = _module()
    calls = []
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    monkeypatch.setattr(worker, "_run_script", lambda _path, label: (_ for _ in ()).throw(SystemExit(1)) if label == failing_label else calls.append(label))
    monkeypatch.setattr(worker, "_run_best_effort_reachability_automation", lambda: calls.append("diagnostic"))
    with pytest.raises(SystemExit) as result:
        worker.main()
    assert result.value.code == 1
    assert "diagnostic" not in calls


def test_successful_archive_stays_success_when_diagnostic_succeeds(monkeypatch, tmp_path):
    worker = _module()
    calls = []
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    monkeypatch.setattr(worker, "_run_script", lambda _path, label: calls.append(label))
    monkeypatch.setattr(worker, "_run_best_effort_reachability_automation", lambda: None)
    with pytest.raises(SystemExit) as result:
        worker.main()
    assert result.value.code == 0
    assert calls == ["sync_wecom_archive_once.py", "decrypt_wecom_messages_once.py"]


def test_best_effort_nonzero_and_lock_held_are_safe_noops(monkeypatch, tmp_path) -> None:
    worker = _module()
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs: MagicMock(returncode=1))
    worker._run_best_effort_reachability_automation()  # nonzero is no raise
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()))
    worker._run_best_effort_reachability_automation()  # exception is no raise
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    first = worker._acquire_lock(str(tmp_path / "worker.lock"))
    try:
        with pytest.raises(SystemExit) as result:
            worker._acquire_lock(str(tmp_path / "worker.lock"))
        assert result.value.code == 0
    finally:
        worker._release_lock(first)
