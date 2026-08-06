"""Static deployment contracts for the RND-339 one-shot reconciliation."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "deploy/systemd/wecom-archive-reachability-check.service"
TIMER = ROOT / "deploy/systemd/wecom-archive-reachability-check.timer"
RUNBOOK = ROOT / "docs/reachability_automation_runbook.md"
WORKER = ROOT / "deploy/systemd/wecom-archive-worker.service"


def _pairs(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text().splitlines() if "=" in line and not line.lstrip().startswith("#")
    }


def test_reconciliation_service_matches_worker_oneshot_hardening_pattern() -> None:
    service, worker = _pairs(SERVICE), _pairs(WORKER)
    assert service["Type"] == "oneshot"
    for key in ("User", "WorkingDirectory", "EnvironmentFile", "NoNewPrivileges", "ProtectSystem", "ProtectHome", "ReadWritePaths", "ReadOnlyPaths", "PrivateTmp"):
        assert service[key] == worker[key]
    assert service["ExecStart"].endswith("scripts/run_reachability_automation_once.py reconcile")


def test_timer_is_daily_persistent_and_has_human_approved_offset() -> None:
    timer = _pairs(TIMER)
    assert timer["OnCalendar"] == "*-*-* 04:30:00"
    assert timer["Persistent"] == "true"
    assert timer["Unit"] == "wecom-archive-reachability-check.service"


def test_runbook_has_required_operations_and_no_secret() -> None:
    # Operational runbooks stay private (see scripts/public_allowlist.txt);
    # the systemd units above ship, the runbook describing how this team
    # operates them does not. The unit-level contracts in this module still
    # run everywhere — only this doc check has nothing to read.
    if not RUNBOOK.exists():
        pytest.skip("runbook not present (public snapshot)")
    text = RUNBOOK.read_text()
    for heading in ("Installation", "Observe", "Manual", "Local safe", "Failure", "Rollback"):
        assert heading in text
    assert "qwe123" not in text
    assert "04:30:00" in text
