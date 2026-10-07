"""GH-106 static contracts for the production payment/billing runtime.

These tests inspect only versioned deployment assets. They never invoke
systemctl, connect to a database, or contact a payment provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANAGED_UNITS = ROOT / "deploy/systemd/MANAGED_UNITS"
DEPLOY_SCRIPT = ROOT / "scripts/deploy_server.sh"
RUNBOOK = ROOT / "docs/operations/payment-billing-runtime.md"

JOBS = {
    "wecom-billing-lifecycle": {
        "script": "scripts/process_billing_lifecycle_once.py",
        "schedule": "*:07/5",
    },
    "wecom-billing-notifications": {
        "script": "scripts/process_billing_notifications_once.py",
        "schedule": "*:02/5",
    },
    "wecom-payment-recovery": {
        "script": "scripts/process_payment_recovery_once.py recovery",
        "schedule": "*:04/5",
    },
    "wecom-payment-reconciliation": {
        "script": "scripts/process_payment_recovery_once.py reconciliation",
        "schedule": "*-*-* 02:30:00 UTC",
    },
}


def _managed() -> list[str]:
    return [
        line.strip()
        for line in MANAGED_UNITS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _pairs(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def test_required_billing_jobs_are_managed_with_existing_entrypoints() -> None:
    managed = _managed()
    for name, expected in JOBS.items():
        service_name = f"{name}.service"
        timer_name = f"{name}.timer"
        service = _pairs(ROOT / "deploy/systemd" / service_name)
        timer = _pairs(ROOT / "deploy/systemd" / timer_name)

        assert managed.count(service_name) == 1
        assert managed.count(timer_name) == 1
        assert managed.index(service_name) < managed.index(timer_name)
        assert service["Type"] == "oneshot"
        assert service["User"] == "wecomarchive"
        assert service["WorkingDirectory"] == "/srv/apps/wecom-archive-365/current/backend"
        assert service["EnvironmentFile"] == "/srv/apps/wecom-archive-365/current/backend/.env"
        assert service["ExecStart"].endswith(expected["script"])
        assert (ROOT / "backend" / expected["script"].split()[0]).is_file()
        assert timer["Unit"] == service_name
        assert timer["Persistent"] == "true"
        assert timer["OnCalendar"] == expected["schedule"]


def test_managed_unit_deployment_contract_installs_and_enables_only_timers() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'src="$DEPLOY_DIR/deploy/systemd/$unit"' in source
    assert '"$SYSTEMCTL_BIN" daemon-reload' in source
    assert "*.timer|*.path) ;;" in source
    assert 'enable --now "$unit"' in source
    assert "only trigger units are enabled directly" in source


@pytest.mark.requires_internal_ops_docs
def test_runbook_states_observability_rollback_and_provider_boundary() -> None:
    text = " ".join(RUNBOOK.read_text(encoding="utf-8").split())

    for name in JOBS:
        assert name in text
    for marker in (
        "payment_recovery processing_completed",
        "billing_lifecycle batch_completed",
        "billing_notifications processing_completed",
        "disable --now",
        "systemctl cat",
        "Alipay",
        "deferred",
    ):
        assert marker in text
