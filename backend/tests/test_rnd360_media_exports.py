"""RND-360 ZIP generation, durable state, notice and expiry coverage."""

from __future__ import annotations

import hashlib
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.db.base import Base
from app.db.models import AdminUser, ExportJob, Tenant
from app.db.session import get_db
from app.main import create_app
from app.media_storage import MediaObjectNotFound
from app.services import export_jobs


NOW = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)


def _factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            AdminUser.__table__,
            ExportJob.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id="tenant-a", name="A", slug="tenant-a"))
        db.add(
            AdminUser(
                id="owner-a",
                tenant_id="tenant-a",
                wecom_user_id="owner",
                role="owner",
                status="active",
                email="owner@example.com",
            )
        )
        db.commit()
    return factory


def test_export_api_is_owner_only() -> None:
    factory = _factory()
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    member = SimpleNamespace(id="member", role="admin", email="member@example.com")
    app.dependency_overrides[get_current_user] = lambda: (member, "tenant-a")
    with TestClient(app) as client:
        assert client.get("/api/admin/exports/jobs").status_code == 403


def test_zip_contains_original_bytes_and_safe_manifest_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "tenants" / "tenant-a" / "images" / "original.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixed-original-media")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    row = SimpleNamespace(
        id=7,
        archive_message_id=42,
        file_type="../../image",
        local_path=str(source),
        storage_backend="local",
        storage_ref=str(source),
        file_size=source.stat().st_size,
        checksum_sha256=checksum,
    )
    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "local")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    monkeypatch.setattr(export_jobs, "_media_rows", lambda _db, _tenant: [row])
    fake_db = SimpleNamespace(rollback=lambda: None)
    job = SimpleNamespace(id="job-a", tenant_id="tenant-a")

    artifact = export_jobs._build_media_zip(fake_db, job)

    assert artifact.storage_backend == "local"
    archive_path = Path(artifact.storage_ref)
    assert archive_path.is_file()
    with ZipFile(archive_path) as archive:
        names = archive.namelist()
        media_name = next(name for name in names if name.startswith("media/"))
        assert ".." not in media_name
        assert archive.read(media_name) == b"fixed-original-media"
        manifest = archive.read("manifest.csv").decode("utf-8-sig")
        assert media_name in manifest
        assert str(source) not in manifest


def test_worker_marks_ready_emails_authenticated_link_and_expires_in_seven_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = _factory()
    artifact_path = None
    captured = {}
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    monkeypatch.setenv("ADMIN_DOMAIN", "archive.example.com")
    def build(_db, job):
        nonlocal artifact_path
        artifact_path = (
            tmp_path / "tenants" / "tenant-a" / "exports" / f"{job.id}.zip"
        )
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_bytes(b"PK-fixed")
        return export_jobs._Artifact(
            storage_backend="local",
            storage_ref=str(artifact_path),
            file_size=artifact_path.stat().st_size,
            checksum_sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        )

    monkeypatch.setattr(export_jobs, "_build_media_zip", build)

    def send(to_email, export_link, expires_at, locale):
        captured.update(
            email=to_email,
            link=export_link,
            expires_at=expires_at,
            locale=locale,
        )
        return True

    monkeypatch.setattr(export_jobs, "send_export_ready_email", send)
    with factory() as db:
        export_jobs.create_media_export_job(
            db, tenant_id="tenant-a", requested_by="owner-a", now=NOW
        )
        db.commit()
        summary = export_jobs.run_export_maintenance_once(db, now=NOW)
        job = db.scalar(select(ExportJob))
        assert summary.ready == summary.notifications_sent == 1
        assert job.status == "ready"
        assert job.expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(days=7)
        assert captured["email"] == "owner@example.com"
        assert captured["link"].startswith(
            "https://archive.example.com/admin/exports?job="
        )
        assert "/api/admin/exports/jobs/" not in captured["link"]

        expired = export_jobs.run_export_maintenance_once(
            db, now=NOW + timedelta(days=7)
        )
        db.refresh(job)
        assert expired.expired == 1
        assert job.status == "expired"
        assert job.storage_ref is None
        assert artifact_path is not None
        assert artifact_path.exists() is False


def test_generation_retries_then_fails_without_ready_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory()
    monkeypatch.setattr(
        export_jobs,
        "_build_media_zip",
        lambda _db, _job: (_ for _ in ()).throw(MediaObjectNotFound("missing")),
    )
    with factory() as db:
        export_jobs.create_media_export_job(
            db, tenant_id="tenant-a", requested_by="owner-a", now=NOW
        )
        db.commit()
        first = export_jobs.run_export_maintenance_once(db, now=NOW)
        second = export_jobs.run_export_maintenance_once(db, now=NOW)
        third = export_jobs.run_export_maintenance_once(db, now=NOW)
        job = db.scalar(select(ExportJob))
        assert first.retried == second.retried == 1
        assert third.failed == 1
        assert job.status == "failed"
        assert job.last_error == "media_missing"
        assert job.storage_ref is None


def test_job_view_distinguishes_retryable_and_final_email_failure() -> None:
    job = SimpleNamespace(
        id="job-a",
        kind="media_zip",
        format="zip",
        status="ready",
        file_size=123,
        requested_at=NOW,
        started_at=NOW,
        completed_at=NOW,
        expires_at=NOW + timedelta(days=7),
        notification_status="failed",
        notification_attempts=1,
    )

    assert export_jobs.export_job_view(job, now=NOW)["notification_retryable"] is True
    job.notification_attempts = export_jobs.MAX_NOTIFICATION_ATTEMPTS
    assert export_jobs.export_job_view(job, now=NOW)["notification_retryable"] is False


def test_0046_migration_creates_and_removes_export_jobs(tmp_path: Path) -> None:
    migration_path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "0046_rnd360_export_jobs.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0046", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    engine = create_engine(f"sqlite:///{tmp_path / 'exports-migration.db'}")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE admin_users (id VARCHAR(36) PRIMARY KEY)")
        )
        operations = Operations(MigrationContext.configure(connection))
        migration.op = operations
        migration.upgrade()
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(export_jobs)"))
        }
        assert {"status", "expires_at", "storage_ref", "notification_status"} <= columns
        migration.downgrade()
        assert connection.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='export_jobs'"
            )
        ).fetchone() is None


def test_export_worker_systemd_units_are_bounded_and_staggered() -> None:
    repo = Path(__file__).resolve().parents[2]
    service = (repo / "deploy/systemd/wecom-export-jobs.service").read_text()
    timer = (repo / "deploy/systemd/wecom-export-jobs.timer").read_text()
    script = (repo / "backend/scripts/process_export_jobs_once.py").read_text()

    assert "Type=oneshot" in service
    assert "PrivateTmp=true" in service
    assert "NoNewPrivileges=yes" in service
    assert "process_export_jobs_once.py" in service
    assert "OnCalendar=*:04/5" in timer
    assert "max(1, min(5" in script
