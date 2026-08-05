"""RND-337 persistent reachability-check tests (SQLite production-shaped)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.test_reachability_audit import _insert_message, _make_session

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
_LIVE_TEST_DATABASE_URL = os.environ.get("RND337_TEST_DATABASE_URL", "")
_LIVE_TEST_DATABASE_NAME = urlparse(_LIVE_TEST_DATABASE_URL).path.lstrip("/")
# Anchored to this file, not the CWD: CI runs pytest from backend/, local
# runs sometimes from the repo root (same convention as the other
# migration tests, e.g. test_voice_playback_migration.py).
_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic/versions/0032_reachability_audit_runs.py"
)
_BACKEND_DIR = Path(__file__).resolve().parent.parent

_RUNS_SCHEMA = """
CREATE TABLE reachability_audit_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    scope_from DATETIME NOT NULL,
    scope_to DATETIME NOT NULL,
    scope_max_message_id INTEGER NOT NULL,
    matching_count INTEGER NOT NULL DEFAULT 0,
    checked_count INTEGER NOT NULL DEFAULT 0,
    reachable_count INTEGER NOT NULL DEFAULT 0,
    unreachable_count INTEGER NOT NULL DEFAULT 0,
    reason_counts JSON NOT NULL DEFAULT '{}',
    safe_error_code TEXT,
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(status IN ('checking', 'completed', 'incomplete', 'error')),
    CHECK(source IN ('manual')),
    CHECK(matching_count >= 0 AND checked_count >= 0 AND reachable_count >= 0 AND unreachable_count >= 0)
);
CREATE UNIQUE INDEX uq_reachability_audit_runs_tenant_checking
ON reachability_audit_runs(tenant_id) WHERE status = 'checking';
"""


@pytest.fixture()
def db() -> Session:
    session = _make_session()
    for statement in _RUNS_SCHEMA.strip().split(";"):
        if statement.strip():
            session.execute(text(statement))
    session.commit()
    yield session
    session.close()


def _msg_time(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _insert_candidate(db: Session, *, tenant_id: str = TENANT_A, when: datetime = NOW):
    return _insert_message(
        db,
        tenant_id=tenant_id,
        sender=None,  # avoids membership work; still a classifier-owned reason.
        msgtime=_msg_time(when),
    )


def test_reachability_entrypoints_import_app_when_executed_as_scripts(tmp_path: Path) -> None:
    """Production invokes both files directly, without relying on PYTHONPATH."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("DATABASE_URL", None)
    env["REACHABILITY_AUTOMATION_LOCK_PATH"] = str(tmp_path / "reachability.lock")

    manual = subprocess.run(
        [sys.executable, "scripts/run_reachability_check_once.py", "probe-public-id"],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    automation = subprocess.run(
        [sys.executable, "scripts/run_reachability_automation_once.py", "reconcile"],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert manual.returncode == 1
    assert manual.stdout == "[FAIL] DATABASE_URL is not set\n"
    assert "ModuleNotFoundError" not in manual.stderr
    assert automation.returncode == 1
    assert "status=database_unavailable" in automation.stdout
    assert "ModuleNotFoundError" not in automation.stderr


def test_model_and_migration_are_field_aligned_and_data_minimized() -> None:
    from app.db.models import ReachabilityAuditRun

    columns = {column.name for column in ReachabilityAuditRun.__table__.columns}
    assert columns == {
        "id", "public_id", "tenant_id", "status", "source", "algorithm_version",
        "scope_from", "scope_to", "scope_max_message_id", "matching_count",
        "checked_count", "reachable_count", "unreachable_count", "reason_counts",
        "safe_error_code", "started_at", "completed_at", "created_at",
    }
    forbidden = {"content", "payload", "sender", "recipient", "room", "msgid", "archive_message_id"}
    assert not any(any(token in column for token in forbidden) for column in columns)
    migration = _MIGRATION_PATH.read_text()
    assert 'down_revision: Union[str, None] = "0031"' in migration
    for column in columns - {"id"}:
        assert f'"{column}"' in migration
    assert "ck_reachability_audit_runs_counts_nonnegative" in migration
    assert "uq_reachability_audit_runs_tenant_checking" in migration


def test_create_freezes_utc_seven_day_scope_and_candidate_watermark(db: Session) -> None:
    from app.services.reachability_check_service import create_or_reuse_run, execute_run, snapshot_for_run

    in_scope = _insert_candidate(db, when=NOW - timedelta(days=1))
    old = _insert_candidate(db, when=NOW - timedelta(days=8))
    run, created = create_or_reuse_run(db, TENANT_A, now=NOW)
    assert created is True
    assert run.scope_from.replace(tzinfo=timezone.utc) == NOW - timedelta(days=7)
    assert run.scope_to.replace(tzinfo=timezone.utc) == NOW
    assert run.scope_max_message_id == in_scope.id
    assert run.matching_count == 1

    # Both a newer id in range and a historical backfill are outside the frozen set.
    _insert_candidate(db, when=NOW - timedelta(hours=1))
    _insert_candidate(db, when=NOW - timedelta(days=9))
    assert execute_run(db, run.public_id) == "completed"
    db.refresh(run)
    assert run.checked_count == run.matching_count == 1
    assert run.checked_count != 3
    assert old.id > 0
    snapshot = snapshot_for_run(run)
    assert snapshot["state"] == "attention"
    assert sum(snapshot["reason_counts"].values()) == snapshot["counts"]["checked"]


def test_zero_candidate_run_completes_as_no_data(db: Session) -> None:
    from app.services.reachability_check_service import create_or_reuse_run, execute_run, snapshot_for_run

    run, _ = create_or_reuse_run(db, TENANT_A, now=NOW)
    assert execute_run(db, run.public_id) == "completed"
    db.refresh(run)
    assert run.checked_count == run.matching_count == 0
    assert snapshot_for_run(run)["state"] == "no_data"


@pytest.mark.parametrize("candidate_count", [2000, 2001])
def test_full_pagination_classifies_every_candidate_once(db: Session, candidate_count: int) -> None:
    from app.db.models import ArchiveMessage
    from app.services.reachability_check_service import create_or_reuse_run, execute_run

    rows = [
        ArchiveMessage(
            msgid=f"test-{index}", seq=index, publickey_ver=1,
            encrypt_random_key="x", encrypt_chat_msg="y", decrypt_status="success",
            sender=None, tenant_id=TENANT_A, msgtime=_msg_time(NOW - timedelta(minutes=1)),
        )
        for index in range(candidate_count)
    ]
    db.add_all(rows)
    db.commit()
    run, _ = create_or_reuse_run(db, TENANT_A, now=NOW)
    assert run.matching_count == candidate_count
    assert execute_run(db, run.public_id) == "completed"
    db.refresh(run)
    assert run.checked_count == candidate_count
    assert run.unreachable_count == candidate_count
    assert sum(run.reason_counts.values()) == candidate_count


@pytest.mark.parametrize(
    ("status", "matching", "checked", "reachable", "unreachable", "reasons", "expected"),
    [
        ("completed", 1, 1, 1, 0, {"ok": 1}, "healthy"),
        ("completed", 1, 1, 0, 1, {"missing": 1}, "attention"),
        ("completed", 0, 0, 0, 0, {}, "no_data"),
        ("checking", 1, 0, 0, 0, {}, "checking"),
        ("incomplete", 1, 0, 0, 0, {}, "incomplete"),
        ("error", 1, 0, 0, 0, {}, "error"),
        ("completed", 2, 1, 1, 0, {"ok": 1}, "incomplete"),
        ("completed", 1, 1, 1, 0, {}, "incomplete"),
        ("completed", 1, 1, 1, 0, {"ok": "one"}, "incomplete"),
    ],
)
def test_state_machine_never_claims_partial_or_errors_healthy(
    status, matching, checked, reachable, unreachable, reasons, expected
) -> None:
    from app.db.models import ReachabilityAuditRun
    from app.services.reachability_check_service import state_for_run

    run = ReachabilityAuditRun(
        public_id="00000000-0000-0000-0000-000000000001", tenant_id=TENANT_A,
        status=status, source="manual", algorithm_version="v", scope_from=NOW,
        scope_to=NOW, scope_max_message_id=0, matching_count=matching,
        checked_count=checked, reachable_count=reachable, unreachable_count=unreachable,
        reason_counts=reasons,
    )
    state, _complete = state_for_run(run)
    assert state == expected
    if expected != "healthy":
        assert state != "healthy"


def test_stale_run_becomes_incomplete_and_tenants_do_not_block_each_other(db: Session) -> None:
    from app.services.reachability_check_service import create_or_reuse_run, latest_snapshot

    first, _ = create_or_reuse_run(db, TENANT_A, now=NOW)
    other, other_created = create_or_reuse_run(db, TENANT_B, now=NOW)
    assert other_created is True and other.public_id != first.public_id
    latest = latest_snapshot(db, TENANT_A, now=NOW + timedelta(minutes=16))
    assert latest["state"] == "incomplete"
    assert latest["safe_error_code"] == "stale_checking"
    second, created = create_or_reuse_run(db, TENANT_A, now=NOW + timedelta(minutes=16))
    assert created is True and second.public_id != first.public_id


def test_runner_failure_and_retry_fail_closed_without_duplicate_counts(db: Session, monkeypatch) -> None:
    import app.services.reachability_check_service as service

    _insert_candidate(db)
    run, _ = service.create_or_reuse_run(db, TENANT_A, now=NOW)
    assert service.execute_run(db, "00000000-0000-0000-0000-000000000000") == "not_found"

    monkeypatch.setattr(service, "build_message_reachability_report", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("SENTINEL_TRACEBACK")))
    assert service.execute_run(db, run.public_id) == "error"
    db.refresh(run)
    assert run.status == "error"
    assert run.safe_error_code == "scan_failed"
    assert service.state_for_run(run)[0] == "error"

    # A terminal run is never scanned or accumulated a second time.
    before = (run.checked_count, run.reachable_count, run.unreachable_count, run.reason_counts)
    assert service.execute_run(db, run.public_id) == "not_active"
    db.refresh(run)
    assert (run.checked_count, run.reachable_count, run.unreachable_count, run.reason_counts) == before


def test_latest_without_history_is_no_data_or_incomplete_without_fake_timestamp(db: Session) -> None:
    from app.services.reachability_check_service import latest_snapshot

    empty = latest_snapshot(db, TENANT_A, now=NOW)
    assert empty["state"] == "no_data"
    assert empty["last_checked_at"] is None
    _insert_candidate(db)
    pending = latest_snapshot(db, TENANT_A, now=NOW)
    assert pending["state"] == "incomplete"
    assert pending["last_checked_at"] is None


def test_same_tenant_active_run_is_reused_by_database_guard(db: Session) -> None:
    from app.services.reachability_check_service import create_or_reuse_run

    first, created = create_or_reuse_run(db, TENANT_A, now=NOW)
    second, second_created = create_or_reuse_run(db, TENANT_A, now=NOW)
    assert created is True
    assert second_created is False
    assert second.public_id == first.public_id


def test_partial_unique_index_rejects_second_active_run(db: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.db.models import ReachabilityAuditRun
    from app.services.reachability_check_service import create_or_reuse_run

    first, _ = create_or_reuse_run(db, TENANT_A, now=NOW)
    db.add(
        ReachabilityAuditRun(
            public_id="00000000-0000-0000-0000-000000000099", tenant_id=TENANT_A,
            status="checking", source="manual", algorithm_version="v",
            scope_from=NOW, scope_to=NOW, scope_max_message_id=0,
            matching_count=0, checked_count=0, reachable_count=0,
            unreachable_count=0, reason_counts={}, started_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.refresh(first)
    assert first.status == "checking"


@pytest.mark.skipif(
    not _LIVE_TEST_DATABASE_URL or "test" not in _LIVE_TEST_DATABASE_NAME,
    reason="RND337_TEST_DATABASE_URL must target a disposable *_test PostgreSQL database",
)
def test_two_postgres_sessions_concurrently_reuse_one_active_run() -> None:
    """The partial unique index resolves a real two-session creation race."""
    from app.db.models import ReachabilityAuditRun, Tenant
    from app.services.reachability_check_service import create_or_reuse_run

    engine = create_engine(_LIVE_TEST_DATABASE_URL)
    tenant_id = str(uuid4())
    with Session(engine) as setup:
        setup.add(Tenant(id=tenant_id, name="RND337 test", slug=f"rnd337-{tenant_id}"))
        setup.commit()
    barrier = Barrier(2)

    def create_in_own_session():
        with Session(engine) as session:
            barrier.wait(timeout=10)
            run, _created = create_or_reuse_run(session, tenant_id, now=NOW)
            return run.public_id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            public_ids = list(executor.map(lambda _unused: create_in_own_session(), range(2)))
        assert public_ids[0] == public_ids[1]
        with Session(engine) as verify:
            assert (
                verify.query(ReachabilityAuditRun)
                .filter(
                    ReachabilityAuditRun.tenant_id == tenant_id,
                    ReachabilityAuditRun.status == "checking",
                )
                .count()
                == 1
            )
    finally:
        with Session(engine) as cleanup:
            cleanup.query(ReachabilityAuditRun).filter(
                ReachabilityAuditRun.tenant_id == tenant_id
            ).delete(synchronize_session=False)
            cleanup.query(Tenant).filter(Tenant.id == tenant_id).delete(synchronize_session=False)
            cleanup.commit()
        engine.dispose()


def test_run_execution_isolates_tenant_message_candidates(db: Session) -> None:
    from app.services.reachability_check_service import create_or_reuse_run, execute_run

    _insert_candidate(db, tenant_id=TENANT_A)
    _insert_candidate(db, tenant_id=TENANT_B)
    run_a, _ = create_or_reuse_run(db, TENANT_A, now=NOW)
    run_b, _ = create_or_reuse_run(db, TENANT_B, now=NOW)
    assert execute_run(db, run_a.public_id) == "completed"
    db.refresh(run_a)
    db.refresh(run_b)
    assert run_a.matching_count == run_a.checked_count == 1
    assert run_b.matching_count == 1
    assert run_b.checked_count == 0


def _assert_no_sentinels(value):
    sentinels = (
        "SENTINEL_MESSAGE_BODY", "SENTINEL_STRUCTURED_CONTENT", "SENTINEL_PASSWORD",
        "SENTINEL_PASSWORD_HASH", "SENTINEL_TOKEN", "SENTINEL_SECRET", "SENTINEL_SIGNED_URL",
        "SENTINEL_STORAGE_KEY", "/sentinel/fs/path", "SENTINEL_SEARCH_TEXT",
        "SENTINEL_TRACEBACK", "SENTINEL_SENDER", "SENTINEL_RECIPIENT", "SENTINEL_ROOM",
        "SENTINEL_RAW_MSGID",
    )
    if isinstance(value, dict):
        for key, item in value.items():
            assert not any(item_sentinel in str(key) for item_sentinel in sentinels)
            _assert_no_sentinels(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_sentinels(item)
    else:
        assert not any(sentinel in str(value) for sentinel in sentinels)


def test_api_is_authenticated_async_and_does_not_accept_tenant_input(db: Session, monkeypatch) -> None:
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    spawned = []
    scanner = MagicMock()
    monkeypatch.setattr("app.routers.reachability_checks.subprocess.Popen", lambda args, **kwargs: spawned.append(args))
    monkeypatch.setattr("app.services.reachability_check_service.build_message_reachability_report", scanner)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/api/admin/reachability-checks/latest").status_code == 401
            app.dependency_overrides[get_current_user] = lambda: (MagicMock(), TENANT_A)
            response = client.post("/api/admin/reachability-checks")
            assert response.status_code == 202
            body = response.json()
            assert body["state"] == "checking"
            assert len(spawned) == 1
            assert scanner.call_count == 0
            assert spawned[0][-1] == body["public_run_id"]
            assert TENANT_A not in spawned[0]
            _assert_no_sentinels(body)
            assert client.post("/api/admin/reachability-checks?tenant_id=other").status_code == 422
            assert client.post("/api/admin/reachability-checks", json={"tenant_id": "other"}).status_code == 422
    finally:
        app.dependency_overrides.clear()
