"""RND-422 repository-only contracts for archive-worker operability.

These tests intentionally exercise versioned units and fake SQLite tenant data
only. They never invoke systemctl, a real SDK, or a production database.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.html_helpers import _fmt_msgtime
from tests.fakes import insert_tenant, insert_tenant_wecom_config, worker_engine  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = ROOT / "backend/scripts/run_archive_worker_once.py"
MANAGED_UNITS = ROOT / "deploy/systemd/MANAGED_UNITS"
ARCHIVE_SERVICE = ROOT / "deploy/systemd/wecom-archive-worker.service"
ARCHIVE_TIMER = ROOT / "deploy/systemd/wecom-archive-worker.timer"
DEPLOY_SCRIPT = ROOT / "scripts/deploy_server.sh"
WORKER_RUNBOOK = ROOT / "docs/wecom_archive_worker_runbook.md"


def _worker_module():
    spec = importlib.util.spec_from_file_location("archive_worker_rnd422", WORKER_SCRIPT)
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


def test_archive_timer_is_managed_once_with_existing_five_minute_contract() -> None:
    managed = [
        line.strip()
        for line in MANAGED_UNITS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    timer = _pairs(ARCHIVE_TIMER)
    service = _pairs(ARCHIVE_SERVICE)

    assert managed.count("wecom-archive-worker.service") == 1
    assert managed.count("wecom-archive-worker.timer") == 1
    assert managed.index("wecom-archive-worker.service") < managed.index(
        "wecom-archive-worker.timer"
    )
    assert timer["Unit"] == "wecom-archive-worker.service"
    assert timer["OnCalendar"] == "*:0/5"
    assert service["Environment"] == "ARCHIVE_WORKER_TRIGGER_SOURCE=timer"


def test_managed_unit_deploy_contract_enables_only_the_existing_trigger_unit() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    # The deploy loop copies the exact manifest entries, reloads only after a
    # changed unit, and direct-enables only timer/path trigger units. A named
    # systemd unit has one definition, so this rules out a second worker timer
    # or a separately enabled oneshot archive service.
    assert 'src="$DEPLOY_DIR/deploy/systemd/$unit"' in source
    assert '"$SYSTEMCTL_BIN" daemon-reload' in source
    assert "*.timer|*.path) ;;" in source
    assert 'enable --now "$unit"' in source
    assert "only trigger units are enabled directly" in source


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("missing_config", "tenant_config_unavailable"),
        ("inactive_config", "tenant_config_unavailable"),
        ("missing_field", "tenant_credentials_missing_fields"),
        ("legacy_format", "tenant_credentials_legacy_format"),
        ("key_mismatch", "tenant_credentials_key_mismatch"),
        ("unknown", "tenant_credentials_unknown"),
    ],
)
def test_tenant_worker_credential_failures_are_classified_and_redacted(
    monkeypatch, tmp_path, worker_engine, capsys, case, expected_error
) -> None:
    tenant_id = "tenant-rnd422-sensitive"
    corp_id = "corp-rnd422-sensitive"
    secret = "secret-rnd422-sensitive"
    current_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", current_key)

    with Session(worker_engine) as db:
        if case != "missing_config":
            insert_tenant(db, tenant_id)
            config = insert_tenant_wecom_config(db, tenant_id, corp_id)
            if case == "inactive_config":
                config.is_active = False
            elif case == "missing_field":
                config.app_secret = ""
            elif case in {"key_mismatch", "unknown"}:
                stored_key = Fernet.generate_key().decode("ascii")
                monkeypatch.setenv("FIELD_ENCRYPTION_KEY", stored_key)
                config.set_app_secret(secret)
                monkeypatch.setenv(
                    "FIELD_ENCRYPTION_KEY",
                    current_key if case == "key_mismatch" else "not-a-fernet-key",
                )
            # ``legacy_format`` deliberately keeps the fixture's plaintext.
            db.commit()

    worker = _worker_module()
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setenv("WECOM_TENANT_ID", tenant_id)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.setattr(worker, "_tenant_engine", lambda: worker_engine)

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 1
    out = capsys.readouterr().out
    assert f"error_class={expected_error}" in out
    assert tenant_id not in out
    assert corp_id not in out
    assert secret not in out


@pytest.mark.requires_internal_ops_docs
def test_msgtime_runbook_contract_and_formatter_use_epoch_milliseconds() -> None:
    msgtime_ms = int(
        datetime(2026, 8, 27, 12, 34, 56, tzinfo=timezone.utc).timestamp() * 1000
    )

    assert "archive_messages.msgtime" in WORKER_RUNBOOK.read_text(encoding="utf-8")
    assert "epoch 毫秒" in WORKER_RUNBOOK.read_text(encoding="utf-8")
    # A seconds/milliseconds mix-up would render a 1970 date instead.
    assert _fmt_msgtime(msgtime_ms) == "2026-08-27 20:34:56"
