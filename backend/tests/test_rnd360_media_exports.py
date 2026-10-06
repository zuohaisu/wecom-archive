"""RND-360 ZIP generation, durable state, notice and expiry coverage."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.db.base import Base
from app.db.models import AdminUser, ExportJob, ExportMonthlyUsage, Tenant
from app.db.session import get_db
from app.main import create_app
from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageOperationError,
    MediaStorageUnavailable,
)
from app.routers import exports as export_router
from app.services import export_jobs


NOW = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)


class _FakeQiniuExportProvider:
    """In-memory Qiniu boundary for Dora export-worker tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.operation_state = "processing"
        self.status_error: Exception | None = None
        self.submitted: tuple[str, str] | None = None
        self.submit_error: Exception | None = None

    def size_bytes(self, storage_ref: str) -> int:
        if storage_ref not in self.objects:
            raise MediaObjectNotFound("missing")
        return len(self.objects[storage_ref])

    def get_sha256_and_size(self, storage_ref: str) -> tuple[str, int]:
        if storage_ref not in self.objects:
            raise MediaObjectNotFound("missing")
        payload = self.objects[storage_ref]
        return hashlib.sha256(payload).hexdigest(), len(payload)

    def get_download_url(self, storage_ref: str, *, expires_in: int) -> str:
        assert expires_in == export_jobs.EXPORT_SOURCE_URL_TTL_SECONDS
        if storage_ref not in self.objects:
            raise MediaObjectNotFound("missing")
        return f"https://media.example.test/{storage_ref}?e=1&token=fake-token"

    def save_bytes(self, storage_ref: str, data: bytes) -> str:
        self.objects[storage_ref] = data
        return storage_ref

    def submit_media_zip(self, index_ref: str, output_ref: str) -> str:
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted = (index_ref, output_ref)
        return "persistent-operation-1"

    def get_persistent_operation_status(self, operation_id: str):
        assert operation_id == "persistent-operation-1"
        if self.status_error is not None:
            raise self.status_error
        if self.operation_state == "succeeded":
            assert self.submitted is not None
            self.objects[self.submitted[1]] = b"PK-qiniu-generated"
        return SimpleNamespace(state=self.operation_state)

    def exists(self, storage_ref: str) -> bool:
        return storage_ref in self.objects

    def delete(self, storage_ref: str) -> bool:
        return self.objects.pop(storage_ref, None) is not None


def _qiniu_media_row(*, payload: bytes = b"fixed-original-media"):
    return SimpleNamespace(
        id=7,
        archive_message_id=42,
        file_type="../../image",
        local_path=None,
        storage_backend="qiniu_kodo",
        storage_ref="tenants/tenant-a/images/original.jpg",
        file_size=len(payload),
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _use_fake_qiniu(
    monkeypatch: pytest.MonkeyPatch, provider: _FakeQiniuExportProvider
) -> None:
    monkeypatch.setattr(export_jobs, "_qiniu_export_provider", lambda: provider)
    monkeypatch.setattr(
        export_jobs, "_qiniu_export_provider_for_existing_job", lambda: provider
    )


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
            ExportMonthlyUsage.__table__,
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


def test_export_api_is_owner_only_and_quota_is_server_derived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory()
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    member = SimpleNamespace(id="member", role="admin", email="member@example.com")
    app.dependency_overrides[get_current_user] = lambda: (member, "tenant-a")
    with TestClient(app) as client:
        assert client.get("/api/admin/exports/quota").status_code == 403

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "synthetic-export-test-key")
    monkeypatch.setenv("SMTP_FROM", "archive@example.com")
    owner = SimpleNamespace(id="owner-a", role="owner", email="owner@example.com")
    app.dependency_overrides[get_current_user] = lambda: (owner, "tenant-a")
    with TestClient(app) as client:
        response = client.get("/api/admin/exports/quota")
    assert response.status_code == 200
    body = response.json()
    assert body["text"]["remaining"] == body["text"]["limit"] == 10
    assert body["media_zip"]["remaining"] == body["media_zip"]["limit"] == 1
    assert body["notification_email_hint"] == "o***@example.com"


