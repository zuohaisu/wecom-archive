"""RND-356 (T2) — static deployment contracts for the AI KB reindex timer."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "deploy/systemd/wecom-ai-kb-reindex.service"
TIMER = ROOT / "deploy/systemd/wecom-ai-kb-reindex.timer"
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
    # PrivateTmp deliberately does NOT follow the worker unit: the worker
    # sets PrivateTmp=false because it depends on a shared lock file under
    # the real /tmp (see wecom-archive-worker.service's own comment). The
    # reindex job has no shared lock file, so it keeps the stricter,
    # isolated PrivateTmp=true default used by the other lock-free oneshot
    # jobs (e.g. wecom-billing-notifications.service).
    assert service["PrivateTmp"] == "true"
    assert service["ExecStart"].endswith("scripts/run_ai_kb_reindex.py")


def test_timer_is_persistent_and_points_at_the_service() -> None:
    timer = _pairs(TIMER)
    assert timer["Persistent"] == "true"
    assert timer["Unit"] == "wecom-ai-kb-reindex.service"


def test_deployment_doc_lists_the_new_unit() -> None:
    text = DEPLOYMENT_DOC.read_text()
    assert "wecom-ai-kb-reindex" in text
