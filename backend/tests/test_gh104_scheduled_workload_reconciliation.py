"""GH-104 static contracts for the canonical production scheduled-workload
manifest.

These tests inspect only versioned deployment assets (deploy/systemd/*,
scripts/assert_scheduled_workloads.sh, docs) and run the assertion script
itself as a subprocess against the real repository tree. They never invoke
systemctl against a real host, connect to a database, or contact WeCom/
payment/AI providers.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy/systemd/WORKLOAD_MANIFEST"
MANAGED_UNITS = ROOT / "deploy/systemd/MANAGED_UNITS"
ASSERT_SCRIPT = ROOT / "scripts/assert_scheduled_workloads.sh"
DEPLOY_SYSTEMD = ROOT / "deploy/systemd"
MANIFEST_DOC = ROOT / "docs/operations/scheduled-workload-manifest.md"
DEPLOYMENT_DOC = ROOT / "docs/DEPLOYMENT.md"

VALID_CLASSIFICATIONS = {
    "required",
    "deferred",
    "manual-oneshot",
    "deprecated",
    "static-helper",
    "template",
    "out-of-scope",
}

FIELDS = (
    "unit",
    "classification",
    "repo_status",
    "auto_install",
    "auto_enable",
    "trigger_type",
    "destructive",
    "notes",
)


def _rows() -> list[dict[str, str]]:
    rows = []
    for lineno, line in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = line.split("|")
        assert len(parts) == len(FIELDS), f"manifest line {lineno} has {len(parts)} fields, expected {len(FIELDS)}: {line!r}"
        rows.append(dict(zip(FIELDS, parts)))
    return rows


def _managed_lines() -> list[str]:
    return [
        line.strip()
        for line in MANAGED_UNITS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _row(unit: str) -> dict[str, str]:
    for row in _rows():
        if row["unit"] == unit:
            return row
    raise AssertionError(f"no WORKLOAD_MANIFEST row for {unit}")


# ── 1. Canonical manifest parses successfully ───────────────────────────


def test_manifest_parses_and_every_row_has_a_valid_classification() -> None:
    rows = _rows()
    assert rows, "manifest must not be empty"
    for row in rows:
        assert row["classification"] in VALID_CLASSIFICATIONS, row
        assert row["repo_status"] in {"present", "absent"}, row
        assert row["auto_install"] in {"true", "false"}, row
        assert row["auto_enable"] in {"true", "false"}, row
        assert row["destructive"] in {"true", "false"}, row


# ── 2/3. Every required unit exists in repo; no required unit references
# a missing ExecStart target ─────────────────────────────────────────────


def test_every_required_unit_with_repo_status_present_exists_on_disk() -> None:
    for row in _rows():
        if row["classification"] not in {"required", "static-helper"}:
            continue
        if row["repo_status"] != "present":
            continue
        assert (DEPLOY_SYSTEMD / row["unit"]).is_file(), row["unit"]


def test_required_service_execstart_targets_exist(tmp_path: Path) -> None:
    for row in _rows():
        if row["classification"] not in {"required", "static-helper"}:
            continue
        if row["repo_status"] != "present" or not row["unit"].endswith(".service"):
            continue
        text = (DEPLOY_SYSTEMD / row["unit"]).read_text(encoding="utf-8")
        execstart = next(
            (line.split("=", 1)[1] for line in text.splitlines() if line.startswith("ExecStart=")),
            "",
        )
        assert execstart, row["unit"]
        if " -m " in f" {execstart} ":
            continue  # module invocation (e.g. `-m app.services.x`) has no script file to check
        script_words = [
            w
            for w in execstart.split()
            if (w.endswith(".py") or w.endswith(".sh"))
        ]
        assert script_words, f"{row['unit']}: no .py/.sh word in ExecStart={execstart!r}"


# ── 4. Required timer/path is marked auto-enabled ───────────────────────


def test_required_triggers_are_auto_install_and_auto_enable() -> None:
    for row in _rows():
        if row["classification"] != "required" or row["repo_status"] != "present":
            continue
        if row["unit"].endswith((".timer", ".path")):
            assert row["auto_install"] == "true", row
            assert row["auto_enable"] == "true", row
            assert row["unit"] in _managed_lines(), row["unit"]


# ── 5/6. Deferred/manual units are never auto-enabled; destructive units
# cannot accidentally enter the auto-enable set ─────────────────────────


def test_non_required_units_are_never_auto_installed_or_enabled() -> None:
    managed = set(_managed_lines())
    for row in _rows():
        if row["classification"] in {"required", "static-helper"}:
            continue
        assert row["auto_install"] == "false", row
        assert row["auto_enable"] == "false", row
        assert row["unit"] not in managed, f"{row['unit']} must not be in MANAGED_UNITS"


def test_destructive_units_are_structurally_barred_from_auto_enable() -> None:
    destructive = [row for row in _rows() if row["destructive"] == "true"]
    assert destructive, "expected at least the message purge/cleanup units to be flagged destructive"
    for row in destructive:
        assert row["auto_install"] == "false", row
        assert row["auto_enable"] == "false", row
        assert row["classification"] != "required", row


@pytest.mark.parametrize("unit", ["wecom-message-purge.service", "wecom-message-purge.timer",
                                   "wecom-message-cleanup.service", "wecom-message-cleanup.timer"])
def test_message_deletion_units_are_flagged_destructive_and_deferred(unit: str) -> None:
    row = _row(unit)
    assert row["classification"] == "deferred"
    assert row["destructive"] == "true"


# ── 7. media-download fallback cadence remains intended 30min ──────────


def test_media_download_timer_cadence_is_30_minutes() -> None:
    text = (DEPLOY_SYSTEMD / "wecom-archive-media-download.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*:15/30" in text, "media-download must stay on the 30-minute (:15/:45) fallback cadence, not regress to 5-minute polling"
    row = _row("wecom-archive-media-download.timer")
    assert row["classification"] == "required"
    assert row["auto_install"] == "true"
    assert row["auto_enable"] == "true"


# ── 8. media-event path is required ─────────────────────────────────────


def test_media_event_path_is_required_and_managed() -> None:
    row = _row("wecom-archive-media-event.path")
    assert row["classification"] == "required"
    assert row["auto_install"] == "true"
    assert row["auto_enable"] == "true"
    assert "wecom-archive-media-event.path" in _managed_lines()
    assert "wecom-archive-media-event.service" in _managed_lines()


# ── 9. external-contact-refresh runtime is required ─────────────────────


def test_external_contact_refresh_path_is_primary_and_timer_is_fallback() -> None:
    path_row = _row("wecom-external-contact-refresh.path")
    timer_row = _row("wecom-external-contact-refresh.timer")
    service_row = _row("wecom-external-contact-refresh.service")
    for row in (path_row, timer_row, service_row):
        assert row["classification"] == "required", row

    timer_text = (DEPLOY_SYSTEMD / "wecom-external-contact-refresh.timer").read_text(encoding="utf-8")
    # The timer is a boot + retry recovery mechanism, not a low-latency
    # primary trigger — it must not be re-tuned into a sub-minute poller.
    assert "OnUnitInactiveSec=" in timer_text
    assert "OnCalendar=" not in timer_text

    path_text = (DEPLOY_SYSTEMD / "wecom-external-contact-refresh.path").read_text(encoding="utf-8")
    assert "PathChanged=" in path_text

    managed = _managed_lines()
    for unit in ("wecom-external-contact-refresh.service", "wecom-external-contact-refresh.path", "wecom-external-contact-refresh.timer"):
        assert unit in managed, unit


# ── 10. backup/resource-check no longer silently unmanaged ─────────────


@pytest.mark.parametrize("stem", ["wecom-backup", "wecom-disk-usage-check"])
def test_backup_and_disk_usage_check_are_now_managed(stem: str) -> None:
    service_row = _row(f"{stem}.service")
    timer_row = _row(f"{stem}.timer")
    assert service_row["classification"] == "required"
    assert timer_row["classification"] == "required"
    assert timer_row["auto_enable"] == "true"
    managed = _managed_lines()
    assert f"{stem}.service" in managed
    assert f"{stem}.timer" in managed


def test_backup_cadence_and_retention_semantics_are_unchanged() -> None:
    # GH-104 closes a "required but unmanaged" drift only; #105 owns
    # off-host DR. Cadence must stay exactly what production already runs.
    text = (DEPLOY_SYSTEMD / "wecom-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:17:00" in text
    text = (DEPLOY_SYSTEMD / "wecom-disk-usage-check.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*:9/15" in text


# ── 11. SSL production renewal path is represented ──────────────────────


def test_wildcard_ssl_renewal_gap_is_explicitly_tracked() -> None:
    rows = [row for row in _rows() if "ssl-renew-wildcard" in row["unit"]]
    assert rows, "the production wildcard SSL renewal workload must have an explicit manifest row"
    row = rows[0]
    assert row["classification"] == "required"
    assert row["repo_status"] == "absent"
    assert row["auto_install"] == "false"
    assert row["auto_enable"] == "false"
    assert MANIFEST_DOC.is_file(), "the wildcard SSL reproducibility gap must be documented"
    doc_text = MANIFEST_DOC.read_text(encoding="utf-8")
    assert "wildcard" in doc_text.lower()
    assert "renew-wildcard.sh" in doc_text


def test_ssl_template_is_marked_template_only_and_not_deleted() -> None:
    service_row = _row("qiniu-ssl-renew@.service")
    timer_row = _row("qiniu-ssl-renew@.timer")
    assert service_row["classification"] == "template"
    assert timer_row["classification"] == "template"
    assert (DEPLOY_SYSTEMD / "qiniu-ssl-renew@.service").is_file()
    assert (DEPLOY_SYSTEMD / "qiniu-ssl-renew@.timer").is_file()


# ── 12. Deprecated telegram relay cannot re-enter deployment ────────────


def test_telegram_relay_is_absent_from_every_deployable_surface() -> None:
    assert not list(DEPLOY_SYSTEMD.glob("*telegram*"))
    assert "telegram" not in MANAGED_UNITS.read_text(encoding="utf-8").lower()
    assert "telegram" not in MANIFEST.read_text(encoding="utf-8").lower()
    assert MANIFEST_DOC.is_file()
    assert "telegram" in MANIFEST_DOC.read_text(encoding="utf-8").lower()


# ── 13. MANAGED_UNITS / manifest / deploy script cannot silently drift ──


def test_assert_scheduled_workloads_script_passes_in_repo_mode() -> None:
    result = subprocess.run(
        [sys.executable if False else "bash", str(ASSERT_SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout


def test_every_managed_unit_has_auto_install_true_in_manifest() -> None:
    manifest_by_unit = {row["unit"]: row for row in _rows()}
    for unit in _managed_lines():
        assert unit in manifest_by_unit, f"MANAGED_UNITS lists {unit} but WORKLOAD_MANIFEST has no row for it"
        assert manifest_by_unit[unit]["auto_install"] == "true"


def test_deploy_script_still_treats_managed_units_as_the_sole_allowlist() -> None:
    # GH-104 deliberately does not rewrite scripts/deploy_server.sh's
    # step-10 sync mechanism (lower blast radius) — it only grows
    # MANAGED_UNITS' content under the new manifest's authority. This
    # guards against a future edit accidentally switching step 10 to scan
    # deploy/systemd/*.timer directly, which would silently auto-enable
    # deferred/destructive units the moment a matching file exists.
    source = (ROOT / "scripts/deploy_server.sh").read_text(encoding="utf-8")
    assert 'manifest="$DEPLOY_DIR/deploy/systemd/MANAGED_UNITS"' in source
    assert "*.timer|*.path) ;;" in source