def test_qiniu_submission_uses_safe_private_index_and_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeQiniuExportProvider()
    row = _qiniu_media_row()
    provider.objects[row.storage_ref] = b"fixed-original-media"
    _use_fake_qiniu(monkeypatch, provider)
    monkeypatch.setattr(export_jobs, "_media_rows", lambda _db, _tenant: [row])
    fake_db = SimpleNamespace(rollback=lambda: None)
    job = SimpleNamespace(id="job-a", tenant_id="tenant-a")

    submission = export_jobs._build_qiniu_manifest_and_index(fake_db, job)

    assert provider.submitted == (
        submission.index_ref,
        "tenants/tenant-a/exports/job-a.zip",
    )
    manifest = provider.objects[submission.manifest_ref].decode("utf-8-sig")
    index = provider.objects[submission.index_ref].decode("utf-8")
    assert "media/image/42-7.jpg" in manifest
    assert hashlib.sha256(b"fixed-original-media").hexdigest() in manifest
    assert ".." not in manifest
    assert row.storage_ref not in manifest
    assert row.storage_ref not in index
    # The index is Qiniu mode-4 syntax: the source URL and alias are encoded,
    # so neither a source key nor a signed token appears as plaintext there.
    assert "fake-token" not in index
    source_url = "https://media.example.test/tenants/tenant-a/images/original.jpg?e=1&token=fake-token"
    assert base64.urlsafe_b64encode(source_url.encode()).decode() in index


def test_qiniu_submission_rejects_checksum_mismatch_before_mkzip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeQiniuExportProvider()
    row = _qiniu_media_row()
    row.checksum_sha256 = "0" * 64
    provider.objects[row.storage_ref] = b"fixed-original-media"
    _use_fake_qiniu(monkeypatch, provider)
    monkeypatch.setattr(export_jobs, "_media_rows", lambda _db, _tenant: [row])
    fake_db = SimpleNamespace(rollback=lambda: None)
    job = SimpleNamespace(id="job-a", tenant_id="tenant-a")

    with pytest.raises(MediaStorageOperationError, match="checksum verification failed"):
        export_jobs._build_qiniu_manifest_and_index(fake_db, job)

    assert provider.submitted is None
    assert not any("export-work" in ref for ref in provider.objects)


def test_qiniu_submission_rejects_size_mismatch_before_mkzip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeQiniuExportProvider()
    row = _qiniu_media_row()
    row.file_size += 1
    provider.objects[row.storage_ref] = b"fixed-original-media"
    _use_fake_qiniu(monkeypatch, provider)
    monkeypatch.setattr(export_jobs, "_media_rows", lambda _db, _tenant: [row])
    fake_db = SimpleNamespace(rollback=lambda: None)
    job = SimpleNamespace(id="job-a", tenant_id="tenant-a")

    with pytest.raises(MediaStorageOperationError, match="size verification failed"):
        export_jobs._build_qiniu_manifest_and_index(fake_db, job)

    assert provider.submitted is None
    assert not any("export-work" in ref for ref in provider.objects)


def test_worker_polls_qiniu_then_emails_and_expires_in_seven_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory()
    provider = _FakeQiniuExportProvider()
    row = _qiniu_media_row()
    provider.objects[row.storage_ref] = b"fixed-original-media"
    captured = {}
    monkeypatch.setenv("ADMIN_DOMAIN", "archive.example.com")
    _use_fake_qiniu(monkeypatch, provider)
    monkeypatch.setattr(export_jobs, "_media_rows", lambda _db, _tenant: [row])

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
        submitted = export_jobs.run_export_maintenance_once(db, now=NOW)
        job = db.scalar(select(ExportJob))
        assert submitted.claimed == 1
        assert submitted.ready == submitted.notifications_sent == 0
        assert job.status == "processing"
        assert job.storage_ref is None
        assert job.provider_operation_id == "persistent-operation-1"

        provider.operation_state = "succeeded"
        summary = export_jobs.run_export_maintenance_once(
            db, now=NOW + timedelta(minutes=5)
        )
        db.refresh(job)
        assert summary.ready == summary.notifications_sent == 1
        assert job.status == "ready"
        assert job.expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(
            minutes=5, days=7
        )
        assert job.storage_backend == "qiniu_kodo"
        assert job.storage_ref == f"tenants/tenant-a/exports/{job.id}.zip"
        assert job.provider_index_ref is None
        assert job.provider_manifest_ref is None
        assert provider.exists(job.storage_ref)
        assert captured["email"] == "owner@example.com"
        assert captured["link"].startswith(
            "https://archive.example.com/admin/exports?job="
        )
        assert "/api/admin/exports/jobs/" not in captured["link"]

        expired = export_jobs.run_export_maintenance_once(
            db, now=NOW + timedelta(minutes=5, days=7)
        )
        db.refresh(job)
        assert expired.expired == 1
        assert job.status == "expired"
        assert job.storage_ref is None
        assert provider.objects == {row.storage_ref: b"fixed-original-media"}


def test_generation_retries_then_fails_without_ready_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory()
    provider = _FakeQiniuExportProvider()
    row = _qiniu_media_row()
    _use_fake_qiniu(monkeypatch, provider)
    monkeypatch.setattr(export_jobs, "_media_rows", lambda _db, _tenant: [row])
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


