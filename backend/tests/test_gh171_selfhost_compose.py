from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
SCHEDULER_PATH = ROOT / "backend/scripts/run_selfhost_worker_scheduler.py"


def _load_scheduler() -> Any:
    spec = importlib.util.spec_from_file_location(
        "selfhost_worker_scheduler", SCHEDULER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCHEDULER = _load_scheduler()


def _compose() -> dict[str, Any]:
    with COMPOSE_PATH.open(encoding="utf-8") as source:
        return yaml.safe_load(source)


def test_compose_isolated_postgres_and_selfhost_services() -> None:
    config = _compose()
    services = config["services"]

    assert services["db"]["image"] == "postgres:16"
    assert "ports" not in services["db"]
    assert services["db"]["healthcheck"]
    assert services["db"]["volumes"] == ["postgres_data:/var/lib/postgresql/data"]
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]

    for service_name in ("migrate", "web", "worker"):
        service = services[service_name]
        assert service["environment"]["APP_EDITION"] == "selfhost"
        assert service["env_file"] == [".env"]
        assert "SETTINGS_ENCRYPTION_KEY" in service["environment"]
        assert "FIELD_ENCRYPTION_KEY" in service["environment"]
        assert "@db:5432/" in service["environment"]["DATABASE_URL"]

    assert services["web"]["ports"] == [
        "${WEB_BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-8035}:8035"
    ]
    assert (
        services["web"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert services["worker"]["command"] == [
        "python",
        "scripts/run_selfhost_worker_scheduler.py",
    ]
    assert services["worker"]["environment"]["ARCHIVE_WORKER_TRIGGER_SOURCE"] == "timer"
    assert set(services["worker"]["volumes"]) == set(services["web"]["volumes"])


def test_compose_and_image_do_not_embed_host_or_cloud_infrastructure() -> None:
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8").lower()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for forbidden in (
        "crowntime",
        "/srv/apps/",
        "/var/run/docker.sock",
        "alipay",
        "wechat_pay",
        "billing_notification",
    ):
        assert forbidden not in compose_text
    assert "USER 10001:10001" in dockerfile
    assert "backend/vendor/" not in dockerfile
    assert "backend/.env" in dockerignore
    assert "backend/vendor/" in dockerignore


def test_environment_template_separates_cloud_only_values() -> None:
    text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    marker = "# Cloud edition only — self-host deployments do not need these values."
    assert text.count(marker) == 1
    core, cloud = text.split(marker, maxsplit=1)
    assignments = re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE)
    assert len(assignments) == len(set(assignments))

    for required in (
        "APP_EDITION=selfhost",
        "DATABASE_URL=",
        "DB_PASSWORD=",
        "FIELD_ENCRYPTION_KEY=",
        "SETTINGS_ENCRYPTION_KEY=",
        "WECOM_CORP_ID=",
    ):
        assert required in core

    cloud_only = (
        "WECOM_THIRD_PARTY_SUITE_ID=",
        "WECOM_THIRD_PARTY_SUITE_SECRET=",
        "SELF_SERVICE_TRIAL_ENTRY_ENABLED=",
        "WECHAT_PAY_ENABLED=",
        "ALIPAY_ENABLED=",
        "RESEND_API_KEY=",
        "BILLING_NOTIFICATION_BATCH_SIZE=",
        "BILLING_LIFECYCLE_BATCH_LIMIT=",
    )
    for setting in cloud_only:
        assert setting not in core
        assert setting in cloud


def test_worker_schedule_contains_only_selfhost_fallback_jobs() -> None:
    jobs = {job.name: job for job in SCHEDULER.SELFHOST_JOBS}

    assert jobs["archive-sync"].interval_seconds == 5 * 60
    assert jobs["external-contact-refresh"].interval_seconds == 15 * 60
    assert jobs["external-contact-reconcile"].interval_seconds == 24 * 60 * 60
    assert jobs["media-download"].interval_seconds == 30 * 60
    assert jobs["export-jobs"].interval_seconds == 5 * 60
    assert jobs["ai-kb-reindex"].interval_seconds == 60 * 60
    assert jobs["ai-retention-sweep"].interval_seconds == 24 * 60 * 60
    assert not any(
        token in name
        for name in jobs
        for token in ("billing", "payment", "purge", "cleanup", "backup")
    )


