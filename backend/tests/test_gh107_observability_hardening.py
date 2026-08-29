"""GH-107 static contracts for job-failure alerting, external uptime
monitoring, and secret-safe logging wiring.

These tests inspect only versioned deployment/workflow/doc assets and
source files. They never invoke systemctl, contact GitHub Actions, or POST
to a real webhook.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANAGED_UNITS = ROOT / "deploy/systemd/MANAGED_UNITS"
DEPLOY_SCRIPT = ROOT / "scripts/deploy_server.sh"
ALERT_UNIT = ROOT / "deploy/systemd/wecom-job-failure-alert@.service"
UPTIME_WORKFLOW = ROOT / ".github/workflows/uptime-check.yml"
RUNBOOK = ROOT / "docs/operations/alerting.md"

ALERTED_UNITS = [
    "wecom-archive-worker",
    "wecom-external-contact-reconcile",
    "wecom-billing-lifecycle",
    "wecom-billing-notifications",
    "wecom-payment-recovery",
    "wecom-payment-reconciliation",
]


def _managed_lines() -> list[str]:
    return [
        line.strip()
        for line in MANAGED_UNITS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_job_failure_alert_unit_is_a_oneshot_notify_sh_wrapper() -> None:
    text = ALERT_UNIT.read_text(encoding="utf-8")

    assert "Type=oneshot" in text
    assert "EnvironmentFile=-/srv/apps/wecom-archive-365/current/backend/.env" in text
    assert "ssl-renew/notify.sh ERROR %i" in text
    # Never echoes journal content or a secret value into the alert message
    # (referencing the env var's name in a comment is fine; expanding it is not).
    assert "journalctl -u %i" in text
    assert "$ALERT_WEBHOOK_URL" not in text


def test_job_failure_alert_unit_is_managed_but_never_enabled_directly() -> None:
    managed = _managed_lines()

    assert managed.count("wecom-job-failure-alert@.service") == 1
    # No matching .timer/.path entry: this unit is only ever instantiated
    # on demand via another unit's OnFailure=, never enabled itself.
    assert "wecom-job-failure-alert@.timer" not in managed
    assert "wecom-job-failure-alert@.path" not in managed


def test_critical_scheduled_units_alert_on_failure() -> None:
    for name in ALERTED_UNITS:
        service = ROOT / f"deploy/systemd/{name}.service"
        text = service.read_text(encoding="utf-8")
        assert f"{name}.service" in _managed_lines()
        assert "OnFailure=wecom-job-failure-alert@%n.service" in text, name


def test_disk_and_backup_checks_keep_their_own_alerting_and_no_onfailure() -> None:
    # These already call notify.sh themselves on a richer WARN/failure path
    # (see docs/operations/alerting.md); an OnFailure= here would just be a
    # second, less specific alert for the same event.
    scripts = {
        "wecom-disk-usage-check": "scripts/disk_usage_check.sh",
        "wecom-backup": "scripts/backup_once.sh",
    }
    for name, script in scripts.items():
        service_text = (ROOT / f"deploy/systemd/{name}.service").read_text(encoding="utf-8")
        assert "OnFailure=wecom-job-failure-alert" not in service_text

        script_text = (ROOT / script).read_text(encoding="utf-8")
        assert "NOTIFY_BIN" in script_text
        assert "notify.sh" in script_text


def test_uptime_workflow_manual_checks_remain_available_while_schedule_is_paused() -> None:
    with UPTIME_WORKFLOW.open(encoding="utf-8") as fh:
        workflow = yaml.safe_load(fh)

    # GH-104 intentionally pauses scheduled external checks until the
    # pre-promotion work is complete. Manual workflow_dispatch remains the
    # supported verification path and must keep all check/alert wiring intact.
    assert "schedule" not in workflow[True]
    assert "workflow_dispatch" in workflow[True]
    assert workflow["env"]["PRODUCTION_URL"] == "https://archive.crowntime.cn/"
    assert workflow["env"]["READINESS_URL"] == "https://archive.crowntime.cn/health/ready"

    check_job = workflow["jobs"]["check"]
    assert check_job["runs-on"] == "ubuntu-latest"
    step_text = " ".join(str(step.get("run", "")) for step in check_job["steps"])
    assert "$PRODUCTION_URL" in step_text
    assert "$READINESS_URL" in step_text
    assert "secrets.ALERT_WEBHOOK_URL" in " ".join(
        str(step.get("env", {})) for step in check_job["steps"]
    )

    test_alert_job = workflow["jobs"]["test-alert"]
    assert "send_test_alert" in workflow[True]["workflow_dispatch"]["inputs"]
    assert "secrets.ALERT_WEBHOOK_URL" in str(test_alert_job["steps"][0]["env"])


def test_uptime_workflow_never_echoes_the_webhook_value() -> None:
    text = UPTIME_WORKFLOW.read_text(encoding="utf-8")
    # Referencing the env var's name in a human-readable message is fine;
    # expanding it (e.g. via `echo "$ALERT_WEBHOOK_URL"`) is not.
    for line in text.splitlines():
        if "echo" in line:
            assert "$ALERT_WEBHOOK_URL" not in line, line


def test_runbook_documents_every_alerted_unit_and_the_uptime_workflow() -> None:
    text = " ".join(RUNBOOK.read_text(encoding="utf-8").split())

    for name in ALERTED_UNITS:
        assert name in text
    for marker in (
        "wecom-job-failure-alert@.service",
        "notify.sh",
        "ALERT_WEBHOOK_URL",
        "uptime-check.yml",
        "/health/ready",
        "send_test_alert",
        "filter_secrets",
    ):
        assert marker in text, marker


def test_log_safety_helper_is_wired_into_every_httpx_using_entrypoint() -> None:
    sources = {
        "app/main.py": "app.log_safety",
        "app/services/wecom_org_authorization.py": "configure_secret_safe_logging",
        "app/services/external_contact_sync.py": "configure_secret_safe_logging()",
    }
    for rel_path, marker in sources.items():
        text = (ROOT / "backend" / rel_path).read_text(encoding="utf-8")
        assert marker in text, rel_path

    # The historical leak site: basicConfig(INFO) is still fine as long as
    # it never runs before the shared helper has already pinned httpx down.
    sync_text = (ROOT / "backend/app/services/external_contact_sync.py").read_text(
        encoding="utf-8"
    )
    configure_idx = sync_text.index("configure_secret_safe_logging()")
    basic_config_idx = sync_text.index("logging.basicConfig(level=logging.INFO)")
    assert configure_idx < basic_config_idx
