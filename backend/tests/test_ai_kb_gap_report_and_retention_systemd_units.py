"""RND-359 (T5) — static deployment contracts for the gap-report and
retention-sweep timers."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "deploy/systemd/wecom-archive-worker.service"
DEPLOYMENT_DOC = ROOT / "docs/DEPLOYMENT.md"

GAP_REPORT_SERVICE = ROOT / "deploy/systemd/wecom-ai-kb-gap-report.service"
GAP_REPORT_TIMER = ROOT / "deploy/systemd/wecom-ai-kb-gap-report.timer"
RETENTION_SERVICE = ROOT / "deploy/systemd/wecom-ai-retention-sweep.service"
RETENTION_TIMER = ROOT / "deploy/systemd/wecom-ai-retention-sweep.timer"

_HARDENING_KEYS = (
    "User",
    "WorkingDirectory",
    "EnvironmentFile",
    "NoNewPrivileges",
    "ProtectSystem",
    "ProtectHome",
    "ReadWritePaths",
    "ReadOnlyPaths",
)


def _pairs(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def test_gap_report_service_matches_worker_hardening_and_targets_its_script() -> None:
    service, worker = _pairs(GAP_REPORT_SERVICE), _pairs(WORKER)
    assert service["Type"] == "oneshot"
    for key in _HARDENING_KEYS:
        assert service[key] == worker[key]
    assert service["ExecStart"].endswith("scripts/run_ai_kb_gap_report.py")


def test_gap_report_timer_is_weekly_and_persistent() -> None:
    timer = _pairs(GAP_REPORT_TIMER)
    assert timer["OnCalendar"] == "Mon *-*-* 06:00:00"
    assert timer["Persistent"] == "true"
    assert timer["Unit"] == "wecom-ai-kb-gap-report.service"


def test_retention_sweep_service_matches_worker_hardening_and_targets_its_script() -> None:
    service, worker = _pairs(RETENTION_SERVICE), _pairs(WORKER)
    assert service["Type"] == "oneshot"
    for key in _HARDENING_KEYS:
        assert service[key] == worker[key]
    assert service["ExecStart"].endswith("scripts/run_ai_retention_sweep_once.py")


def test_retention_sweep_timer_is_daily_and_persistent() -> None:
    timer = _pairs(RETENTION_TIMER)
    assert timer["OnCalendar"] == "*-*-* 03:30:00"
    assert timer["Persistent"] == "true"
    assert timer["Unit"] == "wecom-ai-retention-sweep.service"


def test_deployment_doc_lists_both_new_units() -> None:
    text = DEPLOYMENT_DOC.read_text()
    assert "wecom-ai-kb-gap-report" in text
    assert "wecom-ai-retention-sweep" in text