def test_scheduler_wakes_signal_job_and_never_overlaps_runs(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> int:
        signal_path = tmp_path / "external-contact-refresh.trigger"
        monkeypatch.setenv("TEST_SIGNAL_PATH", str(signal_path))
        job = SCHEDULER.ScheduledJob("test-refresh", 3600, (), "TEST_SIGNAL_PATH")
        stop_event = asyncio.Event()
        calls = 0
        running = False

        async def run_once(_job, _stop_event) -> int:
            nonlocal calls, running
            assert not running
            running = True
            try:
                calls += 1
                await asyncio.sleep(0)
                if calls == 1:
                    signal_path.touch()
                else:
                    stop_event.set()
                return 0
            finally:
                running = False

        await asyncio.wait_for(
            SCHEDULER._run_job(job, stop_event, run_once=run_once), timeout=2
        )
        return calls

    assert asyncio.run(scenario()) == 2


def test_unreadable_signal_path_preserves_periodic_fallback(
    tmp_path, monkeypatch
) -> None:
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("TEST_SIGNAL_PATH", str(blocking_file / "signal"))
    job = SCHEDULER.ScheduledJob("test-refresh", 3600, (), "TEST_SIGNAL_PATH")

    assert SCHEDULER._signal_signature(job) is None


def test_scheduler_logs_job_failure_without_exception_message(caplog) -> None:
    async def scenario() -> None:
        job = SCHEDULER.ScheduledJob("test-failure", 3600, ())
        stop_event = asyncio.Event()

        async def fail_once(_job, _stop_event) -> int:
            stop_event.set()
            raise RuntimeError("do not log credential-like exception detail")

        with caplog.at_level(logging.ERROR, logger="selfhost_worker_scheduler"):
            await SCHEDULER._run_job(job, stop_event, run_once=fail_once)

    asyncio.run(scenario())
    assert "error_class=RuntimeError" in caplog.text
    assert "credential-like exception detail" not in caplog.text


def test_scheduler_terminates_running_child_on_shutdown() -> None:
    async def scenario() -> int:
        job = SCHEDULER.ScheduledJob(
            "test-child", 3600, ("-c", "import time; time.sleep(60)")
        )
        stop_event = asyncio.Event()
        task = asyncio.create_task(SCHEDULER._run_command(job, stop_event))
        await asyncio.sleep(0.1)
        stop_event.set()
        return await asyncio.wait_for(task, timeout=3)

    assert asyncio.run(scenario()) != 0


def test_media_signal_uses_configured_compose_path(tmp_path, monkeypatch) -> None:
    from app import media_event_dispatch

    signal_path = tmp_path / "run" / "media-event.trigger"
    monkeypatch.setenv("MEDIA_EVENT_SIGNAL_PATH", str(signal_path))

    assert media_event_dispatch._signal_media_worker() is True
    assert signal_path.is_file()


def test_external_contact_signal_uses_configured_compose_path(
    tmp_path, monkeypatch
) -> None:
    from app.services import external_contact_refresh_trigger

    signal_path = tmp_path / "run" / "external-contact-refresh.trigger"
    monkeypatch.setenv("EXTERNAL_CONTACT_REFRESH_SIGNAL_PATH", str(signal_path))

    assert external_contact_refresh_trigger._signal_refresh_worker() is True
    assert signal_path.is_file()


def test_media_signal_fails_closed_on_invalid_configured_path(
    tmp_path, monkeypatch
) -> None:
    from app import media_event_dispatch

    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("MEDIA_EVENT_SIGNAL_PATH", str(blocking_file / "signal"))
    fallback_path = tmp_path / "default-signal"
    monkeypatch.setattr(
        media_event_dispatch, "_DEFAULT_SIGNAL_PATH", str(fallback_path)
    )

    assert media_event_dispatch._signal_media_worker() is False
    assert not fallback_path.exists()


def test_runtime_image_installs_the_voice_transcode_dependency() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    install_command = next(
        line for line in dockerfile.splitlines() if "apt-get install" in line
    )
    assert "ffmpeg" in install_command
    assert "RUN ffmpeg -version" in dockerfile


def test_runtime_image_contains_every_required_knowledge_base_source(tmp_path) -> None:
    from shutil import copyfile, copytree

    from app.ai_kb.manifest_schema import load_manifest

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "COPY --chown=app:app docs/kb/ /app/docs/kb/" in dockerfile
    assert (
        "COPY --chown=app:app docs/ARCHITECTURE.md /app/docs/ARCHITECTURE.md"
        in dockerfile
    )
    for rule in (
        "!docs/",
        "!docs/ARCHITECTURE.md",
        "!docs/kb/",
        "!docs/kb/public/",
        "!docs/kb/public/**",
        "!docs/kb/customer/",
        "!docs/kb/customer/**",
    ):
        assert rule in dockerignore

    manifest_source = ROOT / "backend/app/ai_kb/manifest.json"
    manifest_in_image = tmp_path / "backend/app/ai_kb/manifest.json"
    manifest_in_image.parent.mkdir(parents=True)
    copyfile(manifest_source, manifest_in_image)
    copytree(ROOT / "docs/kb", tmp_path / "docs/kb")
    (tmp_path / "docs").mkdir(exist_ok=True)
    copyfile(ROOT / "docs/ARCHITECTURE.md", tmp_path / "docs/ARCHITECTURE.md")

    entries = load_manifest(manifest_in_image, repo_root=tmp_path)
    assert len(entries) == 12
