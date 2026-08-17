"""RND-359 (T5) — static deployment contracts for the AI KB eval timer."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "deploy/systemd/wecom-ai-kb-eval.service"
TIMER = ROOT / "deploy/systemd/wecom-ai-kb-eval.timer"
WORKER = ROOT / "deploy/systemd/wecom-archive-worker.service"
DEPLOYMENT_DOC = ROOT / "docs/DEPLOYMENT.md"


def _pairs(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def test_service_matches_worker_oneshot_hardening_pattern() -> None:
    service, worker = _pairs(SERVICE), _pairs(WORKER)
    assert service["Type"] == "oneshot"
    for key in (
        "User",
        "WorkingDirectory",
        "EnvironmentFile",
        "NoNewPrivileges",
        "ProtectSystem",
        "ProtectHome",
        "ReadWritePaths",
        "ReadOnlyPaths",
    ):
        assert service[key] == worker[key]
    assert service["PrivateTmp"] == "true"  # no shared lock file, unlike the worker (see reindex unit's own test)
    assert service["ExecStart"].endswith("scripts/run_ai_kb_eval.py")


def test_timer_is_daily_persistent_and_points_at_the_service() -> None:
    timer = _pairs(TIMER)
    assert timer["OnCalendar"] == "*-*-* 05:00:00"
    assert timer["Persistent"] == "true"
    assert timer["Unit"] == "wecom-ai-kb-eval.service"


def test_deployment_doc_lists_the_new_unit() -> None:
    text = DEPLOYMENT_DOC.read_text()
    assert "wecom-ai-kb-eval" in text