def test_terminal_qiniu_work_cleanup_retries_after_ready_delete_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory()
    provider = _FakeQiniuExportProvider()
    row = _qiniu_media_row()
    provider.objects[row.storage_ref] = b"fixed-original-media"
    _use_fake_qiniu(monkeypatch, provider)
    monkeypatch.setattr(export_jobs, "_media_rows", lambda _db, _tenant: [row])

    with factory() as db:
        job = export_jobs.create_media_export_job(
            db, tenant_id="tenant-a", requested_by="owner-a", now=NOW
        )
        db.commit()
        export_jobs.run_export_maintenance_once(db, now=NOW)
        provider.operation_state = "succeeded"
        original_delete = provider.delete
        failed_attempts = {"remaining": 2}

        def delete_once(ref: str) -> bool:
            if failed_attempts["remaining"] and ref.endswith("-index.txt"):
                failed_attempts["remaining"] -= 1
                return False
            return original_delete(ref)

        monkeypatch.setattr(provider, "delete", delete_once)
        first = export_jobs.run_export_maintenance_once(
            db, now=NOW + timedelta(minutes=5)
        )
        db.refresh(job)
        assert first.ready == 1
        assert job.status == "ready"
        assert job.provider_index_ref is not None
        assert job.last_error == "cleanup_pending"

        second = export_jobs.run_export_maintenance_once(
            db, now=NOW + timedelta(minutes=10)
        )
        db.refresh(job)
        assert second.cleanup_pending == 0
        assert job.provider_index_ref is None
        assert job.provider_manifest_ref is None
        assert job.last_error is None


def test_qiniu_status_outage_keeps_submitted_operation_for_later_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory()
    provider = _FakeQiniuExportProvider()
    row = _qiniu_media_row()
    provider.objects[row.storage_ref] = b"fixed-original-media"
    _use_fake_qiniu(monkeypatch, provider)
    monkeypatch.setattr(export_jobs, "_media_rows", lambda _db, _tenant: [row])

    with factory() as db:
        job = export_jobs.create_media_export_job(
            db, tenant_id="tenant-a", requested_by="owner-a", now=NOW
        )
        db.commit()
        export_jobs.run_export_maintenance_once(db, now=NOW)
        provider.status_error = MediaStorageUnavailable("temporary outage")

        summary = export_jobs.run_export_maintenance_once(
            db, now=NOW + timedelta(minutes=5)
        )
        db.refresh(job)

        assert summary.ready == summary.retried == summary.failed == 0
        assert job.status == "processing"
        assert job.provider_operation_id == "persistent-operation-1"
        assert job.attempt_count == 1
        assert job.last_error == "storage_unavailable"


def test_ready_qiniu_export_download_redirects_directly_to_private_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory()
    expected_ref = {"value": None}

    class _DownloadProvider:
        def exists(self, storage_ref: str) -> bool:
            assert storage_ref == expected_ref["value"]
            return True

        def supports_local_path(self) -> bool:
            return False

        def get_download_url(self, storage_ref: str, *, expires_in: int) -> str:
            assert storage_ref == expected_ref["value"]
            assert 1 <= expires_in <= 900
            return f"https://media.example.test/{storage_ref}?e=1&token=fake-token"

    monkeypatch.setattr(
        export_router,
        "get_media_storage_provider_for_backend",
        lambda backend: _DownloadProvider(),
    )
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    owner = SimpleNamespace(id="owner-a", role="owner", email="owner@example.com")
    app.dependency_overrides[get_current_user] = lambda: (owner, "tenant-a")
    with factory() as db:
        job = export_jobs.create_media_export_job(
            db,
            tenant_id="tenant-a",
            requested_by="owner-a",
            now=datetime.now(timezone.utc),
        )
        job.status = "ready"
        job.storage_backend = "qiniu_kodo"
        job.storage_ref = f"tenants/tenant-a/exports/{job.id}.zip"
        expected_ref["value"] = job.storage_ref
        job.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        db.commit()
        job_id = job.id

    with TestClient(app) as client:
        response = client.get(
            f"/api/admin/exports/jobs/{job_id}/download", follow_redirects=False
        )

    assert response.status_code == 307
    assert response.headers["location"] == (
        f"https://media.example.test/{expected_ref['value']}?e=1&token=fake-token"
    )
    assert response.headers["cache-control"] == "private, no-store"


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


def test_0050_migration_adds_and_removes_qiniu_operation_columns(tmp_path: Path) -> None:
    migration_path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "0050_rnd397_qiniu_media_export.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0050", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    engine = create_engine(f"sqlite:///{tmp_path / 'qiniu-export-migration.db'}")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE export_jobs (id VARCHAR(36) PRIMARY KEY)"))
        operations = Operations(MigrationContext.configure(connection))
        migration.op = operations
        migration.upgrade()
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(export_jobs)"))
        }
        assert {
            "provider_operation_id",
            "provider_index_ref",
            "provider_manifest_ref",
        } <= columns
        migration.downgrade()
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(export_jobs)"))
        }
        assert "provider_operation_id" not in columns


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
